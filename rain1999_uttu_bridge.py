#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rain1999_uttu_bridge —— 故事线 / 时间线 图谱桥接模块

作用：把 uttu.merui.net 的「故事线 Storyline」与「时间线 Timeline」两个页面，
      解析成结构化图谱，并写入 rain1999.py 生成的 SQLite 剧情库，
      使 rain1999_sim.py（故事模拟器）能直接读到官方的剧情顺序与世界史事件。

为什么需要它
------------
rain1999.py 抓取的是每条剧情的**正文**（transcript），它自己用规则
（ARC_RULES + TIMELINE_PHASES）推断分类和顺序。而 /storyline/ 与 /timeline/
两个页面提供的是站方**权威的图结构与年代学**：
  · 故事线：4 条泳道、9 条链、42 个节点的先后关系，外加 13 条跨线连线
            （汇入主线 / 汇出 / 同时发生 / 闪回）
  · 时间线：48 个年份节点、78 条世界史事件，含「风暴前 / 风暴后」双时代语义
这两份信息 rain1999.py 原本拿不到，桥接后即可补齐。

对接键
------
rain1999.py 中 episodes.id = f"{section}_{slug}"（如 main_chapter-0）。
本模块从详情页路径 /story/en/main/chapter-0 反解出 (板块, slug)，
生成同规则的「剧情库ID」，实测与 rain1999.py 内置 FALLBACK_SLUGS
**42/42 全部命中**，因此可精确 JOIN，不依赖标题模糊匹配。

数据库影响（重要）
------------------
默认只做**增量新增**，不改动 rain1999.py 与模拟器原有的三张表：
  · uttu_storyline_node   故事线节点（含泳道、链、顺序、剧情库ID）
  · uttu_storyline_edge   节点间关系（汇入/汇出/同时发生/闪回）
  · uttu_world_event      时间线世界史事件（含归一化时间与关联剧情ID）
  · uttu_meta             抓取元信息与统计
只有显式加 --rewrite-timeline 时，才会用官方图谱顺序覆写 timeline 表
（arc_kind 仍限定在 rain1999 的 main/character/event/side/other 词表内，
 保证 `python rain1999.py timeline --kind main` 与模拟器查询都不受影响）。

纯标准库实现
------------
rain1999.py 不依赖第三方库（只用 urllib + html.parser），本模块同样只用标准库，
可直接放在 rain1999.py 同目录被 import，无需 pip 安装任何东西。

用法
----
    # 抓取两页并写入剧情库（默认库路径 = rain1999/data/rain1999_story.db）
    python rain1999_uttu_bridge.py sync

    # 指定库
    python rain1999_uttu_bridge.py sync --db ./rain1999/data/rain1999_story.db

    # 同时用官方图谱顺序覆写 timeline 表（影响模拟器叙事顺序）
    python rain1999_uttu_bridge.py sync --rewrite-timeline

    # 只导出 JSON，不动数据库
    python rain1999_uttu_bridge.py sync --json-only --out ./graph

    # 用已下载的 HTML 离线解析
    python rain1999_uttu_bridge.py sync --from-cache storyline.html timeline.html

    # 查看写入结果
    python rain1999_uttu_bridge.py show --db ./rain1999/data/rain1999_story.db

    # 被 rain1999.py 调用（已并入下载器）
    python rain1999.py uttu --db rain1999_story.db
"""

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = "https://uttu.merui.net"
STORYLINE_PATH = "/storyline/"
TIMELINE_PATH = "/timeline/"
DEFAULT_DB = os.path.join(HERE, "rain1999", "data", "rain1999_story.db")

SCHEMA_VERSION = "1.1"
STORM_YEAR_INDEX = 30          # data-year-index 30 = 1999「风暴」，此后为回溯段
TIMELINE_PLACEHOLDER = '[{"date":"Date","events":[{"title":"Event 1"}]}]'

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 泳道 -> (中文名, rain1999 的 arc 名, arc_kind)
# arc_kind 必须落在 rain1999.ARC_LABEL 的词表内，否则 --kind 过滤会失效
LANE_MAP = {
    "main":         ("主线剧情", "主线", "main"),
    "events":       ("活动故事", "活动", "event"),
    "side-stories": ("支线故事", "番外", "side"),
    "unknown-line": ("归属待定", "未分类", "other"),
}


# =========================================================================== #
# 一、网络层（复用 rain1999.py 的缓存目录与限速风格）
# =========================================================================== #

class RateLimiter:
    def __init__(self, delay=0.4):
        self.delay = delay
        self._last = 0.0

    def wait(self):
        gap = self._last + self.delay - time.time()
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()


def build_opener():
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", UA),
                     ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
                     ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8"),
                     ("Referer", DEFAULT_BASE + "/")]
    return op


def fetch(url, cache_dir, opener=None, limiter=None, retries=3, timeout=30):
    """带磁盘缓存 + 指数退避重试的 GET。缓存键与 rain1999.py 一致（md5）。"""
    opener = opener or build_opener()
    limiter = limiter or RateLimiter()
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, hashlib.md5(url.encode("utf-8")).hexdigest() + ".html")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(), {"url": url, "来源": "本地缓存", "文件": path}
        except OSError:
            pass
    last = None
    for i in range(retries):
        try:
            limiter.wait()
            with opener.open(url, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                status = getattr(r, "status", 200)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            return body, {"url": url, "来源": "网络抓取", "HTTP状态": status,
                          "字节数": len(body.encode("utf-8")), "文件": path,
                          "抓取时间": datetime.now(timezone.utc)
                                        .replace(microsecond=0).isoformat()}
        except Exception as e:                                   # noqa: BLE001
            last = e
            time.sleep(1.5 * (2 ** i) + random.random() * 0.5)
    raise RuntimeError(f"抓取失败（重试 {retries} 次）：{url} -> {last}")


# =========================================================================== #
# 二、纯标准库 HTML 解析器
# =========================================================================== #
# 注意：data-dates 属性值内部含 <nobr> 标签，朴素正则会被其中的 ">" 截断，
#       必须用 HTMLParser 按属性语义读取。

class _StorylineParser(HTMLParser):
    """收集 storylane / chain / node 与内嵌的 storyline-edges JSON。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.edges_raw = ""
        self.nodes = []            # 保序：[{lane,node,href,title}]
        self._in_edges = False
        self._stack = []           # (kind, payload)
        self._lane = None
        self._chain = -1
        self._cur = None
        self._title_buf = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "")

        if tag == "script" and a.get("id") == "storyline-edges":
            self._in_edges = True
            return

        if "storyline-lane" in cls or ("data-lane" in a and "data-storyline" in a):
            self._lane = a.get("data-storyline") or ""
            self._chain = -1

        if tag == "div" and "node-chain" in cls:
            self._chain += 1

        if tag == "a" and "storyline-node" in cls:
            self._cur = {
                "lane": a.get("data-storyline") or self._lane or "",
                "node": a.get("data-node") or "",
                "href": a.get("href") or "",
                "chain": max(self._chain, 0),
                "title": "",
            }
            self._title_buf = None

        # 节点标题：a.storyline-node 内第一个 .node-title
        if self._cur is not None and "node-title" in cls and self._title_buf is None:
            self._title_buf = []

    def handle_data(self, data):
        if self._in_edges:
            self.edges_raw += data
        if self._title_buf is not None:
            self._title_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_edges:
            self._in_edges = False
        if self._title_buf is not None and tag == "span":
            if self._cur is not None and not self._cur["title"]:
                self._cur["title"] = clean_text("".join(self._title_buf))
            self._title_buf = None
        if tag == "a" and self._cur is not None:
            if self._cur["node"]:
                self.nodes.append(self._cur)
            self._cur = None


class _TimelineParser(HTMLParser):
    """收集 button.year-node 的 data-year / data-year-index / data-dates / 标签。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []            # [{year,index,dates_raw,label}]
        self._depth = 0
        self._cur = None
        self._label_buf = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "")
        if tag == "button" and "year-node" in cls:
            self._cur = {"year": a.get("data-year") or "",
                         "index": a.get("data-year-index"),
                         "dates_raw": a.get("data-dates") or "",
                         "label": ""}
            self._depth = 1
            return
        if self._cur is not None:
            if tag == "button":
                self._depth += 1
            if "node-label" in cls and self._label_buf is None:
                self._label_buf = []

    def handle_data(self, data):
        if self._label_buf is not None:
            self._label_buf.append(data)

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if self._label_buf is not None and tag == "span":
            if not self._cur["label"]:
                self._cur["label"] = clean_text("".join(self._label_buf))
            self._label_buf = None
        if tag == "button":
            self._depth -= 1
            if self._depth <= 0:
                self.items.append(self._cur)
                self._cur = None


# =========================================================================== #
# 三、文本与时间归一化（与 抓取1999故事线与时间线.py 规则一致）
# =========================================================================== #

REDACTED_RE = re.compile(r"[█▓▒×xX*]{2,}")
MONTH_DAY_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})$")
MONTH_DAY_TIME_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})\s+(\d{1,2}):(\d{2})$")
MONTH_REDACTED_RE = re.compile(r"^(\d{1,2})[.\-/]\s*[█▓▒×xX*]+$")
DAY_REDACTED_RE = re.compile(r"^[█▓▒×xX*]+[.\-/]\s*(\d{1,2})$")
ISO_IN_TEXT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DECADE_RE = re.compile(r"^(\d{3})0s$", re.I)
YEAR_RE = re.compile(r"^(\d{4})$")
PERIOD_RE = re.compile(r"(\d{4})\s*(?:~|-|–|至)\s*(\d{4})")
PERIOD_OPEN_RE = re.compile(r"(\d{4})\s*(?:~|-|–)\s*$")
HREF_RE = re.compile(r"/story/(?:en|chs|jp|kr|cht)/([a-zA-Z0-9_.\-]+)/(.+?)/?$")


def clean_text(v):
    """剥离 <nobr> 等标签、还原实体、压缩空白，保留段落换行。"""
    if v is None:
        return ""
    t = str(v)
    t = re.sub(r"</?nobr\s*>", "", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"),
                 ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">")):
        t = t.replace(a, b)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def split_story_href(href):
    """/story/en/main/chapter-0 -> ("main","chapter-0")；用于生成 rain1999 主键。"""
    if not href:
        return "", ""
    m = HREF_RE.search(href.strip())
    if not m:
        return "", ""
    section, rest = m.group(1), m.group(2)
    if "/" in rest:                       # main/trails/800260x -> ("trails","800260x")
        head, tail = rest.rsplit("/", 1)
        section, rest = head.split("/")[-1], tail
    return section, rest


def episode_id(section, slug):
    return f"{section}_{slug}" if section and slug else ""


def parse_year(raw):
    y = (raw or "").strip()
    m = YEAR_RE.match(y)
    if m:
        v = int(m.group(1))
        return v, v, "exact_year"
    m = DECADE_RE.match(y)
    if m:
        v = int(m.group(1) + "0")
        return v, v + 9, "decade"
    m = re.search(r"(\d{4})", y)
    if m:
        v = int(m.group(1))
        return (v, v + 9, "decade") if "s" in y.lower() else (v, v, "exact_year")
    return None, None, "unknown"


def parse_period(label):
    label = (label or "").strip()
    m = PERIOD_RE.search(label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = PERIOD_OPEN_RE.search(label)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d{4})", label)
    return (int(m.group(1)), None) if m else (None, None)


def normalize_time(raw_year, raw_date, description="", year_index=None, label=""):
    """把站点 6 类日期写法归一化成可排序结构，缺失即留空不猜测。"""
    ys, ye, yprec = parse_year(raw_year)
    d = (raw_date or "").strip()
    month = day = None
    tod = ""
    redacted = partial = False

    if d and REDACTED_RE.search(d) and not re.search(r"\d", d):
        prec, iso, redacted = "redacted", (str(ys) if ys else ""), True
    else:
        m = MONTH_DAY_TIME_RE.match(d)
        if m and ys:
            month, day = int(m.group(1)), int(m.group(2))
            tod = f"{int(m.group(3)):02d}:{m.group(4)}"
            prec, iso = "day", f"{ys:04d}-{month:02d}-{day:02d}"
        elif (m := MONTH_DAY_RE.match(d)) and ys:
            month, day = int(m.group(1)), int(m.group(2))
            prec, iso = "day", f"{ys:04d}-{month:02d}-{day:02d}"
        elif (m := MONTH_REDACTED_RE.match(d)):
            month, partial = int(m.group(1)), True
            prec = "month"
            iso = f"{ys:04d}-{month:02d}" if ys else ""
        elif (m := DAY_REDACTED_RE.match(d)):
            day, partial = int(m.group(1)), True
            prec = "year"
            iso = str(ys) if ys else ""
        elif re.match(r"^\d{1,2}$", d):
            month = int(d)
            prec = "month"
            iso = f"{ys:04d}-{month:02d}" if ys else ""
        elif d == "":
            m2 = ISO_IN_TEXT_RE.search(description or "")
            if m2:
                y, mo, da = (int(m2.group(i)) for i in (1, 2, 3))
                if ys in (None, y):
                    ys = ye = y
                    yprec = "exact_year"
                month, day = mo, da
                prec, iso = "day", f"{y:04d}-{mo:02d}-{da:02d}"
            else:
                prec = "decade" if yprec == "decade" else ("year" if ys else "unknown")
                iso = str(ys) if ys else ""
        else:
            prec, iso = "narrative", (str(ys) if ys else "")

    era_no = 0
    if year_index is not None:
        era_no = 0 if year_index <= STORM_YEAR_INDEX else 1
    return {
        "原始年份": (raw_year or "").strip(),
        "原始日期": d,
        "起始年": ys, "结束年": ye, "月": month, "日": day,
        "年份精度": yprec, "日期精度": prec, "标准日期": iso, "时刻": tod,
        "日期已抹除": redacted, "日期部分抹除": partial,
        "叙事时段": (label or "").strip(),
        "时代序号": era_no,
        "排序键": [era_no, year_index if year_index is not None else 0,
                   ys or 0, month or 0, day or 0],
    }


# =========================================================================== #
# 四、页面 -> 图谱
# =========================================================================== #

def build_storyline(html_text):
    p = _StorylineParser()
    p.feed(html_text)

    edges_raw = {}
    if p.edges_raw.strip():
        try:
            edges_raw = json.loads(p.edges_raw)
        except json.JSONDecodeError as e:
            print(f"  [warn] storyline-edges 解析失败：{e}", file=sys.stderr)

    key_map, lanes, order = {}, {}, 0
    for n in p.nodes:
        lane, nid = n["lane"], n["node"]
        sec, slug = split_story_href(n["href"])
        eid = episode_id(sec, slug)
        key_map[(lane, nid)] = eid
        L = lanes.setdefault(lane, {"故事线ID": lane,
                                    "故事线名称": LANE_MAP.get(lane, (lane,))[0],
                                    "arc": LANE_MAP.get(lane, ("", "未分类", "other"))[1],
                                    "arc_kind": LANE_MAP.get(lane, ("", "", "other"))[2],
                                    "链": {}})
        ch = L["链"].setdefault(n["chain"], [])
        ch.append({"节点ID": nid, "标题": n["title"], "链序号": n["chain"],
                   "链内顺序": len(ch), "全局顺序": order,
                   "板块": sec, "slug": slug, "剧情库ID": eid})
        order += 1

    def ref(d):
        if not d:
            return {}
        sl, nd = d.get("storyline", ""), d.get("node", "")
        return {"故事线": sl, "节点": nd, "剧情库ID": key_map.get((sl, nd), ""),
                "接入方向": d.get("into")}

    out_lanes = []
    for lane, L in lanes.items():
        chains = [{"链序号": k, "节点数": len(v),
                   "节点顺序": [x["节点ID"] for x in v], "节点": v}
                  for k, v in sorted(L["链"].items())]
        out_lanes.append({
            "故事线ID": lane, "故事线名称": L["故事线名称"],
            "arc": L["arc"], "arc_kind": L["arc_kind"],
            "是否主线": lane == "main",
            "节点数": sum(c["节点数"] for c in chains),
            "链数": len(chains), "链": chains,
        })

    edges = {
        "汇入主线": [{"来源": ref(e.get("from")), "汇入": ref(e.get("connectsFrom")),
                      "关系": "connects_from", "原始": e}
                     for e in edges_raw.get("nodeLinks", [])],
        "汇出连接": [{"来源": ref(e.get("from")), "汇出至": ref(e.get("connectsTo")),
                      "关系": "connects_to", "原始": e}
                     for e in edges_raw.get("nodeOuts", [])],
        "同时发生或闪回": [{"节点A": ref(e.get("a")), "节点B": ref(e.get("b")),
                          "关系": "same_time",
                          "标签": e.get("label", ""), "B侧标签": e.get("bLabel", ""),
                          "是否有连线": bool(e.get("connected")), "原始": e}
                         for e in edges_raw.get("sameTime", [])],
    }
    all_edges = edges["汇入主线"] + edges["汇出连接"] + edges["同时发生或闪回"]

    return {"泳道": out_lanes, "连线关系": edges,
            "统计": {"故事线数": len(out_lanes),
                     "节点总数": order,
                     "链总数": sum(l["链数"] for l in out_lanes),
                     "连线总数": len(all_edges),
                     "可对接剧情库的节点数": sum(1 for v in key_map.values() if v)}}, all_edges


def build_timeline(html_text):
    p = _TimelineParser()
    p.feed(html_text)

    nodes, flat = [], []
    for it in p.items:
        idx_raw = it.get("index")
        yi = int(idx_raw) if idx_raw is not None and str(idx_raw).lstrip("-").isdigit() else None
        raw_dates = it.get("dates_raw") or ""
        label = it.get("label") or ""
        year = it.get("year") or ""
        era = "风暴前" if (yi is not None and yi <= STORM_YEAR_INDEX) else "风暴后"

        placeholder = raw_dates.strip() == TIMELINE_PLACEHOLDER
        groups = []
        if raw_dates and not placeholder:
            try:
                groups = json.loads(raw_dates)
            except json.JSONDecodeError as e:
                print(f"  [warn] {year} data-dates 解析失败：{e}", file=sys.stderr)

        parsed = []
        for gi, g in enumerate(groups if isinstance(groups, list) else []):
            if not isinstance(g, dict):
                continue
            rd = clean_text(g.get("date", ""))
            evs = []
            for ei, ev in enumerate(g.get("events", []) or []):
                if not isinstance(ev, dict):
                    continue
                desc = clean_text(ev.get("description", ""))
                nt = normalize_time(year, rd, desc, yi, label)
                sec, slug = split_story_href(ev.get("link", ""))
                e = {"标题": clean_text(ev.get("title", "")), "描述": desc,
                     "配图": (ev.get("image") or "").strip(),
                     "关联链接": (ev.get("link") or "").strip(),
                     "关联板块": sec, "关联slug": slug,
                     "关联剧情ID": episode_id(sec, slug),
                     "所属版本": (ev.get("version") or "").strip(),
                     "日期分组序号": gi, "组内序号": ei, "时间": nt}
                evs.append(e)
                flat.append({"年份": year, "年份序号": yi, "时代": era,
                             "叙事时段": label, "标题": e["标题"],
                             "标准日期": nt["标准日期"], "日期精度": nt["日期精度"],
                             "日期已抹除": nt["日期已抹除"],
                             "关联剧情ID": e["关联剧情ID"], "_sort": nt["排序键"]})
            parsed.append({"原始日期": rd, "日期序号": gi,
                           "事件数": len(evs), "事件": evs})

        nodes.append({"年份序号": yi, "年份原文": year, "时代": era,
                      "时代标识": "pre_storm" if era == "风暴前" else "post_storm",
                      "叙事时段": label,
                      "叙事时段起始年": parse_period(label)[0],
                      "叙事时段结束年": parse_period(label)[1],
                      "是否有数据": (not placeholder) and bool(parsed),
                      "数据状态": ("站点未填充" if placeholder
                                   else ("已填充" if parsed else "解析为空")),
                      "日期分组数": len(parsed),
                      "事件数": sum(g["事件数"] for g in parsed),
                      "日期分组": parsed})

    flat.sort(key=lambda e: e["_sort"])
    for i, e in enumerate(flat):
        e["时间轴序号"] = i + 1
        e.pop("_sort", None)

    return {"时间轴说明": {
                "排序规则": "先按时代（风暴前 → 风暴后），再按站点年份序号，最后按归一化年月日",
                "风暴节点": f"年份序号 {STORM_YEAR_INDEX}（1999，标签 The Storm）",
                "风暴后语义": "data-year 是回溯抵达的旧年份，叙事先后由『叙事时段』决定",
                "日期已抹除": "██.██ 为官方刻意隐去，保留标记不猜测"},
            "年份节点": nodes, "全局事件时间轴": flat,
            "统计": {"年份节点数": len(nodes),
                     "已填充节点数": sum(1 for n in nodes if n["是否有数据"]),
                     "未填充节点数": sum(1 for n in nodes if not n["是否有数据"]),
                     "事件总数": len(flat),
                     "风暴前节点数": sum(1 for n in nodes if n["时代"] == "风暴前"),
                     "风暴后节点数": sum(1 for n in nodes if n["时代"] == "风暴后"),
                     "可关联剧情的事件数": sum(1 for e in flat if e["关联剧情ID"])}}


# =========================================================================== #
# 五、写入 SQLite（增量新增表，默认不动原有三张表）
# =========================================================================== #

BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS uttu_storyline_node (
    storyline_id  TEXT NOT NULL,
    storyline_zh  TEXT,
    arc           TEXT,
    arc_kind      TEXT,
    is_main       INTEGER,
    node_id       TEXT NOT NULL,
    title         TEXT,
    chain_index   INTEGER,
    order_in_chain INTEGER,
    global_order  INTEGER,
    section       TEXT,
    slug          TEXT,
    episode_id    TEXT,
    PRIMARY KEY (storyline_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_uttu_node_ep ON uttu_storyline_node(episode_id);
CREATE INDEX IF NOT EXISTS idx_uttu_node_ord ON uttu_storyline_node(global_order);

CREATE TABLE IF NOT EXISTS uttu_storyline_edge (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    relation     TEXT NOT NULL,
    a_storyline  TEXT, a_node TEXT, a_episode_id TEXT,
    b_storyline  TEXT, b_node TEXT, b_episode_id TEXT,
    label        TEXT,
    connected    INTEGER,
    raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_uttu_edge_a ON uttu_storyline_edge(a_episode_id);
CREATE INDEX IF NOT EXISTS idx_uttu_edge_b ON uttu_storyline_edge(b_episode_id);

CREATE TABLE IF NOT EXISTS uttu_world_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    axis_order    INTEGER NOT NULL,
    era           TEXT,
    era_code      TEXT,
    year_index    INTEGER,
    year_raw      TEXT,
    narrative     TEXT,
    date_raw      TEXT,
    iso_date      TEXT,
    time_of_day   TEXT,
    date_precision TEXT,
    date_redacted INTEGER,
    title         TEXT,
    description   TEXT,
    image         TEXT,
    version       TEXT,
    episode_id    TEXT,
    sort_key      TEXT,
    has_data      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_uttu_ev_axis ON uttu_world_event(axis_order);
CREATE INDEX IF NOT EXISTS idx_uttu_ev_ep   ON uttu_world_event(episode_id);
CREATE INDEX IF NOT EXISTS idx_uttu_ev_era  ON uttu_world_event(era_code, axis_order);

CREATE TABLE IF NOT EXISTS uttu_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def install_schema(conn):
    conn.executescript(BRIDGE_SCHEMA)


def write_graph(conn, sl, tl, sl_meta, tl_meta, base):
    cur = conn.cursor()
    for t in ("uttu_storyline_node", "uttu_storyline_edge", "uttu_world_event"):
        cur.execute(f"DELETE FROM {t}")

    n_nodes = 0
    for lane in sl["泳道"]:
        for ch in lane["链"]:
            for n in ch["节点"]:
                cur.execute(
                    "INSERT INTO uttu_storyline_node (storyline_id, storyline_zh, arc,"
                    " arc_kind, is_main, node_id, title, chain_index, order_in_chain,"
                    " global_order, section, slug, episode_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lane["故事线ID"], lane["故事线名称"], lane["arc"], lane["arc_kind"],
                     1 if lane["是否主线"] else 0, n["节点ID"], n["标题"],
                     n["链序号"], n["链内顺序"], n["全局顺序"],
                     n["板块"], n["slug"], n["剧情库ID"]))
                n_nodes += 1

    n_edges = 0
    for e in sl["连线关系"]["汇入主线"] + sl["连线关系"]["汇出连接"]:
        a, b = e["来源"], e.get("汇入") or e.get("汇出至") or {}
        cur.execute("INSERT INTO uttu_storyline_edge (relation,a_storyline,a_node,"
                    "a_episode_id,b_storyline,b_node,b_episode_id,label,connected,raw_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (e["关系"], a.get("故事线"), a.get("节点"), a.get("剧情库ID"),
                     b.get("故事线"), b.get("节点"), b.get("剧情库ID"), "", 1,
                     json.dumps(e["原始"], ensure_ascii=False)))
        n_edges += 1
    for e in sl["连线关系"]["同时发生或闪回"]:
        a, b = e["节点A"], e["节点B"]
        cur.execute("INSERT INTO uttu_storyline_edge (relation,a_storyline,a_node,"
                    "a_episode_id,b_storyline,b_node,b_episode_id,label,connected,raw_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("same_time", a.get("故事线"), a.get("节点"), a.get("剧情库ID"),
                     b.get("故事线"), b.get("节点"), b.get("剧情库ID"),
                     e.get("标签") or e.get("B侧标签") or "",
                     1 if e.get("是否有连线") else 0,
                     json.dumps(e["原始"], ensure_ascii=False)))
        n_edges += 1

    # 世界史事件：先按归一化排序键整体排好序，再写入，axis_order 即最终时间轴序号。
    # 占位（站点未填充）节点也参与排序，保留年代骨架，但 has_data=0 便于过滤。
    rows = []
    for node in tl["年份节点"]:
        era_no = 0 if node["时代"] == "风暴前" else 1
        if not node["是否有数据"]:
            rows.append({
                "sort": [era_no, node["年份序号"] or 0, 0, 0, 0],
                "vals": (node["时代"], node["时代标识"],
                         node["年份序号"], node["年份原文"], node["叙事时段"],
                         "", "", "", "unknown", 0, "（站点未填充）", "", "", "", "",
                         json.dumps([era_no, node["年份序号"] or 0, 0, 0, 0]), 0)})
            continue
        for g in node["日期分组"]:
            for ev in g["事件"]:
                t = ev["时间"]
                rows.append({
                    "sort": t["排序键"],
                    "vals": (node["时代"], node["时代标识"],
                             node["年份序号"], node["年份原文"], node["叙事时段"],
                             t["原始日期"], t["标准日期"], t["时刻"], t["日期精度"],
                             1 if t["日期已抹除"] else 0, ev["标题"], ev["描述"],
                             ev["配图"], ev["所属版本"], ev["关联剧情ID"],
                             json.dumps(t["排序键"]), 1)})

    rows.sort(key=lambda r: r["sort"])      # 稳定排序：同一日期的事件保持站点原顺序
    for i, r in enumerate(rows, start=1):
        cur.execute("INSERT INTO uttu_world_event (axis_order,era,era_code,year_index,"
                    "year_raw,narrative,date_raw,iso_date,time_of_day,date_precision,"
                    "date_redacted,title,description,image,version,episode_id,sort_key,"
                    "has_data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i,) + tuple(r["vals"]))
    n_ev = sum(1 for r in rows if r["vals"][-1] == 1)

    cur.execute("DELETE FROM uttu_meta")
    meta = {"规范版本": SCHEMA_VERSION, "站点": base,
            "故事线URL": base + STORYLINE_PATH, "时间线URL": base + TIMELINE_PATH,
            "故事线抓取": json.dumps(sl_meta, ensure_ascii=False),
            "时间线抓取": json.dumps(tl_meta, ensure_ascii=False),
            "故事线统计": json.dumps(sl["统计"], ensure_ascii=False),
            "时间线统计": json.dumps(tl["统计"], ensure_ascii=False)}
    cur.executemany("INSERT INTO uttu_meta (key,value) VALUES (?,?)", list(meta.items()))
    conn.commit()
    return n_nodes, n_edges, n_ev


def rewrite_timeline(conn):
    """
    可选：用官方图谱顺序覆写 rain1999 的 timeline 表。
    arc_kind 严格限定在 main/character/event/side/other，
    保证 `rain1999.py timeline --kind ...` 与模拟器查询不受影响。
    """
    cur = conn.cursor()
    try:
        rows = cur.execute("SELECT episode_id FROM timeline").fetchall()
    except sqlite3.OperationalError:
        print("  [warn] timeline 表不存在，先运行 rain1999.py crawl", file=sys.stderr)
        return 0
    if not rows:
        print("  [warn] timeline 表为空，跳过覆写", file=sys.stderr)
        return 0

    graph = {r[0]: r for r in cur.execute(
        "SELECT episode_id, arc, arc_kind, global_order, storyline_zh, title "
        "FROM uttu_storyline_node WHERE episode_id != ''")}
    if not graph:
        print("  [warn] 图谱为空，跳过覆写", file=sys.stderr)
        return 0

    base = 1000
    updated = 0
    for eid, in rows:
        g = graph.get(eid)
        if not g:
            continue
        _, arc, kind, gorder, lane_zh, title = g
        phase = f"官方图谱 · {lane_zh}"
        cur.execute("UPDATE timeline SET order_key=?, arc=?, arc_kind=?, phase=?, label=? "
                    "WHERE episode_id=?",
                    (base + int(gorder), arc, kind, phase, "storyline", eid))
        updated += 1
    conn.commit()
    return updated


# =========================================================================== #
# 六、对外主入口（供 rain1999.py 直接 import 调用）
# =========================================================================== #

def collect(base=DEFAULT_BASE, cache_dir=None, from_cache=None, verbose=True):
    """抓取（或读本地 HTML）并解析两页，返回 (storyline, timeline, meta)。"""
    cache_dir = cache_dir or os.path.join(HERE, "rain1999", "data", "_cache")
    opener, limiter = build_opener(), RateLimiter(0.4)

    if from_cache:
        sl_path, tl_path = from_cache
        sl_html = open(sl_path, encoding="utf-8", errors="replace").read()
        tl_html = open(tl_path, encoding="utf-8", errors="replace").read()
        sl_meta = {"url": base + STORYLINE_PATH, "来源": f"本地文件 {os.path.basename(sl_path)}",
                   "字节数": len(sl_html.encode("utf-8")),
                   "抓取时间": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
        tl_meta = {"url": base + TIMELINE_PATH, "来源": f"本地文件 {os.path.basename(tl_path)}",
                   "字节数": len(tl_html.encode("utf-8")),
                   "抓取时间": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    else:
        if verbose:
            print(f"  [uttu] 抓取故事线 {base + STORYLINE_PATH}")
        sl_html, sl_meta = fetch(base + STORYLINE_PATH, cache_dir, opener, limiter)
        if verbose:
            print(f"  [uttu] 抓取时间线 {base + TIMELINE_PATH}")
        tl_html, tl_meta = fetch(base + TIMELINE_PATH, cache_dir, opener, limiter)

    sl, edges = build_storyline(sl_html)
    tl = build_timeline(tl_html)
    sl["_edges_flat"] = edges
    return sl, tl, {"storyline": sl_meta, "timeline": tl_meta}


def export_json(sl, tl, out_dir, meta=None):
    os.makedirs(out_dir, exist_ok=True)
    meta = meta or {}
    paths = {}
    for name, kind, doc in (
        ("重返未来1999_故事线.json", "storyline", sl),
        ("重返未来1999_时间线.json", "timeline", tl),
    ):
        d = dict(doc)
        d.pop("_edges_flat", None)
        payload = {"文档信息": {"类型": kind, "规范版本": SCHEMA_VERSION,
                               "游戏": "重返未来：1999 / Reverse: 1999",
                               "语言": "en（站点源数据为英文）",
                               "对接": "rain1999.py（episodes.id = 板块_slug）/ rain1999_sim.py",
                               "抓取": (meta or {}).get(kind)},
                   "统计": d.get("统计", {}),
                   "数据": {k: v for k, v in d.items() if k not in ("统计", "_edges_flat")}}
        p = os.path.join(out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        paths[kind] = p
    return paths


def sync(db_path=DEFAULT_DB, base=DEFAULT_BASE, cache_dir=None, from_cache=None,
         rewrite=False, json_out=None, verbose=True):
    """
    一站式：抓取 -> 解析 -> 写库（可选覆写 timeline）-> 可选导出 JSON。
    返回统计 dict，供 rain1999.py 的 cmd 调用。
    """
    sl, tl, meta = collect(base, cache_dir, from_cache, verbose)
    if verbose:
        print(f"  [uttu] 故事线：{sl['统计']['故事线数']} 条泳道 / "
              f"{sl['统计']['节点总数']} 节点 / {sl['统计']['链总数']} 链 / "
              f"{sl['统计']['连线总数']} 连线")
        print(f"  [uttu] 时间线：{tl['统计']['年份节点数']} 年份节点 / "
              f"{tl['统计']['事件总数']} 事件 / "
              f"{tl['统计']['可关联剧情的事件数']} 条可关联剧情")

    result = {"storyline": sl["统计"], "timeline": tl["统计"],
              "db": None, "matched": 0, "rewritten": 0, "json": None}

    if json_out:
        result["json"] = export_json(sl, tl, json_out, meta)
        if verbose:
            for k, v in result["json"].items():
                print(f"  [uttu] 导出 {k}: {v}")

    if db_path:
        if not os.path.exists(db_path):
            print(f"  [warn] 剧情库不存在：{db_path}\n"
                  f"         先运行 `python rain1999.py crawl` 生成，"
                  f"或用 --json-only 只导出 JSON。", file=sys.stderr)
        else:
            conn = sqlite3.connect(db_path)
            try:
                install_schema(conn)
                n, e_, ev = write_graph(conn, sl, tl, meta["storyline"],
                                        meta["timeline"], base)
                matched = conn.execute(
                    "SELECT COUNT(*) FROM uttu_storyline_node n JOIN episodes e"
                    " ON e.id = n.episode_id").fetchone()[0]
                total_ep = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                ev_matched = conn.execute(
                    "SELECT COUNT(DISTINCT episode_id) FROM uttu_world_event"
                    " WHERE episode_id != '' AND episode_id IN"
                    " (SELECT id FROM episodes)").fetchone()[0]
                result["db"] = {"path": db_path, "节点": n, "连线": e_, "事件": ev,
                                "剧情库条目数": total_ep}
                result["matched"] = matched
                result["event_matched"] = ev_matched
                if verbose:
                    print(f"  [uttu] 写入库：节点 {n} / 连线 {e_} / 世界史事件 {ev}")
                    print(f"  [uttu] 图谱节点命中剧情库：{matched}/{total_ep} 条")
                    print(f"  [uttu] 时间线关联到已入库剧情：{ev_matched} 条")
                if rewrite:
                    result["rewritten"] = rewrite_timeline(conn)
                    if verbose:
                        print(f"  [uttu] 已按官方图谱覆写 timeline 表："
                              f"{result['rewritten']} 条")
            finally:
                conn.close()
    return result


def show(db_path=DEFAULT_DB, limit=20):
    if not os.path.exists(db_path):
        print(f"剧情库不存在：{db_path}")
        return 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'uttu_%'")]
        if not tables:
            print("尚未写入图谱数据，请先运行： python rain1999_uttu_bridge.py sync")
            return 1
        print(f"库：{db_path}")
        for k, v in conn.execute("SELECT key,value FROM uttu_meta ORDER BY key"):
            if k.endswith("统计"):
                print(f"  {k}: {v}")
        print("\n— 官方剧情顺序（图谱 JOIN 剧情库）—")
        for r in conn.execute(
            """SELECT n.global_order, n.storyline_zh, n.node_id, n.episode_id,
                      e.title_zh, e.line_count
               FROM uttu_storyline_node n LEFT JOIN episodes e ON e.id = n.episode_id
               ORDER BY n.global_order LIMIT ?""", (limit,)):
            hit = r["title_zh"] or "（剧情库中暂无）"
            print(f'  {r["global_order"]:>3} 【{r["storyline_zh"]}】{r["node_id"]:<8} '
                  f'{r["episode_id"]:<22} {hit}  行数={r["line_count"] or 0}')
        print("\n— 世界史时间轴（前 %d 条）—" % limit)
        for r in conn.execute(
            """SELECT axis_order, era, year_raw, iso_date, date_precision,
                      title, episode_id FROM uttu_world_event
               WHERE has_data=1 ORDER BY axis_order LIMIT ?""", (limit,)):
            flag = "·抹除" if r["date_precision"] == "redacted" else ""
            print(f'  {r["axis_order"]:>3} [{r["era"]}] {r["year_raw"]:<7} '
                  f'{r["iso_date"] or "?":<10} {flag} {r["title"][:44]}'
                  + (f'  -> {r["episode_id"]}' if r["episode_id"] else ""))
        print("\n— 跨线关系 —")
        for r in conn.execute(
            """SELECT relation, a_episode_id, b_episode_id, label
               FROM uttu_storyline_edge ORDER BY relation, id"""):
            print(f'  {r["relation"]:<14} {r["a_episode_id"] or "-":<22} -> '
                  f'{r["b_episode_id"] or "-":<22} {r["label"] or ""}')
    finally:
        conn.close()
    return 0


# =========================================================================== #
# 七、命令行
# =========================================================================== #

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="rain1999_uttu_bridge",
        description="把 UTTU 故事线/时间线图谱并入 rain1999 剧情库")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("sync", help="抓取并写入剧情库（默认动作）")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 库路径（默认 {DEFAULT_DB}）")
    p.add_argument("--base", default=DEFAULT_BASE, help="站点根地址")
    p.add_argument("--cache-dir", default=None, help="HTML 缓存目录")
    p.add_argument("--rewrite-timeline", action="store_true",
                   help="用官方图谱顺序覆写 timeline 表（影响模拟器叙事顺序）")
    p.add_argument("--json-only", action="store_true", help="只导出 JSON，不写库")
    p.add_argument("--out", default=None, help="JSON 导出目录")
    p.add_argument("--from-cache", nargs=2, metavar=("STORYLINE_HTML", "TIMELINE_HTML"),
                   help="离线：用本地已下载的 HTML 解析")

    q = sub.add_parser("show", help="查看库中图谱数据")
    q.add_argument("--db", default=DEFAULT_DB)
    q.add_argument("--limit", type=int, default=20)

    args = ap.parse_args(argv if argv is not None else
                         (sys.argv[1:] or ["sync"]))
    if args.cmd == "show":
        return show(args.db, args.limit)

    db = None if args.json_only else args.db
    out = args.out or (os.path.join(HERE, "rain1999", "data", "graph")
                       if args.json_only else None)
    r = sync(db_path=db, base=args.base, cache_dir=args.cache_dir,
             from_cache=args.from_cache, rewrite=args.rewrite_timeline,
             json_out=out)
    print("\n完成。", json.dumps({k: v for k, v in r.items() if k in
                                  ("matched", "event_matched", "rewritten")},
                                 ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
