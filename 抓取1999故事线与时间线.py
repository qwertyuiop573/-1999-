#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《重返未来：1999》资料站 —— 故事线 / 时间线 抓取与结构化脚本
================================================================

数据源（uttu.merui.net，Astro 静态站，服务端直出 HTML）：
  1) 故事线 Storyline : https://uttu.merui.net/storyline/
  2) 时间线 Timeline  : https://uttu.merui.net/timeline/

页面数据藏在哪里（逆向结论，脚本按此解析）：
  · 故事线
      - 4 条泳道：div[data-lane][data-storyline]（main / events / side-stories / unknown-line）
      - 泳道内若干条链：.node-chain，链内顺序即剧情推进顺序
      - 节点：a.storyline-node，携带 data-node(节点ID) / data-storyline(所属线) / href / 标题 / 封面图
      - 连线关系：<script id="storyline-edges" type="application/json"> 内嵌 JSON
                （storylines 泳道元信息、nodeLinks 汇入主线、nodeOuts 汇出、sameTime 同时发生/闪回）
  · 时间线
      - 48 个年份节点：button.year-node
      - 每个节点的全部事件都在属性 data-dates 里（HTML 实体转义的 JSON 字符串）
      - data-year = 世界观年份；data-year-index = 全局序号；.node-label = 叙事时段（风暴后才有）
      - 纵向列表 #vertical-timeline-content 是前端点击后用 data-dates 渲染的，HTML 里为空
        —— 所以只解析 data-dates 即可拿到全量数据，无需执行 JS。
      - 部分节点的 data-dates 是模板占位值 [{"date":"Date","events":[{"title":"Event 1"}]}]
        代表站点尚未填充，脚本标记 has_data=false，不伪造内容。

语言说明：
  该站的简体中文是前端调用 Google 翻译实时生成的（?lang=chs 服务端仍返回英文），
  因此英文 HTML 是唯一可靠的源数据。本脚本抓取英文原文，字段名与说明为中文。

时间规划（本脚本的核心处理）：
  游戏设定中「风暴」使时间回溯，因此时间线分两段，不能简单按年份排序：
    · 风暴前 (pre_storm)  : year_index 0~30，年份 1770s → 1999 正序，1999 即「风暴」
    · 风暴后 (post_storm) : year_index 31~47，data-year 是回溯到的旧年份（1996、1985…），
                            真正的叙事先后顺序由 .node-label（2000、2000~2001…）决定
  脚本为每个节点/每条事件计算统一的排序键 sort_key，并归一化出：
    year_start / year_end / year_precision / iso_date / date_redacted / narrative_period
  这样任何下游程序都能得到一个确定的、可直接排序的时间轴。

用法：
    python3 抓取1999故事线与时间线.py                # 抓取两页，输出两个 JSON
    python3 抓取1999故事线与时间线.py --only timeline # 只抓时间线
    python3 抓取1999故事线与时间线.py --out-dir 输出目录
    python3 抓取1999故事线与时间线.py --from-cache storyline.html timeline.html
                                                     # 用本地已下载的 HTML 解析（离线调试）

依赖：requests、beautifulsoup4、lxml
    pip install requests beautifulsoup4 lxml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:  # 允许 --from-cache 模式下无 requests 也能跑
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("缺少依赖 beautifulsoup4，请先执行： pip install beautifulsoup4 lxml")


# --------------------------------------------------------------------------- #
# 常量配置
# --------------------------------------------------------------------------- #

STORYLINE_URL = "https://uttu.merui.net/storyline/"
TIMELINE_URL = "https://uttu.merui.net/timeline/"
SITE_ROOT = "https://uttu.merui.net"

SCHEMA_VERSION = "1.0"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}

# 站点未填充内容时使用的模板占位值，需识别为「无数据」
TIMELINE_PLACEHOLDER = '[{"date":"Date","events":[{"title":"Event 1"}]}]'

# 抓取节奏控制：礼貌抓取，避免给站点压力
REQUEST_TIMEOUT = 30
RETRY_TIMES = 3
RETRY_BACKOFF = 2.0        # 秒，指数退避基数
DELAY_BETWEEN_PAGES = 1.0  # 两个页面之间的间隔

# 泳道 ID → 中文名
LANE_NAME_ZH = {
    "main": "主线剧情",
    "events": "活动故事",
    "side-stories": "支线故事",
    "unknown-line": "归属待定",
}

# 风暴节点：index 30 是 1999「风暴」，此后为回溯段
STORM_YEAR_INDEX = 30


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    """当前 UTC 时间，ISO 8601。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_soup(html_text: str) -> BeautifulSoup:
    """优先 lxml，失败回退 html.parser。"""
    try:
        return BeautifulSoup(html_text, "lxml")
    except Exception:
        return BeautifulSoup(html_text, "html.parser")


def clean_text(value: Any) -> str:
    """
    清洗富文本：去掉 <nobr> 等标签、还原实体、压缩空白。
    保留段落换行（原文用 \n\n 分段），但不保留 HTML。
    """
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<\s*nobr\s*>", "", text, flags=re.I)
    text = re.sub(r"<\s*/\s*nobr\s*>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)          # 兜底：剥掉其余标签
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_story_href(href: str | None) -> tuple[str, str]:
    """
    把详情页路径拆成 (板块, slug)，用于与 rain1999.py 下载器对接。

    /story/en/main/chapter-0      -> ("main", "chapter-0")
    /story/en/event/1_6           -> ("event", "1_6")
    /story/en/other/sos           -> ("other", "sos")
    /story/chs/main/trails/800260x-> ("trails", "800260x")
    /story/en/character/lucy      -> ("character", "lucy")

    rain1999.py 的 episodes.id 规则是 f"{section}_{slug}"，
    因此 (板块, slug) 就是两份数据之间唯一可靠的联合键。
    """
    if not href:
        return "", ""
    m = re.search(r"/story/(?:en|chs|jp|kr|cht)/([a-zA-Z0-9_.\-]+)/(.+?)/?$", href.strip())
    if not m:
        return "", ""
    section, rest = m.group(1), m.group(2)
    # 形如 main/trails/800260x：真正的板块是 trails
    if "/" in rest:
        section, rest = rest.rsplit("/", 1)[0].split("/")[-1], rest.rsplit("/", 1)[1]
    return section, rest


def rain1999_episode_id(section: str, slug: str) -> str:
    """生成 rain1999.py 的 episodes.id（下载器与模拟器共用的主键）。"""
    if not section or not slug:
        return ""
    return f"{section}_{slug}"


def absolute_url(href: str | None) -> str:
    """把 /story/en/main/chapter-0 这类相对路径补成绝对 URL。"""
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return SITE_ROOT + (href if href.startswith("/") else "/" + href)


def fetch_page(url: str, *, cache_dir: Path | None = None,
               use_cache: bool = False) -> tuple[str, dict]:
    """
    下载页面 HTML，带重试与可选本地缓存。
    返回 (html文本, 抓取元信息)。
    """
    meta: dict[str, Any] = {
        "url": url,
        "fetched_at": None,
        "http_status": None,
        "bytes": None,
        "source": None,
        "cache_file": None,
    }

    cache_file = None
    if cache_dir is not None:
        key = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        name = re.sub(r"\W+", "_", url.replace("https://", "").replace("http://", ""))[:40]
        cache_file = cache_dir / f"cache_{name}_{key}.html"

    if use_cache and cache_file and cache_file.exists():
        html_text = cache_file.read_text(encoding="utf-8", errors="replace")
        meta.update(source="本地缓存", bytes=len(html_text.encode("utf-8")),
                    cache_file=str(cache_file), fetched_at=now_iso())
        print(f"  [缓存] {url}  ({meta['bytes']} 字节)")
        return html_text, meta

    if requests is None:
        raise RuntimeError("未安装 requests，无法联网抓取。pip install requests，"
                           "或改用 --from-cache 提供本地 HTML。")

    last_err: Exception | None = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            html_text = resp.text
            meta.update(source="网络抓取", http_status=resp.status_code,
                        bytes=len(html_text.encode("utf-8")), fetched_at=now_iso())
            if cache_file is not None:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(html_text, encoding="utf-8")
                meta["cache_file"] = str(cache_file)
            print(f"  [抓取] {url}  HTTP {resp.status_code}  {meta['bytes']} 字节"
                  f"（第 {attempt} 次尝试）")
            return html_text, meta
        except Exception as exc:  # 网络/HTTP 错误统一重试
            last_err = exc
            wait = RETRY_BACKOFF * attempt
            print(f"  [重试] 第 {attempt}/{RETRY_TIMES} 次失败：{exc}"
                  + (f"，{wait:.0f} 秒后重试" if attempt < RETRY_TIMES else ""),
                  file=sys.stderr)
            if attempt < RETRY_TIMES:
                time.sleep(wait)

    raise RuntimeError(f"抓取失败（已重试 {RETRY_TIMES} 次）：{url} -> {last_err}")


# --------------------------------------------------------------------------- #
# 时间归一化：把站点的各种「日期写法」统一成可排序结构
# --------------------------------------------------------------------------- #

REDACTED_RE = re.compile(r"[█▓▒×xX*]{2,}")           # ██.██ 这类被抹掉的日期
YEAR_RE = re.compile(r"^(\d{4})$")
DECADE_RE = re.compile(r"^(\d{3})0s$", re.I)
MONTH_DAY_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})$")            # 08.02
MONTH_DAY_TIME_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})\s+(\d{1,2}):(\d{2})$")  # 12.31 23:59
MONTH_REDACTED_RE = re.compile(r"^(\d{1,2})[.\-/]\s*[█▓▒×xX*]{1,}$")   # 05.██
DAY_REDACTED_RE = re.compile(r"^[█▓▒×xX*]{1,}[.\-/]\s*(\d{1,2})$")      # ██.05
ISO_IN_TEXT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")    # 正文里出现的 1831-08-02
PERIOD_RE = re.compile(r"(\d{4})\s*(?:~|-|–|至)\s*(\d{4})")
PERIOD_OPEN_RE = re.compile(r"(\d{4})\s*(?:~|-|–)\s*$")


@dataclass
class NormalizedTime:
    """归一化后的时间描述，字段全部可空，缺失即留空不猜测。"""
    raw_year: str = ""            # 站点原始 data-year，如 "1770s" / "1831"
    raw_date: str = ""            # 事件原始 date，如 "08.02" / "██.██"
    year_start: int | None = None
    year_end: int | None = None
    month: int | None = None
    day: int | None = None
    year_precision: str = ""      # exact_year | decade | unknown
    date_precision: str = ""      # day | month | year | decade | redacted | narrative | unknown
    iso_date: str = ""            # 能确定到日/月/年时给出，如 1831-08-02 / 1831-08 / 1831
    time_of_day: str = ""         # 时刻，如 "23:59"
    date_redacted: bool = False   # 日期是否被官方完全抹去（██.██）
    date_partial_redacted: bool = False  # 是否部分抹除（05.██ / ██.05）
    narrative_period: str = ""    # 风暴后节点的叙事时段标签，如 "2000~2001"
    sort_key: tuple = ()          # 用于全局排序的键

    def to_dict(self) -> dict:
        return {
            "原始年份": self.raw_year,
            "原始日期": self.raw_date,
            "起始年": self.year_start,
            "结束年": self.year_end,
            "月": self.month,
            "日": self.day,
            "年份精度": self.year_precision,
            "日期精度": self.date_precision,
            "标准日期": self.iso_date,
            "时刻": self.time_of_day,
            "日期已抹除": self.date_redacted,
            "日期部分抹除": self.date_partial_redacted,
            "叙事时段": self.narrative_period,
            "排序键": list(self.sort_key),
        }


def _parse_year(raw_year: str) -> tuple[int | None, int | None, str]:
    """解析 data-year：'1831' -> (1831,1831,exact_year)；'1770s' -> (1770,1779,decade)。"""
    y = (raw_year or "").strip()
    m = YEAR_RE.match(y)
    if m:
        v = int(m.group(1))
        return v, v, "exact_year"
    m = DECADE_RE.match(y)
    if m:
        v = int(m.group(1) + "0")          # "1770s" -> 1770，区间 1770~1779
        return v, v + 9, "decade"
    # 形如 "1770's"、"Early 1800s" 等：抓第一个四位年份
    m = re.search(r"(\d{4})", y)
    if m:
        v = int(m.group(1))
        if "s" in y.lower():
            return v, v + 9, "decade"
        return v, v, "exact_year"
    return None, None, "unknown"


def _parse_period_label(label: str) -> tuple[int | None, int | None]:
    """解析 .node-label 的叙事时段：'2000~2001' -> (2000,2001)；'2008~' -> (2008,None)。"""
    label = (label or "").strip()
    if not label:
        return None, None
    m = PERIOD_RE.search(label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = PERIOD_OPEN_RE.search(label)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d{4})", label)
    if m:
        return int(m.group(1)), None
    return None, None


def normalize_time(*, raw_year: str, raw_date: str, description: str = "",
                   year_index: int | None = None,
                   narrative_label: str = "") -> NormalizedTime:
    """
    把一个年份节点 + 一条事件的日期信息，归一化成 NormalizedTime。

    规则（保守优先，宁缺勿造）：
      1. 年份来自 data-year；日期来自事件 date 字段。
      2. date 为 ██.██ / 全遮蔽 -> date_redacted=True, date_precision="redacted"，
         标准日期只保留年份部分（若年份可确定）。
      3. date 为 MM.DD -> 与年份拼成 YYYY-MM-DD，date_precision="day"。
      4. date 为自然语言（如 "Coming into the era"）-> date_precision="narrative"。
      5. date 为空但正文里出现 YYYY-MM-DD -> 取正文首个作为回退，标记精度为 day。
      6. 排序键 = (时代, 全局序号, 年, 月, 日)，保证风暴前/风暴后不会混排。
    """
    nt = NormalizedTime(raw_year=(raw_year or "").strip(),
                        raw_date=(raw_date or "").strip())

    nt.year_start, nt.year_end, nt.year_precision = _parse_year(nt.raw_year)
    nt.narrative_period = (narrative_label or "").strip()

    # ---- 日期精度判定（覆盖站点全部 6 类写法）---- #
    d = nt.raw_date
    fully_redacted = bool(d) and bool(REDACTED_RE.search(d)) and not re.search(r"\d", d)

    if fully_redacted:
        # ██.██：官方完全抹除，只保留年份信息，不猜测具体日期
        nt.date_redacted = True
        nt.date_precision = "redacted"
        nt.iso_date = str(nt.year_start) if nt.year_start else ""

    else:
        m = MONTH_DAY_TIME_RE.match(d)
        if m and nt.year_start:
            # 12.31 23:59
            nt.month, nt.day = int(m.group(1)), int(m.group(2))
            nt.time_of_day = f"{int(m.group(3)):02d}:{m.group(4)}"
            nt.date_precision = "day"
            nt.iso_date = f"{nt.year_start:04d}-{nt.month:02d}-{nt.day:02d}"

        elif (m := MONTH_DAY_RE.match(d)) and nt.year_start:
            # 08.02
            nt.month, nt.day = int(m.group(1)), int(m.group(2))
            nt.date_precision = "day"
            nt.iso_date = f"{nt.year_start:04d}-{nt.month:02d}-{nt.day:02d}"

        elif (m := MONTH_REDACTED_RE.match(d)):
            # 05.██：月份已知，日期被抹除
            nt.month = int(m.group(1))
            nt.date_partial_redacted = True
            nt.date_precision = "month"
            nt.iso_date = f"{nt.year_start:04d}-{nt.month:02d}" if nt.year_start else ""

        elif (m := DAY_REDACTED_RE.match(d)):
            # ██.05：日期已知，月份被抹除
            nt.day = int(m.group(1))
            nt.date_partial_redacted = True
            nt.date_precision = "year"
            nt.iso_date = str(nt.year_start) if nt.year_start else ""

        elif re.match(r"^\d{1,2}$", d):
            # 仅有月份数字
            nt.month = int(d)
            nt.date_precision = "month"
            nt.iso_date = f"{nt.year_start:04d}-{nt.month:02d}" if nt.year_start else ""

        elif d == "":
            # date 为空：回退到正文里的 YYYY-MM-DD（如 1831-08-02）
            m2 = ISO_IN_TEXT_RE.search(description or "")
            if m2:
                y, mo, da = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
                if nt.year_start in (None, y):
                    nt.year_start, nt.year_end = y, y
                    nt.year_precision = "exact_year"
                nt.month, nt.day = mo, da
                nt.date_precision = "day"
                nt.iso_date = f"{y:04d}-{mo:02d}-{da:02d}"
            else:
                nt.date_precision = ("decade" if nt.year_precision == "decade"
                                     else ("year" if nt.year_start else "unknown"))
                if nt.year_start:
                    nt.iso_date = str(nt.year_start)

        else:
            # 自然语言日期，如 "Coming into the era" / "Events with unclear years"
            nt.date_precision = "narrative"
            nt.iso_date = str(nt.year_start) if nt.year_start else ""

    # ---- 排序键：时代优先，其次站点全局序号，再按归一化年月日 ---- #
    era = 0
    if year_index is not None:
        era = 0 if year_index <= STORM_YEAR_INDEX else 1
    nt.sort_key = (
        era,
        year_index if year_index is not None else 0,
        nt.year_start if nt.year_start is not None else 0,
        nt.month if nt.month is not None else 0,
        nt.day if nt.day is not None else 0,
    )
    return nt


# --------------------------------------------------------------------------- #
# 故事线解析
# --------------------------------------------------------------------------- #

def parse_storyline(html_text: str) -> dict:
    """
    解析故事线页面。
    产出：泳道(lanes) -> 链(chains，链内有序) -> 节点(nodes)；外加连线关系(edges)。
    """
    soup = make_soup(html_text)

    # ---- 内嵌连线 JSON ---- #
    edges_raw: dict = {}
    tag = soup.select_one("#storyline-edges")
    if tag:
        try:
            edges_raw = json.loads(tag.get_text())
        except json.JSONDecodeError as exc:
            print(f"  [警告] storyline-edges JSON 解析失败：{exc}", file=sys.stderr)

    lane_meta = {s.get("id"): s for s in edges_raw.get("storylines", [])}

    # ---- 泳道 / 链 / 节点（单次遍历，链内顺序即剧情推进顺序）---- #
    lanes: list[dict] = []
    total_nodes = 0
    node_key_map: dict[tuple[str, str], str] = {}   # (故事线, 节点ID) -> 剧情库ID

    for lane_el in soup.select("[data-lane][data-storyline]"):
        lane_id = lane_el.get("data-storyline") or ""
        lane_no_raw = lane_el.get("data-lane") or "0"
        lane_no = int(lane_no_raw) if lane_no_raw.isdigit() else 0
        meta = lane_meta.get(lane_id, {})

        chains_out: list[dict] = []
        lane_node_count = 0

        for chain_seq, chain_el in enumerate(lane_el.select(".node-chain")):
            chain_ids: list[str] = []
            chain_nodes: list[dict] = []

            for node_seq, a in enumerate(chain_el.select("a.storyline-node")):
                node_id = a.get("data-node") or ""
                if not node_id:
                    continue
                titles = a.select(".node-title")
                title = clean_text(titles[0].get_text()) if titles else ""

                # 与 rain1999.py 对接的联合键：板块 + slug -> episode_id
                section, slug = split_story_href(a.get("href"))

                node = {
                    "节点ID": node_id,
                    "标题": title,
                    "所属故事线": lane_id,
                    "所属故事线名称": LANE_NAME_ZH.get(lane_id, lane_id),
                    "链序号": chain_seq,
                    "链内顺序": node_seq,
                    "全局顺序": total_nodes,
                    # ---- rain1999 对接字段 ---- #
                    "板块": section,                                  # main/event/other/...
                    "slug": slug,                                     # chapter-0 / 1_6 / sos
                    "剧情库ID": rain1999_episode_id(section, slug),    # main_chapter-0
                }
                node_key_map[(lane_id, node_id)] = node["剧情库ID"]
                chain_ids.append(node_id)
                chain_nodes.append(node)
                total_nodes += 1

            lane_node_count += len(chain_ids)
            chains_out.append({
                "链序号": chain_seq,
                "节点数": len(chain_ids),
                "节点顺序": chain_ids,
                "节点": chain_nodes,
            })

        lanes.append({
            "故事线ID": lane_id,
            "故事线名称": LANE_NAME_ZH.get(lane_id, lane_id),
            "标题原文": (lane_el.get("title") or "").strip(),
            "泳道编号": lane_no,
            "是否主线": bool(meta.get("isMain", lane_id == "main")),
            "是否与主线断开": bool(meta.get("disconnected", False)),
            "节点数": lane_node_count,
            "链数": len(chains_out),
            "链": chains_out,
        })

    # ---- 连线关系（中文化，同时保留原始键名便于回溯） ---- #
    def ref(d: dict | None) -> dict:
        """把站点的 {storyline,node,into} 转成中文键，并补上 rain1999 联合键。"""
        if not d:
            return {}
        sl, nid = d.get("storyline", ""), d.get("node", "")
        return {
            "故事线": sl,
            "节点": nid,
            "剧情库ID": node_key_map.get((sl, nid), ""),
            "接入方向": d.get("into"),
        }

    node_links = [{"来源": ref(e.get("from")), "汇入": ref(e.get("connectsFrom")),
                   "原始": e} for e in edges_raw.get("nodeLinks", [])]
    node_outs = [{"来源": ref(e.get("from")), "汇出至": ref(e.get("connectsTo")),
                  "原始": e} for e in edges_raw.get("nodeOuts", [])]
    same_time = [{
        "节点A": ref(e.get("a")),
        "节点B": ref(e.get("b")),
        "是否有连线": bool(e.get("connected", False)),
        "标签": e.get("label", ""),
        "B侧标签": e.get("bLabel", ""),
        "原始": e,
    } for e in edges_raw.get("sameTime", [])]

    return {
        "泳道": lanes,
        "连线关系": {
            "汇入主线": node_links,
            "汇出连接": node_outs,
            "同时发生或闪回": same_time,
        },
        "统计": {
            "故事线数": len(lanes),
            "节点总数": total_nodes,
            "链总数": sum(l["链数"] for l in lanes),
            "汇入主线连线数": len(node_links),
            "汇出连线数": len(node_outs),
            "同时发生关系数": len(same_time),
            "可对接剧情库的节点数": sum(
                1 for v in node_key_map.values() if v),
        },
    }


# --------------------------------------------------------------------------- #
# 时间线解析
# --------------------------------------------------------------------------- #

def parse_timeline(html_text: str) -> dict:
    """
    解析时间线页面：48 个年份节点 -> 每个节点下若干日期分组 -> 每组若干事件。
    同时完成时间归一化与全局排序。
    """
    soup = make_soup(html_text)
    year_nodes: list[dict] = []
    flat_events: list[dict] = []

    for btn in soup.select("button.year-node"):
        raw_year = (btn.get("data-year") or "").strip()
        raw_index = btn.get("data-year-index")
        year_index = int(raw_index) if raw_index is not None and raw_index.lstrip("-").isdigit() else None
        raw_dates = btn.get("data-dates") or ""

        label_el = btn.select_one(".node-label")
        narrative_label = clean_text(label_el.get_text()) if label_el else ""
        year_text_el = btn.select_one(".node-text")
        year_display = clean_text(year_text_el.get_text()) if year_text_el else raw_year

        is_placeholder = raw_dates.strip() == TIMELINE_PLACEHOLDER
        groups: list[dict] = []
        if raw_dates and not is_placeholder:
            try:
                groups = json.loads(raw_dates)
            except json.JSONDecodeError as exc:
                print(f"  [警告] {raw_year} 的 data-dates 解析失败：{exc}", file=sys.stderr)

        era = "风暴前" if (year_index is not None and year_index <= STORM_YEAR_INDEX) else "风暴后"
        era_en = "pre_storm" if era == "风暴前" else "post_storm"

        parsed_groups: list[dict] = []
        for gi, g in enumerate(groups if isinstance(groups, list) else []):
            if not isinstance(g, dict):
                continue
            raw_date = clean_text(g.get("date", ""))
            events_out: list[dict] = []
            for ei, ev in enumerate(g.get("events", []) or []):
                if not isinstance(ev, dict):
                    continue
                desc = clean_text(ev.get("description", ""))
                nt = normalize_time(
                    raw_year=raw_year, raw_date=raw_date, description=desc,
                    year_index=year_index, narrative_label=narrative_label,
                )
                link_section, link_slug = split_story_href(ev.get("link", ""))
                event = {
                    "标题": clean_text(ev.get("title", "")),
                    "描述": desc,
                    "配图": (ev.get("image") or "").strip(),
                    "关联链接": absolute_url(ev.get("link", "")),
                    # ---- rain1999 对接字段：指向对应剧情条目 ---- #
                    "关联板块": link_section,
                    "关联slug": link_slug,
                    "关联剧情ID": rain1999_episode_id(link_section, link_slug),
                    "所属版本": (ev.get("version") or "").strip(),
                    "日期分组序号": gi,
                    "组内序号": ei,
                    "时间": nt.to_dict(),
                }
                events_out.append(event)
                flat_events.append({
                    "年份": raw_year,
                    "年份序号": year_index,
                    "时代": era,
                    "叙事时段": narrative_label,
                    "标题": event["标题"],
                    "标准日期": nt.iso_date,
                    "日期精度": nt.date_precision,
                    "日期已抹除": nt.date_redacted,
                    "关联剧情ID": event["关联剧情ID"],
                    "_sort": nt.sort_key,
                })
            parsed_groups.append({
                "原始日期": raw_date,
                "日期序号": gi,
                "事件数": len(events_out),
                "事件": events_out,
            })

        year_nodes.append({
            "年份序号": year_index,
            "年份原文": raw_year,
            "年份显示": year_display,
            "时代": era,
            "时代标识": era_en,
            "叙事时段": narrative_label,
            "叙事时段起始年": _parse_period_label(narrative_label)[0],
            "叙事时段结束年": _parse_period_label(narrative_label)[1],
            "是否有数据": (not is_placeholder) and bool(parsed_groups),
            "数据状态": ("站点未填充" if is_placeholder
                         else ("已填充" if parsed_groups else "解析为空")),
            "日期分组数": len(parsed_groups),
            "事件数": sum(g["事件数"] for g in parsed_groups),
            "日期分组": parsed_groups,
        })

    # 全局时间轴：按归一化排序键排序（风暴前正序 -> 风暴后按站点序号）
    flat_events.sort(key=lambda e: e["_sort"])
    for i, e in enumerate(flat_events):
        e["时间轴序号"] = i + 1
        e.pop("_sort", None)

    filled = [n for n in year_nodes if n["是否有数据"]]
    return {
        "时间轴说明": {
            "排序规则": "先按时代（风暴前 → 风暴后），再按站点年份序号，最后按归一化年月日",
            "风暴节点": f"年份序号 {STORM_YEAR_INDEX}（1999，标签 The Storm）",
            "风暴后语义": ("data-year 是回溯抵达的旧年份，"
                          "叙事先后由『叙事时段』(node-label, 如 2000~2001) 决定"),
            "日期已抹除": "██.██ 为官方刻意隐去的日期，保留标记不猜测具体值",
        },
        "年份节点": year_nodes,
        "全局事件时间轴": flat_events,
        "统计": {
            "年份节点数": len(year_nodes),
            "已填充节点数": len(filled),
            "未填充节点数": len(year_nodes) - len(filled),
            "事件总数": len(flat_events),
            "风暴前节点数": sum(1 for n in year_nodes if n["时代"] == "风暴前"),
            "风暴后节点数": sum(1 for n in year_nodes if n["时代"] == "风暴后"),
        },
    }


# --------------------------------------------------------------------------- #
# 输出封装
# --------------------------------------------------------------------------- #

def build_document(kind: str, title: str, source_url: str,
                   fetch_meta: dict, payload: dict) -> dict:
    """统一信封结构：元信息 + 数据。"""
    return {
        "文档信息": {
            "标题": title,
            "类型": kind,                       # storyline | timeline
            "规范版本": SCHEMA_VERSION,
            "数据来源": source_url,
            "站点": "uttu.merui.net（《重返未来：1999》玩家资料站）",
            "游戏": "重返未来：1999 / Reverse: 1999",
            "抓取时间": fetch_meta.get("fetched_at"),
            "抓取方式": fetch_meta.get("source"),
            "HTTP状态": fetch_meta.get("http_status"),
            "页面字节数": fetch_meta.get("bytes"),
            "语言": "en（站点源数据为英文，简体中文由前端实时翻译生成，无独立中文数据源）",
            "说明": "本文件由脚本自动抓取解析生成，未填充内容以『数据状态』字段标记，不做推测补全。",
        },
        "统计": payload.get("统计", {}),
        "数据": {k: v for k, v in payload.items() if k != "统计"},
    }


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    size = path.stat().st_size
    print(f"  [输出] {path}  ({size/1024:.1f} KB)")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description="抓取并结构化《重返未来：1999》故事线与时间线，输出标准 JSON。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out-dir", default=".", help="JSON 输出目录（默认当前目录）")
    ap.add_argument("--only", choices=["storyline", "timeline"],
                    help="只处理其中一个页面")
    ap.add_argument("--cache-dir", default=".cache_1999",
                    help="HTML 缓存目录（默认 .cache_1999）")
    ap.add_argument("--use-cache", action="store_true",
                    help="若缓存存在则直接使用，不联网")
    ap.add_argument("--from-cache", nargs=2, metavar=("STORYLINE_HTML", "TIMELINE_HTML"),
                    help="直接用本地已下载的 HTML 文件解析（离线模式，两个路径都要给）")
    ap.add_argument("--quiet", action="store_true", help="只输出结果路径，不打印进度")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = {
        "storyline": (STORYLINE_URL, "《重返未来：1999》故事线 Storyline",
                      out_dir / "重返未来1999_故事线.json", parse_storyline),
        "timeline": (TIMELINE_URL, "《重返未来：1999》时间线 Timeline",
                     out_dir / "重返未来1999_时间线.json", parse_timeline),
    }
    if args.only:
        tasks = {args.only: tasks[args.only]}

    offline_files = args.from_cache
    results: dict[str, Path] = {}

    for idx, (kind, (url, title, out_path, parser)) in enumerate(tasks.items()):
        if not args.quiet:
            print(f"\n[{idx+1}/{len(tasks)}] 处理 {kind}：{url}")

        if offline_files:
            file_idx = 0 if kind == "storyline" else 1
            p = Path(offline_files[file_idx])
            if not p.exists():
                print(f"  [错误] 找不到本地文件：{p}", file=sys.stderr)
                return 1
            html_text = p.read_text(encoding="utf-8", errors="replace")
            meta = {"url": url, "fetched_at": now_iso(), "http_status": None,
                    "bytes": len(html_text.encode("utf-8")),
                    "source": f"本地文件 {p.name}", "cache_file": str(p)}
            print(f"  [本地] {p}  ({meta['bytes']} 字节)")
        else:
            html_text, meta = fetch_page(url, cache_dir=cache_dir,
                                         use_cache=args.use_cache)

        payload = parser(html_text)
        doc = build_document(kind, title, url, meta, payload)
        write_json(out_path, doc)
        results[kind] = out_path

        st = doc["统计"]
        if kind == "storyline":
            print(f"  [校验] 故事线 {st.get('故事线数')} 条 / 节点 {st.get('节点总数')} 个"
                  f" / 链 {st.get('链总数')} 条")
        else:
            print(f"  [校验] 年份节点 {st.get('年份节点数')} 个（已填充 "
                  f"{st.get('已填充节点数')}，未填充 {st.get('未填充节点数')}）"
                  f" / 事件 {st.get('事件总数')} 条")

        if idx + 1 < len(tasks) and not offline_files:
            time.sleep(DELAY_BETWEEN_PAGES)   # 礼貌抓取间隔

    print("\n完成，输出文件：")
    for kind, p in results.items():
        print(f"MEDIA:{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
