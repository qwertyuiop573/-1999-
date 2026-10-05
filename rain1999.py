#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rain1999 —— 雨幕档案《重返未来：1999》剧情一体化工具

抓取源：https://uttu.merui.net
  · 固定请求 /story/chs/<板块>/<slug>/transcript/ 直接取简体中文正文
  · 正文以 <script type="text/plain" id="chaptersData"> 内嵌 JSON 提供
  · 板块：main / event / character / anecdote / other
  · 板块页解析失败时回退到内置 slug 清单

功能：
  · 抓取（默认 15 并发 + 0.05s 间隔；磁盘缓存）
  · SQLite 数据库：episodes + lines + timeline
  · 每条剧情导出独立 TXT，文件夹名 = 该条目的站点分类（动态）
  · 时间线：阶段 + 时间标签 + 篇章自动标注
  · 播放接口：StoryDB / StoryPlayer
  · 一键启动模拟器（与同目录的 rain1999_sim.py 联动）
  · 并入 UTTU 官方故事线/时间线图谱（uttu 子命令，桥接 rain1999_uttu_bridge.py）

目录约定（自动锚定到脚本所在目录）：
    <脚本目录>/
    ├── rain1999.py
    ├── rain1999_sim.py          ← 模拟器（可选，与 sim/auto 联动）
    └── rain1999/data/
        ├── rain1999_story.db
        ├── _cache/
        └── story_txt/

用法：
    # 一键：没库先抓、有库直接进模拟器
    python rain1999.py auto

    # 直接启动模拟器（要求数据库已存在）
    python rain1999.py sim

    # 抓取（默认写入 <脚本目录>/rain1999/data/）
    python rain1999.py
    python rain1999.py --limit 20

    # 指定其它目录
    python rain1999.py crawl --out ./data

    # 查询 / 播放
    python rain1999.py stats      --db ./rain1999/data/rain1999_story.db
    python rain1999.py chapters   --db ./rain1999/data/rain1999_story.db
    python rain1999.py episodes   --db ./rain1999/data/rain1999_story.db --chapter "第一章 芝加哥打字机"
    python rain1999.py play       --db ./rain1999/data/rain1999_story.db --id 100001
    python rain1999.py search     --db ./rain1999/data/rain1999_story.db --kw 深海
    python rain1999.py speakers   --db ./rain1999/data/rain1999_story.db

    # 并入官方故事线 / 时间线图谱（供模拟器使用）
    python rain1999.py uttu       --db rain1999_story.db
    python rain1999.py uttu       --db rain1999_story.db --rewrite-timeline
    python rain1999.py uttu-show  --db rain1999_story.db

    # 时间线
    python rain1999.py timeline   --db ./rain1999/data/rain1999_story.db
    python rain1999.py timeline   --db ./rain1999/data/rain1999_story.db --kind main
    python rain1999.py retimeline --db ./rain1999/data/rain1999_story.db
"""

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_BASE = "https://uttu.merui.net"

# UTTU 板块清单
UTTU_SECTIONS = ["main", "event", "character", "anecdote", "other"]

# 板块 → 中文分类名（写入 category_title）
SECTION_NAMES_ZH = {
    "main":      "主线",
    "event":     "活动",
    "character": "角色",
    "anecdote":  "轶事",
    "other":     "其他",
}

# 板块页解析失败时的内置回退清单
FALLBACK_SLUGS = {
    "main": [
        "chapter-0", "chapter-1", "chapter-2", "chapter-3", "chapter-4",
        "chapter-5", "chapter-5sp", "chapter-6", "chapter-7", "chapter-7sp",
        "chapter-8", "chapter-9", "chapter-10", "chapter-11", "chapter-12",
        "chapter-13", "chapter-14", "chapter-14sp", "trails/800260x",
    ],
    "event": [
        "1_1", "1_2", "1_3", "1_5", "1_6", "1_8", "2_0", "2_1", "2_3",
        "2_4", "2_5", "2_7", "3_1", "3_2", "3_4", "3_5", "3_6", "3_8",
        "3_9", "S01_1", "S01_2", "S02",
    ],
    "character": [
        "37", "6", "aleph", "anjo_nala", "argus", "barcarola", "beryl",
        "brume", "charon", "cheng_heguang", "cornerstone", "corvus",
        "everecho", "ezra_theodore", "flutterpage", "getian", "hedone",
        "hissabeth", "huntsworn_lilya", "igor", "isolde", "j", "jessica",
        "jiu_niangzi", "kaalaa_baunaa", "kakania", "kiperina", "liang_yue",
        "lopera", "lorentz_butterfly", "lucy", "marcus", "marsha", "melania",
        "mercuria", "moldir", "ms_stranger", "narcissus", "nautika", "noire",
        "paper_heron", "pickles", "ramona", "recoleta", "rhiannon", "rubuska",
        "sentinel", "shamane", "spathodea", "tooth_fairy", "tuesday", "vila",
        "willow", "windsong",
    ],
    "anecdote": [
        "37", "a_knight", "alien_t", "an-an_lee", "beryl", "bette",
        "bkornblume", "blonney", "centurion", "charlie", "diggers", "dikke",
        "eagle", "erick", "eternity", "fatutu", "getian", "kanjira",
        "la_source", "lilya", "matilda", "mercuria", "mesmer_jr.", "moldir",
        "necrologist", "oliver_fog", "onion", "regulus", "semmelweis",
        "silverwing_eagle", "sotheby", "tennant", "x",
    ],
    "other": [
        "4_0_lilya", "asd", "echoes", "psp", "sos", "ulrich", "uttu1_1",
        "uttu1_2", "uttu1_3", "uttu1_4", "uttu1_6",
    ],
}

# ---- 关键：锚定到脚本所在目录，不受 cwd 影响 --------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "rain1999", "data")
DEFAULT_DB_FILE = "rain1999_story.db"
SIM_FILENAME = "rain1999_sim.py"
BRIDGE_FILENAME = "rain1999_uttu_bridge.py"

# 可选：故事线/时间线图谱桥接（uttu 子命令）。缺失时其余功能完全不受影响。
try:
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import rain1999_uttu_bridge as uttu_bridge
    _HAS_BRIDGE = True
except Exception as _e:                                    # noqa: BLE001
    uttu_bridge = None
    _HAS_BRIDGE = False
    _BRIDGE_ERR = _e


# ===========================================================================
# 一、网络层（UTTU 抓取）
# ===========================================================================

class RateLimiter:
    """全局限速器：所有线程共享同一把锁，保证请求间隔不小于 delay 秒。"""

    def __init__(self, delay):
        self.delay = delay
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            gap = self._last + self.delay - now
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()


def build_uttu_opener():
    opener = urllib.request.build_opener()
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ("Accept",
         "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"),
        ("Referer", DEFAULT_BASE + "/"),
    ]
    return opener


def uttu_fetch(url, cache_dir, opener, limiter,
               retries=3, timeout=30):
    """带磁盘缓存 + 指数退避重试的 GET，返回页面文本。"""
    key = hashlib.md5(url.encode("utf-8")).hexdigest()
    path = os.path.join(cache_dir, key + ".html")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            pass

    last_err = None
    for attempt in range(retries):
        try:
            limiter.wait()
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            return body
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (2 ** attempt) + random.random() * 0.5)
    raise RuntimeError(f"fetch failed after {retries} tries: {url} ({last_err})")


def extract_slugs(board_html, section):
    """从板块页 HTML 提取该板块的故事清单，返回 [(path, slug)]。"""
    items = set()
    for s in re.findall(
        r"href=/story/(?:en|chs)/" + re.escape(section)
        + r"/([a-zA-Z0-9_.\-]+)",
        board_html,
    ):
        items.add((section, s))
    if section == "main":
        for s in re.findall(
            r"href=/story/(?:en|chs)/trails/([a-zA-Z0-9_.\-]+)", board_html,
        ):
            items.add(("trails", s))
    return sorted(items)


def get_uttu_section_slugs(section, opener, limiter, cache_dir):
    """获取某板块的 slug 清单；失败时回退到内置清单。"""
    try:
        body = uttu_fetch(f"{DEFAULT_BASE}/story/{section}/",
                          cache_dir, opener, limiter)
        items = extract_slugs(body, section)
        if items:
            return items
    except Exception as e:  # noqa: BLE001
        print(f"      [warn] board page {section} failed: {e}")

    items = []
    for s in FALLBACK_SLUGS.get(section, []):
        if "/" in s:
            path, slug = s.split("/", 1)
            items.append((path, slug))
        else:
            items.append((section, s))
    print(f"      [plan] {section}: fallback to builtin {len(items)} stories")
    return items


def fetch_uttu_story(path, slug, cache_dir, opener, limiter):
    """抓取单个故事的 transcript 页面并解析 chaptersData。"""
    url = f"{DEFAULT_BASE}/story/chs/{path}/{slug}/transcript/"
    body = uttu_fetch(url, cache_dir, opener, limiter)
    m = re.search(
        r'<script type="text/plain" id="chaptersData">(.*?)</script>',
        body, re.S)
    if not m:
        return slug, {"error": "chaptersData not found", "url": url}
    try:
        obj = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        return slug, {"error": f"json parse: {e}", "url": url}
    obj["_source"] = url
    return slug, obj


def load_or_fetch_uttu(section, plan, cache_dir, opener, limiter,
                       workers=8, limit=0):
    """从缓存或网络抓取某板块的所有故事，返回 {slug: obj}。"""
    results = {}
    todo = []

    # 先尝试从磁盘缓存读取，跳过已命中的 URL
    for path, slug in plan:
        url = f"{DEFAULT_BASE}/story/chs/{path}/{slug}/transcript/"
        key = hashlib.md5(url.encode("utf-8")).hexdigest()
        cpath = os.path.join(cache_dir, key + ".html")
        if os.path.exists(cpath):
            try:
                with open(cpath, "r", encoding="utf-8",
                          errors="replace") as f:
                    body = f.read()
                m = re.search(
                    r'<script type="text/plain" id="chaptersData">(.*?)'
                    r'</script>',
                    body, re.S)
                if m:
                    obj = json.loads(m.group(1))
                    obj["_source"] = url
                    results[slug] = obj
                    continue
            except Exception:  # noqa: BLE001
                pass
        todo.append((path, slug))

    if limit > 0:
        todo = todo[:limit]

    print(f"      [run] {section}: cached {len(results)}, "
          f"to fetch {len(todo)}")

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(fetch_uttu_story, path, slug,
                          cache_dir, opener, limiter): slug
                for path, slug in todo
            }
            done = 0
            for fut in as_completed(futs):
                try:
                    slug, obj = fut.result()
                    results[slug] = obj
                except Exception as e:  # noqa: BLE001
                    print(f"      [fail] {e}")
                done += 1
                if done % 20 == 0 or done == len(futs):
                    print(f"      [run] {section}: fetched "
                          f"{done}/{len(futs)}")

    return results


# ===========================================================================
# 二、章节 HTML → (speaker, text) 解析
# ===========================================================================

class _BlockTextParser(HTMLParser):
    """按块级标签收集文本块。"""

    BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)

    def _flush(self):
        text = "".join(self._buf).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            self.blocks.append(text)
        self._buf = []


_SPEAKER_RE = re.compile(r"^([^：:\n]{1,24})[：:]\s*(.+)$")


def parse_uttu_chapter_html(html_text):
    """解析章节 HTML 成 [(speaker, text), ...]。speaker 可能为 None。"""
    if not html_text:
        return []
    parser = _BlockTextParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001
        # 兜底：去掉标签按行处理
        text = re.sub(r"<[^>]+>", "\n", html_text)
        return [(None, t.strip()) for t in text.split("\n") if t.strip()]

    out = []
    for block in parser.blocks:
        m = _SPEAKER_RE.match(block)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
        else:
            out.append((None, block))
    return out


# ===========================================================================
# 三、UTTU → rain1999 数据适配
# ===========================================================================

def adapt_uttu(uttu_dict, section):
    """
    把 UTTU 的 {slug: chaptersData} 适配成 rain1999 预期的
        (episodes, results)
      episodes: [(fname, url, entry), ...]
      results:  {fname: {"lines": [{"id","nodeId","type","spk_zh","zh"}, ...]}}
    """
    episodes = []
    results = {}
    name_zh = SECTION_NAMES_ZH.get(section, section)

    for slug in sorted(uttu_dict.keys()):
        obj = uttu_dict[slug]
        if not isinstance(obj, dict) or "error" in obj:
            continue

        fm = obj.get("frontmatter") or {}
        chapters = obj.get("chapters") or []

        ep_id = f"{section}_{slug}"
        fname = ep_id

        title_zh = (fm.get("title") or "").strip() or slug
        subtitle = (fm.get("subtitle") or "").strip()
        chapter_title = subtitle or title_zh

        lines = []
        for ci, ch in enumerate(chapters):
            ch_num = ch.get("number")
            if ch_num is None:
                ch_num = ci + 1
            node_id = f"{ep_id}_c{ch_num}"
            content_html = ch.get("content") or ""
            parsed = parse_uttu_chapter_html(content_html)
            for spk, txt in parsed:
                lines.append({
                    "id": "",
                    "nodeId": node_id,
                    "type": "dialog",
                    "spk_zh": spk or "",
                    "zh": txt or "",
                })

        entry = {
            "id": ep_id,
            "tag": slug,
            "title_zh": title_zh,
            "chapterTitle": chapter_title,
            "categoryTitle": name_zh,
            "entryNodeId": "",
        }

        episodes.append((fname, obj.get("_source", ""), entry))
        results[fname] = {"lines": lines}

    return episodes, results


# ===========================================================================
# 四、数据库 Schema（保持不变）
# ===========================================================================

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS episodes (
    id              TEXT PRIMARY KEY,
    tag             TEXT,
    title_zh        TEXT,
    chapter_title   TEXT,
    category_title  TEXT,
    entry_node_id   TEXT,
    line_count      INTEGER
);

CREATE TABLE IF NOT EXISTS lines (
    id          TEXT,
    episode_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    node_id     TEXT,
    type        TEXT,
    speaker_zh  TEXT,
    zh          TEXT,
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_lines_episode ON lines(episode_id);
CREATE INDEX IF NOT EXISTS idx_lines_seq     ON lines(episode_id, seq);
CREATE INDEX IF NOT EXISTS idx_lines_speaker ON lines(speaker_zh);
CREATE INDEX IF NOT EXISTS idx_lines_node    ON lines(node_id);

CREATE TABLE IF NOT EXISTS timeline (
    episode_id  TEXT PRIMARY KEY,
    order_key   INTEGER NOT NULL,
    arc         TEXT,
    arc_kind    TEXT,
    phase       TEXT,
    label       TEXT,
    note        TEXT,
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_timeline_order ON timeline(order_key);
CREATE INDEX IF NOT EXISTS idx_timeline_phase ON timeline(phase, order_key);
CREATE INDEX IF NOT EXISTS idx_timeline_kind  ON timeline(arc_kind, order_key);
"""


def init_db(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def write_db(conn, episodes, results):
    cur = conn.cursor()
    cur.execute("DELETE FROM lines")
    cur.execute("DELETE FROM episodes")

    ep_rows, line_rows = [], []
    for fname, _url, entry in episodes:
        data = results.get(fname)
        if not data:
            continue
        lines = data.get("lines") or []
        eid = entry.get("id") or ""
        ep_rows.append((
            eid,
            entry.get("tag"),
            entry.get("title_zh"),
            entry.get("chapterTitle"),
            entry.get("categoryTitle"),
            entry.get("entryNodeId"),
            len(lines),
        ))
        for idx, line in enumerate(lines, start=1):
            line_rows.append((
                str(line.get("id") or ""),
                eid,
                idx,
                str(line.get("nodeId") or ""),
                line.get("type") or "",
                (line.get("spk_zh") or "").strip(),
                (line.get("zh") or "").strip(),
            ))

    cur.executemany(
        "INSERT INTO episodes "
        "(id, tag, title_zh, chapter_title, category_title, entry_node_id, line_count) "
        "VALUES (?,?,?,?,?,?,?)", ep_rows)
    cur.executemany(
        "INSERT INTO lines "
        "(id, episode_id, seq, node_id, type, speaker_zh, zh) "
        "VALUES (?,?,?,?,?,?,?)", line_rows)
    conn.commit()
    return len(ep_rows), len(line_rows)


# ===========================================================================
# 五、TXT 导出（保持不变）
# ===========================================================================

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')
_MULTISPACE = re.compile(r"\s+")


def safe_filename(name, fallback="untitled", max_len=120):
    name = _ILLEGAL.sub("_", name or "")
    name = _MULTISPACE.sub(" ", name).strip().strip(". ")
    if not name:
        name = fallback
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    return name


def safe_dirname(name, fallback="未分类"):
    name = _ILLEGAL.sub("_", name or "")
    name = _MULTISPACE.sub(" ", name).strip().strip(". ")
    return name or fallback


def decide_folder(entry):
    cat = (entry.get("categoryTitle") or "").strip()
    if cat:
        return safe_dirname(cat)
    chapter = (entry.get("chapterTitle") or "").strip()
    if chapter:
        return safe_dirname(chapter)
    name, _kind = classify_arc(entry)
    return safe_dirname(name)


def export_txt(txt_root, episodes, results):
    os.makedirs(txt_root, exist_ok=True)
    used = {}
    written = 0
    per_dir = {}

    for fname, _url, entry in episodes:
        data = results.get(fname)
        if not data:
            continue

        lines = data.get("lines") or []
        eid = str(entry.get("id") or "")
        title = (entry.get("title_zh") or "").strip() or eid or "untitled"
        chapter = (entry.get("chapterTitle") or "").strip()
        category = (entry.get("categoryTitle") or "").strip()
        tag = (entry.get("tag") or "").strip()

        subdir = decide_folder(entry)
        dir_path = os.path.join(txt_root, subdir)
        os.makedirs(dir_path, exist_ok=True)

        base = safe_filename(f"{eid}_{title}" if eid else title)
        key = (subdir, base)
        if key in used:
            used[key] += 1
            base = f"{base}_{used[key]}"
        else:
            used[key] = 0
        path = os.path.join(dir_path, base + ".txt")

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"【分类】{category}\n")
            fh.write(f"【章节】{chapter}\n")
            if tag:
                fh.write(f"【标签】{tag}\n")
            fh.write(f"【标题】{title}\n")
            fh.write(f"【ID】{eid}\n")
            fh.write(f"【台词数】{len(lines)}\n")
            fh.write("-" * 50 + "\n")
            for line in lines:
                zh = (line.get("zh") or "").strip()
                spk = (line.get("spk_zh") or "").strip()
                if not zh and not spk:
                    continue
                fh.write(f"{spk}：{zh}\n" if spk else f"{zh}\n")

        written += 1
        per_dir[subdir] = per_dir.get(subdir, 0) + 1

    return written, per_dir


# ===========================================================================
# 六、时间线 & 篇章标注（保持不变）
# ===========================================================================

TIMELINE_PHASES = [
    ("第一幕 · 暴雨降临",  ["序章", "第一章", "第二章"],   "1999"),
    ("第二幕 · 逃离",      ["第三章", "第四章"],            "1966"),
    ("第三幕 · 溯源",      ["第五章", "第六章"],            "1929"),
]

ARC_RULES = [
    ("主线", "main", [
        {"field": "category_title", "contains": "主线"},
        {"field": "tag",            "contains": "main"},
        {"field": "chapter_title",
         "regex": r"^(序章|第[一二三四五六七八九十百零\d]+章|主线)"},
    ]),
    ("角色", "character", [
        {"field": "category_title", "contains": "角色"},
        {"field": "category_title", "contains": "人物"},
        {"field": "tag",            "contains": "char"},
        {"field": "tag",            "contains": "role"},
    ]),
    ("活动", "event", [
        {"field": "category_title", "contains": "活动"},
        {"field": "tag",            "contains": "event"},
    ]),
    ("番外", "side", [
        {"field": "category_title", "contains": "番外"},
        {"field": "category_title", "contains": "支线"},
        {"field": "tag",            "contains": "side"},
        {"field": "tag",            "contains": "extra"},
    ]),
]

ARC_LABEL = {
    "main":      "【主线】",
    "character": "【角色】",
    "event":     "【活动】",
    "side":      "【番外】",
    "other":     "【未分类】",
}

ARC_WEIGHT = {"main": 1, "character": 3, "event": 4, "side": 5, "other": 9}


def classify_arc(ep):
    for name, kind, conds in ARC_RULES:
        for c in conds:
            v = (ep.get(c["field"]) or "")
            if "contains" in c and c["contains"] in v:
                return name, kind
            if "regex" in c and re.search(c["regex"], v):
                return name, kind
    return "未分类", "other"


def build_timeline(conn):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("DELETE FROM timeline")

    phase_rank, phase_label, chapter_to_phase = {}, {}, {}
    for i, (name, chapters, label) in enumerate(TIMELINE_PHASES):
        phase_rank[name] = i
        phase_label[name] = label
        for ch in chapters:
            chapter_to_phase[ch] = name

    chapter_rank = {
        r["chapter_title"]: r["r"]
        for r in conn.execute(
            "SELECT chapter_title, MIN(rowid) AS r FROM episodes "
            "WHERE chapter_title IS NOT NULL AND chapter_title <> '' "
            "GROUP BY chapter_title")
    }

    rows = list(conn.execute("""
        SELECT rowid AS rid, id, tag, chapter_title, category_title
        FROM episodes ORDER BY rowid
    """))

    out = []
    for r in rows:
        ch = r["chapter_title"] or ""
        cat = r["category_title"] or ""
        tag = r["tag"] or ""

        phase = None
        for key, name in chapter_to_phase.items():
            if ch == key or (key and ch.startswith(key)):
                phase = name
                break

        arc_name, arc_kind = classify_arc({
            "category_title": cat,
            "tag": tag,
            "chapter_title": ch,
        })

        if phase is None:
            pw = 9_999_999
            phase_out = "未分类"
            label = ""
        else:
            pw = phase_rank[phase]
            phase_out = phase
            label = phase_label.get(phase, "")

        cw = ARC_WEIGHT.get(arc_kind, 9)
        cr = chapter_rank.get(ch, 9_999_999)
        order_key = pw * 10_000_000 + cw * 1_000_000 + cr * 100 + (r["rid"] % 100)

        out.append((r["id"], order_key, arc_name, arc_kind,
                    phase_out, label, ""))

    cur.executemany(
        "INSERT INTO timeline "
        "(episode_id, order_key, arc, arc_kind, phase, label, note) "
        "VALUES (?,?,?,?,?,?,?)", out)
    conn.commit()
    return len(out)


# ===========================================================================
# 七、数据访问层（StoryDB / StoryPlayer）（保持不变）
# ===========================================================================

class StoryDB:
    def __init__(self, db_path, readonly=True):
        if readonly:
            uri = f"file:{db_path}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True)
        else:
            self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def stats(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM episodes")
        n_ep = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM lines")
        n_lines = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT chapter_title) FROM episodes")
        n_ch = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT category_title) FROM episodes")
        n_cat = c.fetchone()[0]
        return {"episodes": n_ep, "lines": n_lines,
                "chapters": n_ch, "categories": n_cat}

    def chapters(self):
        rows = self.conn.execute("""
            SELECT chapter_title AS chapter,
                   COUNT(*)       AS episode_count,
                   SUM(line_count) AS line_count
            FROM episodes
            WHERE chapter_title IS NOT NULL AND chapter_title <> ''
            GROUP BY chapter_title
            ORDER BY MIN(rowid)
        """).fetchall()
        return [dict(r) for r in rows]

    def categories(self):
        rows = self.conn.execute("""
            SELECT category_title AS category, COUNT(*) AS episode_count
            FROM episodes
            GROUP BY category_title
            ORDER BY COUNT(*) DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def episodes(self, chapter=None, category=None, keyword=None):
        sql = ("SELECT id, title_zh AS title, tag, chapter_title AS chapter, "
               "category_title AS category, entry_node_id AS entry_node, "
               "line_count FROM episodes WHERE 1=1")
        args = []
        if chapter:
            sql += " AND chapter_title = ?"; args.append(chapter)
        if category:
            sql += " AND category_title = ?"; args.append(category)
        if keyword:
            sql += " AND (title_zh LIKE ? OR id LIKE ?)"
            args += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY rowid"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def episode(self, episode_id, with_lines=True):
        row = self.conn.execute(
            "SELECT id, title_zh AS title, tag, chapter_title AS chapter, "
            "category_title AS category, entry_node_id AS entry_node, "
            "line_count FROM episodes WHERE id = ?", (str(episode_id),)
        ).fetchone()
        if not row:
            return None
        ep = dict(row)
        if with_lines:
            ep["lines"] = self.lines(episode_id)
        return ep

    def lines(self, episode_id):
        rows = self.conn.execute(
            "SELECT seq, id, node_id, type, speaker_zh, zh "
            "FROM lines WHERE episode_id = ? ORDER BY seq",
            (str(episode_id),)
        ).fetchall()
        return [{
            "seq": r["seq"],
            "id": r["id"] or "",
            "node_id": r["node_id"] or "",
            "type": r["type"] or "dialog",
            "speaker": (r["speaker_zh"] or "").strip(),
            "text": (r["zh"] or "").strip(),
        } for r in rows]

    def lines_by_node(self, episode_id, node_id):
        rows = self.conn.execute(
            "SELECT seq, id, node_id, type, speaker_zh, zh FROM lines "
            "WHERE episode_id = ? AND node_id = ? ORDER BY seq",
            (str(episode_id), str(node_id))
        ).fetchall()
        return [{
            "seq": r["seq"], "id": r["id"] or "", "node_id": r["node_id"] or "",
            "type": r["type"] or "dialog",
            "speaker": (r["speaker_zh"] or "").strip(),
            "text": (r["zh"] or "").strip(),
        } for r in rows]

    def search(self, keyword, limit=200):
        rows = self.conn.execute("""
            SELECT l.episode_id, e.title_zh AS title,
                   e.chapter_title AS chapter, l.seq,
                   l.speaker_zh, l.zh
            FROM lines l JOIN episodes e ON e.id = l.episode_id
            WHERE l.zh LIKE ?
            ORDER BY l.episode_id, l.seq
            LIMIT ?
        """, (f"%{keyword}%", limit)).fetchall()
        return [{
            "episode_id": r["episode_id"], "title": r["title"],
            "chapter": r["chapter"], "seq": r["seq"],
            "speaker": (r["speaker_zh"] or "").strip(),
            "text": (r["zh"] or "").strip(),
        } for r in rows]

    def speakers(self, limit=50):
        rows = self.conn.execute("""
            SELECT speaker_zh AS speaker, COUNT(*) AS n
            FROM lines WHERE speaker_zh IS NOT NULL AND speaker_zh <> ''
            GROUP BY speaker_zh ORDER BY n DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def timeline(self, phase=None, arc=None, kind=None, limit=None):
        sql = """
            SELECT t.episode_id AS id, t.order_key,
                   t.arc, t.arc_kind, t.phase, t.label,
                   e.title_zh AS title, e.chapter_title AS chapter,
                   e.category_title AS category, e.line_count
            FROM timeline t JOIN episodes e ON e.id = t.episode_id
            WHERE 1=1
        """
        args = []
        if phase: sql += " AND t.phase = ?";    args.append(phase)
        if arc:   sql += " AND t.arc = ?";      args.append(arc)
        if kind:  sql += " AND t.arc_kind = ?"; args.append(kind)
        sql += " ORDER BY t.order_key"
        if limit:
            sql += " LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def phases(self):
        rows = self.conn.execute("""
            SELECT phase, MIN(order_key) AS k, COUNT(*) AS n
            FROM timeline GROUP BY phase ORDER BY k
        """).fetchall()
        return [dict(r) for r in rows]

    def arc_summary(self):
        rows = self.conn.execute("""
            SELECT arc, arc_kind, COUNT(*) AS n
            FROM timeline GROUP BY arc, arc_kind
            ORDER BY MIN(order_key)
        """).fetchall()
        return [dict(r) for r in rows]

    def play(self, episode_id):
        return StoryPlayer(self, episode_id)


class StoryPlayer:
    def __init__(self, db, episode_id):
        self._db = db
        self._id = str(episode_id)
        self.meta = db.episode(self._id, with_lines=False) or {}
        self._lines = db.lines(self._id)
        self._idx = 0

    def __iter__(self):
        self.reset()
        while True:
            line = self.next()
            if line is None:
                return
            yield line

    def __len__(self):
        return len(self._lines)

    def next(self):
        if self._idx >= len(self._lines):
            return None
        line = self._lines[self._idx]
        self._idx += 1
        return line

    def peek(self):
        if self._idx >= len(self._lines):
            return None
        return self._lines[self._idx]

    def seek(self, seq):
        for i, l in enumerate(self._lines):
            if l["seq"] == seq:
                self._idx = i
                return l
        return None

    def reset(self):
        self._idx = 0

    def progress(self):
        return self._idx, len(self._lines)

    def is_finished(self):
        return self._idx >= len(self._lines)

    def all_lines(self):
        return list(self._lines)


# ===========================================================================
# 八、子命令
# ===========================================================================

def cmd_crawl(args):
    out_dir = args.out
    cache_dir = os.path.join(out_dir, "_cache")
    os.makedirs(cache_dir, exist_ok=True)

    opener = build_uttu_opener()
    limiter = RateLimiter(args.delay)

    sections = list(UTTU_SECTIONS)

    print(f"[1/4] 解析板块 slug（共 {len(sections)} 个板块）")
    plan = {}
    for sec in sections:
        plan[sec] = get_uttu_section_slugs(sec, opener, limiter, cache_dir)
        print(f"      {sec}: {len(plan[sec])} 条")

    print(f"[2/4] 抓取剧情正文（并发 {args.workers}, 间隔 {args.delay}s）")
    all_episodes = []
    all_results = {}
    for sec in sections:
        uttu_data = load_or_fetch_uttu(
            sec, plan[sec], cache_dir, opener, limiter,
            workers=args.workers, limit=args.limit)
        eps, res = adapt_uttu(uttu_data, sec)
        all_episodes.extend(eps)
        all_results.update(res)
        print(f"      {sec}: {len(eps)} episodes")

    total = len(all_episodes)
    print(f"      共 {total} 条剧情")

    db_path = os.path.join(out_dir, args.db)
    print(f"[3/4] 写入数据库: {db_path}")
    conn = init_db(db_path)
    try:
        n_ep, n_lines = write_db(conn, all_episodes, all_results)
        n_tl = build_timeline(conn)
    finally:
        conn.close()
    print(f"      剧情条目 {n_ep}, 台词 {n_lines}, 时间线 {n_tl}")

    txt_root = os.path.join(out_dir, args.txt_dir)
    n_txt, per_dir = export_txt(txt_root, all_episodes, all_results)
    print(f"[4/4] TXT 已写出: {txt_root}（共 {n_txt} 个文件）")
    for name, cnt in sorted(per_dir.items(), key=lambda x: -x[1]):
        print(f"    {name}/ : {cnt}")

    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT chapter_title) FROM episodes")
        n_chapters = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT category_title) FROM episodes")
        n_cats = c.fetchone()[0]
        c.execute("""SELECT category_title, COUNT(*) FROM episodes
                     GROUP BY category_title ORDER BY COUNT(*) DESC""")
        cat_dist = c.fetchall()
        c.execute("""SELECT arc, arc_kind, COUNT(*) FROM timeline
                     GROUP BY arc, arc_kind ORDER BY MIN(order_key)""")
        arc_dist = c.fetchall()
    finally:
        conn.close()

    print("=== 抓取概览 ===")
    print(f"剧情条目: {n_ep}  台词行: {n_lines}  TXT: {n_txt}")
    print(f"章节数: {n_chapters}  分类数: {n_cats}")
    print("分类分布:")
    for name, cnt in cat_dist:
        print(f"  {name}: {cnt}")
    print("篇章分布:")
    for name, kind, cnt in arc_dist:
        print(f"  {ARC_LABEL.get(kind, '【?】')} {name}: {cnt}")


def cmd_stats(args):
    db = StoryDB(args.db)
    try:
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
        print("\n分类分布:")
        for c in db.categories():
            print(f"  {c['category']}: {c['episode_count']}")
        print("\n篇章分布:")
        for r in db.arc_summary():
            print(f"  {ARC_LABEL.get(r['arc_kind'], '【?】')} "
                  f"{r['arc']}: {r['n']}")
    finally:
        db.close()


def cmd_chapters(args):
    db = StoryDB(args.db)
    try:
        for ch in db.chapters():
            print(f"{ch['episode_count']:>4} 集  "
                  f"{ch['line_count'] or 0:>6} 行  {ch['chapter']}")
    finally:
        db.close()


def cmd_episodes(args):
    db = StoryDB(args.db)
    try:
        eps = db.episodes(chapter=args.chapter,
                          category=args.category,
                          keyword=args.kw)
        for ep in eps:
            print(f"{ep['id']:<10} {ep['line_count']:>4} 行  "
                  f"{ep['title']}  【{ep['chapter']}】")
        print(f"\n共 {len(eps)} 条")
    finally:
        db.close()


def cmd_play(args):
    db = StoryDB(args.db)
    try:
        p = db.play(args.id)
        if not p.meta:
            print(f"未找到剧情: {args.id}", file=sys.stderr)
            sys.exit(1)
        meta = p.meta
        print(f"# {meta.get('title')}")
        print(f"# 章节: {meta.get('chapter')}   分类: {meta.get('category')}")
        print(f"# 共 {len(p)} 行")
        print("-" * 50)
        if args.seq and args.seq > 1:
            p.seek(args.seq)
        while True:
            line = p.next()
            if line is None:
                break
            if line["speaker"]:
                print(f'{line["speaker"]}：{line["text"]}')
            else:
                print(line["text"])
    finally:
        db.close()


def cmd_search(args):
    db = StoryDB(args.db)
    try:
        hits = db.search(args.kw, limit=args.limit)
        for h in hits:
            print(f'{h["chapter"]} / {h["title"]}  #{h["seq"]}')
            print(f'    {h["speaker"]}：{h["text"]}')
        print(f"\n共 {len(hits)} 条匹配（上限 {args.limit}）")
    finally:
        db.close()


def cmd_speakers(args):
    db = StoryDB(args.db)
    try:
        for r in db.speakers(limit=args.limit):
            print(f"{r['n']:>6}  {r['speaker']}")
    finally:
        db.close()


def cmd_timeline(args):
    db = StoryDB(args.db)
    try:
        rows = db.timeline(phase=args.phase, arc=args.arc, kind=args.kind)

        print("篇章统计:")
        for r in db.arc_summary():
            print(f'  {ARC_LABEL.get(r["arc_kind"], "【?】")} '
                  f'{r["arc"]:<6} {r["n"]} 集')
        print()

        cur = None
        for r in rows:
            if r["phase"] != cur:
                cur = r["phase"]
                print(f"\n【{cur}】")
            mark = ARC_LABEL.get(r["arc_kind"], "【?】")
            label = f'{r["label"]:<4}' if r["label"] else "    "
            print(f'  {r["id"]:<10} {label} {mark} '
                  f'{r["title"]}  ({r["chapter"]}, {r["line_count"]} 行)')
        print(f"\n共 {len(rows)} 条")
    finally:
        db.close()


def cmd_retimeline(args):
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(SCHEMA)
        n = build_timeline(conn)
        print(f"时间线已重建: {n} 条")
    finally:
        conn.close()


# ---- 与模拟器联动 -------------------------------------------------------

def _db_path_for(args):
    """根据 --db 参数（可能是文件名或路径）解析出实际路径。"""
    p = args.db
    if os.path.isabs(p) or os.sep in p or "/" in p:
        return p
    return os.path.join(DEFAULT_OUT, p)


def _sim_path():
    return os.path.join(HERE, SIM_FILENAME)


def _launch_sim():
    sim = _sim_path()
    if not os.path.exists(sim):
        print(f"未找到模拟器: {sim}", file=sys.stderr)
        print(f"请把 rain1999_sim.py 放在与 {os.path.basename(__file__)} "
              f"相同的目录下。", file=sys.stderr)
        sys.exit(1)
    os.execv(sys.executable, [sys.executable, sim])


def cmd_sim(_args):
    """直接启动模拟器（要求数据库已存在）。"""
    _launch_sim()


def cmd_auto(args):
    """没库先抓、有库直接进模拟器。"""
    db_path = _db_path_for(args)
    if os.path.exists(db_path):
        print(f"[auto] 已存在数据库：{db_path}")
    else:
        print(f"[auto] 未找到数据库：{db_path}")
        print(f"[auto] 开始抓取 ……")
        crawl_args = argparse.Namespace(
            base=DEFAULT_BASE,
            out=DEFAULT_OUT,
            db=DEFAULT_DB_FILE,
            txt_dir="story_txt",
            workers=15,
            delay=0.05,
            retries=4,
            slim_cache=True,
            limit=0,
        )
        cmd_crawl(crawl_args)
        if not os.path.exists(db_path):
            print("[auto] 抓取未生成数据库，退出。", file=sys.stderr)
            sys.exit(1)
    print("[auto] 启动模拟器 ……")
    _launch_sim()


def cmd_uttu(args):
    """
    抓取 UTTU 的「故事线 Storyline」与「时间线 Timeline」两个图谱页面，
    解析后并入剧情库，使模拟器能读到官方的剧情顺序与世界史年代。

    只新增 uttu_* 表，不改动 episodes / lines / timeline 三张原表；
    加 --rewrite-timeline 才会用官方顺序覆写 timeline 表。
    """
    if not _HAS_BRIDGE:
        print(f"未找到桥接模块 {BRIDGE_FILENAME}（或导入失败：{_BRIDGE_ERR}）。",
              file=sys.stderr)
        print(f"请把 {BRIDGE_FILENAME} 放在与本脚本相同的目录下。", file=sys.stderr)
        sys.exit(1)

    db_path = None if args.json_only else _db_path_for(args)
    out_dir = args.out or (os.path.join(DEFAULT_OUT, "graph") if args.json_only else None)
    cache_dir = args.cache_dir or os.path.join(DEFAULT_OUT, "_cache")

    print(f"[uttu] 故事线：{args.base}/storyline/")
    print(f"[uttu] 时间线：{args.base}/timeline/")
    r = uttu_bridge.sync(db_path=db_path, base=args.base, cache_dir=cache_dir,
                         from_cache=args.from_cache,
                         rewrite=args.rewrite_timeline, json_out=out_dir)
    if db_path:
        print(f"[uttu] 数据库：{db_path}")
        print(f"[uttu] 图谱节点命中剧情库：{r['matched']} 条；"
              f"时间线关联到已入库剧情：{r.get('event_matched', 0)} 条")
        if args.rewrite_timeline:
            print(f"[uttu] 已按官方图谱覆写 timeline：{r['rewritten']} 条")
    print("[uttu] 查看： python rain1999.py uttu-show --db "
          f"{args.db}")
    return 0


def cmd_uttu_show(args):
    """查看已并入的故事线图谱与世界史时间轴。"""
    if not _HAS_BRIDGE:
        print(f"未找到桥接模块 {BRIDGE_FILENAME}。", file=sys.stderr)
        sys.exit(1)
    return uttu_bridge.show(_db_path_for(args), args.limit)


# ===========================================================================
# 九、入口 / 参数
# ===========================================================================

def build_parser():
    ap = argparse.ArgumentParser(
        prog="rain1999",
        description="雨幕档案《重返未来：1999》剧情一体化工具")
    sub = ap.add_subparsers(dest="cmd")

    # crawl
    p = sub.add_parser("crawl", help="抓取剧情并生成数据库 / TXT")
    p.add_argument("--base", default=DEFAULT_BASE,
                   help="站点根地址（默认 UTTU 镜像）")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help=f"输出目录（默认 {DEFAULT_OUT}）")
    p.add_argument("--db", default=DEFAULT_DB_FILE, help="SQLite 文件名")
    p.add_argument("--txt-dir", default="story_txt",
                   help="每条剧情 TXT 存放的文件夹名（默认 story_txt）")
    p.add_argument("--workers", type=int, default=15,
                   help="并发下载线程数（默认 15）")
    p.add_argument("--delay", type=float, default=0.05,
                   help="每个请求后的休眠秒数（默认 0.05）")
    p.add_argument("--retries", type=int, default=4,
                   help="单文件失败重试次数（默认 4，UTTU 内部固定 3）")
    p.add_argument("--slim-cache", action="store_true",
                   help="（兼容旧参数，UTTU 缓存不做瘦身）")
    p.add_argument("--limit", type=int, default=0,
                   help="每板块仅抓前 N 条剧情（调试用，0=全部）")
    p.set_defaults(func=cmd_crawl)

    # sim
    p = sub.add_parser("sim", help="直接启动模拟器（要求库已存在）")
    p.set_defaults(func=cmd_sim)

    # auto —— 一键：没库先抓、有库进模拟器
    p = sub.add_parser("auto", help="有库直接进模拟器，没库先抓再进")
    p.add_argument("--db", default=DEFAULT_DB_FILE,
                   help="SQLite 文件名（默认 rain1999_story.db）")
    p.set_defaults(func=cmd_auto)

    # stats
    p = sub.add_parser("stats", help="显示数据库概览")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_stats)

    # chapters
    p = sub.add_parser("chapters", help="列出全部章节")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_chapters)

    # episodes
    p = sub.add_parser("episodes", help="列出剧情条目")
    p.add_argument("--db", required=True)
    p.add_argument("--chapter", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--kw", default=None)
    p.set_defaults(func=cmd_episodes)

    # play
    p = sub.add_parser("play", help="在终端播放一条剧情")
    p.add_argument("--db", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--seq", type=int, default=0)
    p.set_defaults(func=cmd_play)

    # search
    p = sub.add_parser("search", help="全文检索")
    p.add_argument("--db", required=True)
    p.add_argument("--kw", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_search)

    # speakers
    p = sub.add_parser("speakers", help="角色台词统计")
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_speakers)

    # timeline
    p = sub.add_parser("timeline", help="按叙事时间线查看剧情")
    p.add_argument("--db", required=True)
    p.add_argument("--phase", default=None)
    p.add_argument("--arc",   default=None)
    p.add_argument("--kind",  default=None,
                   choices=["main", "character", "event", "side", "other"])
    p.set_defaults(func=cmd_timeline)

    # retimeline
    p = sub.add_parser("retimeline", help="离线重建时间线（不重爬）")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_retimeline)

    # uttu —— 并入故事线 / 时间线图谱
    p = sub.add_parser("uttu",
                       help="抓取故事线+时间线图谱并入剧情库（模拟器可读）")
    p.add_argument("--db", default=DEFAULT_DB_FILE, help="SQLite 文件名")
    p.add_argument("--base", default=DEFAULT_BASE, help="站点根地址")
    p.add_argument("--cache-dir", default=None, help="HTML 缓存目录")
    p.add_argument("--rewrite-timeline", action="store_true",
                   help="用官方图谱顺序覆写 timeline 表（影响模拟器叙事顺序）")
    p.add_argument("--json-only", action="store_true",
                   help="只导出 JSON 图谱，不写库")
    p.add_argument("--out", default=None, help="JSON 导出目录（配合 --json-only）")
    p.add_argument("--from-cache", nargs=2,
                   metavar=("STORYLINE_HTML", "TIMELINE_HTML"),
                   help="离线：用本地已下载的 HTML 解析")
    p.set_defaults(func=cmd_uttu)

    # uttu-show —— 查看图谱
    p = sub.add_parser("uttu-show", help="查看已并入的故事线图谱与世界史时间轴")
    p.add_argument("--db", default=DEFAULT_DB_FILE)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_uttu_show)

    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    known = {"crawl", "sim", "auto",
             "stats", "chapters", "episodes",
             "play", "search", "speakers",
             "timeline", "retimeline",
             "uttu", "uttu-show"}

    if not argv:
        argv = ["crawl"]
    elif argv[0] not in known:
        if argv[0].startswith("-"):
            argv = ["crawl"] + argv
        else:
            # `python rain1999.py ./data --limit 20`
            argv = ["crawl", "--out", argv[0]] + argv[1:]

    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()