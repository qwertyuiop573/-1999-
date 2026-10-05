#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rain1999_web —— 《重返未来：1999》世界观模拟器 · 网页版 v3.1

v3.1 新增 / 变化：
  · 回退幅度动态：依据超阈值的瞬时严重程度分级（微溃 / 失序 / 崩解 / 溃灭 / 归墟）
  · 回退目标自动吸附到官方时段（TimelineDB.get_phase_for_ext_year）
  · 全链路实时日志（Termux 运行栏可见）
"""

import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIBS = os.path.join(_HERE, "libs")
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

import openai

try:
    from timeline_organizer import TimelineDB
    _HAS_TIMELINE = True
except Exception:
    TimelineDB = None
    _HAS_TIMELINE = False

TIMELINE = None

# ===========================================================================
# 日志系统 —— 所有输出走 stdout，Termux 运行栏实时可见
# ===========================================================================

_LOG_LOCK = threading.Lock()
_LOG_LEVEL = os.environ.get("RAIN_LOG_LEVEL", "info").lower()
_LOG_COLOR = os.environ.get("RAIN_LOG_COLOR", "1") not in ("0", "false", "no")

_COLORS = {
    "reset": "\033[0m", "dim": "\033[2m", "gray": "\033[90m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "magenta": "\033[95m", "cyan": "\033[96m",
    "white": "\033[97m",
}

_TAG_COLORS = {
    "boot":  "cyan",    "http":  "blue",   "llm":   "magenta",
    "storm": "yellow",  "game":  "green",  "state": "gray",
    "story": "cyan",    "save":  "blue",   "error": "red",
    "warn":  "yellow",
}


def _c(name, text):
    if not _LOG_COLOR:
        return text
    return _COLORS.get(name, "") + text + _COLORS["reset"]


def log(tag, msg, level="info", **fields):
    lv = {"debug": 10, "info": 20, "warn": 30, "error": 40}
    if lv.get(level, 20) < lv.get(_LOG_LEVEL, 20):
        return
    ts = time.strftime("%H:%M:%S")
    tc = _TAG_COLORS.get(tag, "white")
    line = f"{_c('gray', ts)} {_c(tc, '[' + tag + ']')} {msg}"
    if fields:
        tail = " ".join(f"{k}={v}" for k, v in fields.items())
        line += "  " + _c("gray", tail)
    with _LOG_LOCK:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def log_banner():
    print()
    print(_c("cyan", "─" * 62))
    print(_c("cyan", "  雨 幕 档 案 · 网 页 版 v3.1  ")
          + _c("gray", "REVERSE:1999"))
    print(_c("cyan", "─" * 62))


def log_rule(title="", width=62):
    if title:
        pad = max(0, width - len(title) - 6)
        print(_c("gray", "── ") + _c("cyan", title)
              + " " + _c("gray", "─" * pad))
    else:
        print(_c("gray", "─" * width))


# ===========================================================================
# 配置
# ===========================================================================

API_KEY = "sk-7945360e65caccac28f17c597ff94321f3070a40d7058992"
API_BASE = "https://ltzy.top/v1"
MODEL = "LTZY/deepseek-v4-pro"
NARRATOR_TEMP = 0.9
ARCHIVIST_TEMP = 1.0

# 未读可读物惩罚
MUST_READ_PENALTY = 3      # 每次推进且未读 → 每条未读加 N 点病害
MUST_READ_PENALTY_CAP = 20 # 单次惩罚上限
SKIP_PENALTY_MULT = 2      # /skip 强制跳过时倍率

SAVES_ROOT = os.path.join(_HERE, "saves")
os.makedirs(SAVES_ROOT, exist_ok=True)

HOST = os.environ.get("RAIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("RAIN_PORT", "8765"))
AUTOSAVE = os.path.join(_HERE, "sim_autosave.json")   # 旧版单文件存档（仅用于迁移）

# ===========================================================================
# 多存档槽位
# ===========================================================================
# saves/
#   index.json      {"current": "s1", "slots": [{id,name,char_name,...}]}
#   slot_s1.json    槽位存档（schema 与旧 autosave 相同）
# 旧版 sim_autosave.json 存在且无槽位时，首次启动自动迁移为槽位 s1。

SAVES_DIR = os.path.join(_HERE, "saves")
SAVES_INDEX = os.path.join(SAVES_DIR, "index.json")


def _slot_path(slot_id):
    return os.path.join(SAVES_DIR, f"slot_{slot_id}.json")


def _read_saves_index():
    try:
        with open(SAVES_INDEX, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("slots"), list):
            return data
    except Exception:
        pass
    return {"current": None, "slots": []}


def _write_saves_index(idx):
    os.makedirs(SAVES_DIR, exist_ok=True)
    tmp = SAVES_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, SAVES_INDEX)

client = openai.OpenAI(api_key=API_KEY, base_url=API_BASE)

# v3.1.2: 确保 saves/ 目录存在（存/读档不再因缺目录失败）
_SAVES_DIR = os.path.join(_HERE, "saves")
try:
    os.makedirs(_SAVES_DIR, exist_ok=True)
except Exception:
    pass


# ===========================================================================
# 备用 API 提供商（主站失败时自动切换）
# ===========================================================================
FALLBACK_PROVIDERS = [
    {
        "name": "YJS 中转",
        "base_url": "https://api.yjs.im/v1",
        "api_key": "sk-ao7sETTQFExOGYeOTOI3fETixW8cVG1XyCq1ibH72rJX9rG2",
        "model": "deepseek-v4-pro",
    },
    {
        "name": "幻城网安",
        "base_url": "https://api.hcnsec.cn/v1",
        "api_key": "sk-N6J7K7k5bonlfan1N7bZRhKp0sPy1NEw3kH6JagZ6INUXrC5",
        "model": "auto",
    },
    # 你可以继续添加更多备用：
    # {
    #     "name": "DeepSeek 官方",
    #     "base_url": "https://api.deepseek.com/v1",
    #     "api_key": "sk-你的deepseek-key",
    #     "model": "deepseek-chat",
    # },
]


def _llm_chat_via(base_url, api_key, model, messages, temperature):
    """用指定的 base_url 发起一次请求。"""
    _client = openai.OpenAI(api_key=api_key, base_url=base_url)
    resp = _client.chat.completions.create(
        model=model, messages=messages, temperature=temperature,
    )
    return resp.choices[0].message.content or ""


def llm_chat(messages, temperature=NARRATOR_TEMP, max_retries=0):
    last = None
    t0 = time.time()
    in_chars = sum(len(m.get("content") or "") for m in messages)
    log("llm", f"→ 请求 model={MODEL} temp={temperature}",
        level="info", msgs=len(messages), in_chars=in_chars)
    for i in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, temperature=temperature,
            )
            dt = time.time() - t0
            out = resp.choices[0].message.content or ""
            log("llm", f"← 完成 {dt:.1f}s",
                level="info", out_chars=len(out), retry=i)
            return out
        except Exception as e:
            last = e
            log("llm", f"! 失败 attempt={i+1}/{max_retries+1} "
                       f"{type(e).__name__}: {e}", level="warn")
            if i < max_retries:
                wait = 1.5 * (2 ** i)
                log("llm", f"… 退避 {wait:.1f}s 后重试", level="debug")
                time.sleep(wait)
    # 主站挂了 → 依次尝试备用提供商
    for prov in FALLBACK_PROVIDERS:
        name = prov.get("name", "备用")
        try:
            log("llm", f"↻ 切换备用 [{name}] {prov['base_url']}",
                level="warn")
            out = _llm_chat_via(
                prov["base_url"], prov["api_key"],
                prov.get("model", MODEL),
                messages, temperature,
            )
            log("llm", f"← 备用 [{name}] 成功 out_chars={len(out)}",
                level="info")
            return out
        except Exception as e2:
            log("llm", f"! 备用 [{name}] 失败：{e2}", level="warn")

    log("llm", f"x 放弃：{type(last).__name__}: {last}", level="error")
    raise RuntimeError(f"模型调用失败: {last}")


def extract_json(text):
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    return json.loads(s)


STAGE_RE = re.compile(r"【时间锚点[:：][\s\S]*?】")


def split_stage(reply):
    if not reply:
        return reply, None
    matches = list(STAGE_RE.finditer(reply))
    if not matches:
        return reply.rstrip(), None
    m = matches[-1]
    stage = m.group(0)[1:-1]
    stage = re.sub(r"^时间锚点[:：]\s*", "", stage).strip()
    clean = (reply[:m.start()] + reply[m.end():]).rstrip()
    return clean, stage


CLUE_BLOCK_RE = re.compile(
    r"【线索候选】\s*\n((?:[ \t]*[-·•].*\n?)+)",
    re.MULTILINE,
)

READABLE_BLOCK_RE = re.compile(
    r"【可读物】\s*\n((?:[ \t]*[-·•].*\n?)+)",
    re.MULTILINE,
)
# 每行格式：- [类型] 名称 :: 内容摘要
READABLE_LINE_RE = re.compile(
    r"^\[([^\]]{1,12})\]\s*([^:：]{1,40})"
    r"(?:[:：]{2}|[:：])\s*(.+)$"
)


def extract_readables(reply):
    """提取【可读物】块。
    返回 (clean_reply, [{kind, name, content}, ...])"""
    if not reply:
        return reply, []
    m = READABLE_BLOCK_RE.search(reply)
    if not m:
        return reply, []
    items = []
    for line in m.group(1).split("\n"):
        t = line.strip().lstrip("-·• ").strip()
        if not t:
            continue
        mm = READABLE_LINE_RE.match(t)
        if not mm:
            continue
        kind = mm.group(1).strip()
        name = mm.group(2).strip()
        content = mm.group(3).strip()
        if not name or not content:
            continue
        items.append({"kind": kind, "name": name, "content": content})
    clean = (reply[:m.start()] + reply[m.end():]).rstrip()
    return clean, items


STORM_EVENT_RE = re.compile(
    r"【暴雨扰动[:：]?\s*\+?([0-9]+)\s*[·・‧]?\s*([^】]*)】",
)


def extract_clues(reply):
    """提取【线索候选】块。
    每行格式（tag 可选）：- [涌动名] 线索文本 / - 线索文本
    返回 (clean_reply, [{"text": str, "tag": str}, ...])"""
    if not reply:
        return reply, []
    m = CLUE_BLOCK_RE.search(reply)
    if not m:
        return reply, []
    candidates = []
    for line in m.group(1).split("\n"):
        t = line.strip().lstrip("-·• ").strip()
        if not t or len(t) < 4:
            continue
        tag = ""
        mt = re.match(r"^\[([^\]]{1,24})\]\s*(.+)$", t)
        if mt:
            tag = mt.group(1).strip()
            t = mt.group(2).strip()
        if not t:
            continue
        candidates.append({"text": t, "tag": tag})
    clean = (reply[:m.start()] + reply[m.end():]).rstrip()
    return clean, candidates


def extract_storm_event(reply):
    """从叙事者回复中提取【暴雨扰动：+N · 原因】块。
    返回 (clean_reply, {"delta": int, "reason": str} | None)"""
    if not reply:
        return reply, None
    m = STORM_EVENT_RE.search(reply)
    if not m:
        return reply, None
    try:
        delta = int(m.group(1))
    except (TypeError, ValueError):
        delta = 0
    delta = max(0, min(30, delta))
    reason = (m.group(2) or "").strip() or "叙事事件"
    clean = (reply[:m.start()] + reply[m.end():]).rstrip()
    return clean, {"delta": delta, "reason": reason}


_RICH_TAG_RE = re.compile(
    r"</?(?:size|color|b|i|u|s|em|strong|br|p|div|span|align|indent)"
    r"(?:=[^>]*)?\s*/?>",
    re.IGNORECASE,
)
_ANY_ANGLE_TAG_RE = re.compile(r"</?[a-zA-Z][^>]{0,40}>")


def strip_rich_tags(text):
    if not text:
        return text
    s = _RICH_TAG_RE.sub("", text)
    s = _ANY_ANGLE_TAG_RE.sub("", s)
    return s.strip()


_VERTIN_HINTS = ("维尔汀", "Vertin")


def _mentions_vertin(text):
    return any(h in (text or "") for h in _VERTIN_HINTS)


def format_lore_block(snippets):
    if not snippets:
        return ""
    normal, vertin = [], []
    for s in snippets:
        (vertin if _mentions_vertin(s) else normal).append(s)
    parts = []
    if normal:
        parts.append(
            "[档案·环境参考]\n"
            "下列片段来自官方剧情库，可作氛围、路人、组织背景取材。\n"
            + "\n".join(f"- {x}" for x in normal)
        )
    if vertin:
        parts.append(
            "[档案·维尔汀 · 不同时间线的她 · 绝对不可合并]\n"
            "下列每一条都来自**另一条时间线**上、**另一个时刻**的她。\n"
            "不要把它们视作同一场景、同一时刻、同一版本的维尔汀。\n"
            "不要让她基于这些片段「同时出现」在玩家身边两处。\n"
            + "\n".join(f"- [异时间线档案] {x}" for x in vertin)
        )
    return "\n\n".join(parts)


# ===========================================================================
# 随机种子
# ===========================================================================

HAIR_SEEDS = [
    "浅亚麻色短卷发", "深栗色长直发", "红铜色蓬松卷发",
    "乌黑利落短发", "灰金色长发松散", "暖棕色编成一条发辫",
    "浅蜜色低马尾", "黑得泛靛蓝的中长发", "被日光洗褪的浅褐短发",
    "深棕色波波头", "银灰色碎剪短发", "暗红色及肩卷发",
]
EYE_SEEDS = [
    "琥珀色", "灰绿色", "深棕色", "浅褐色", "蓝灰色",
    "榛色", "紫灰色", "蜜金色", "近乎黑色的深褐",
    "淡青灰色", "铜褐色", "海蓝色",
]
SKIN_SEEDS = [
    "暖白", "橄榄色", "浅蜜色", "玫瑰调", "麦色",
    "深棕", "古铜", "被日晒过的浅棕", "带着雀斑的浅色",
    "冷瓷色", "象牙色",
]
BUILD_SEEDS = [
    "身形清瘦，肩背挺直", "身形高挑，步态轻盈",
    "身形小巧，动作利落", "身形匀称，姿态放松",
    "身形纤细，腰线明显", "身形修长，肩宽略窄",
    "体格健硕但收束", "骨架小巧，轮廓利落",
]
TONE_SEEDS = [
    "冷艳而疏离", "明快而锐利", "沉静温和", "鲜活灵动",
    "少年般的中性", "隽永幽深", "慵懒优雅",
    "旧日贵族式的倦怠", "野性未驯", "病态而干净",
    "温润而沉静", "锋芒不掩的安静",
]
DRESS_SEEDS = [
    "常穿旧剪裁的深色西装", "爱穿浅色呢子大衣",
    "常穿深色高领毛衣", "偏爱灰蓝色的长外套",
    "总是一件洗得发白的衬衫配马甲", "常穿无袖连衣裙外披风衣",
    "惯穿学院风西装马甲", "常穿旧式绒面猎装",
    "偏爱粗花呢三件套", "常穿宽松的工装外套",
    "惯披一件半旧的开司米披肩", "总穿深色长裙配皮靴",
]
INCANTATION_THEMES = [
    "沉默 / 言语 / 名字", "时间 / 时针 / 记忆",
    "灰烬 / 火焰 / 光", "影子 / 暮色 / 夜空",
    "雨 / 水 / 潮汐", "门 / 钥匙 / 边界",
    "呼吸 / 心跳 / 血", "镜 / 倒影 / 另一个",
    "契约 / 誓言 / 信物", "针线 / 缝合 / 断裂",
    "钟声 / 回声 / 静默", "风 / 尘土 / 归处",
]


def roll_appearance_seed():
    return {
        "hair": random.choice(HAIR_SEEDS),
        "eye": random.choice(EYE_SEEDS),
        "skin": random.choice(SKIN_SEEDS),
        "build": random.choice(BUILD_SEEDS),
        "tone": random.choice(TONE_SEEDS),
        "dress": random.choice(DRESS_SEEDS),
    }


def roll_incantation_theme():
    return random.choice(INCANTATION_THEMES)


# ===========================================================================
# 剧情库
# ===========================================================================

DEFAULT_KEYWORDS = ["维尔汀", "圣洛夫", "暴雨", "神秘学家", "基金会",
                    "拉普拉斯", "马戏团", "时代"]


class LoreDB:
    def __init__(self, db_path):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"剧情库不存在: {db_path}")
        self.lock = threading.RLock()
        uri = f"file:{db_path}?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.OperationalError:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._stats = None
        self._phases = None
        self._arcs = None
        self._chapters = None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def stats(self):
        if self._stats is not None:
            return self._stats
        with self.lock:
            if self._stats is not None:
                return self._stats
            try:
                c = self.conn.cursor()
                self._stats = {
                    "episodes": c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                    "lines": c.execute("SELECT COUNT(*) FROM lines").fetchone()[0],
                    "chapters": c.execute(
                        "SELECT COUNT(DISTINCT chapter_title) FROM episodes"
                    ).fetchone()[0],
                    "categories": c.execute(
                        "SELECT COUNT(DISTINCT category_title) FROM episodes"
                    ).fetchone()[0],
                }
            except sqlite3.OperationalError:
                self._stats = {"episodes": 0, "lines": 0,
                               "chapters": 0, "categories": 0}
        return self._stats

    def phases(self):
        if self._phases is not None:
            return self._phases
        with self.lock:
            if self._phases is not None:
                return self._phases
            try:
                rows = self.conn.execute("""
                    SELECT phase, COUNT(*) AS n, MIN(order_key) AS k
                    FROM timeline GROUP BY phase ORDER BY k
                """).fetchall()
                self._phases = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                self._phases = []
        return self._phases

    def arcs(self):
        if self._arcs is not None:
            return self._arcs
        with self.lock:
            if self._arcs is not None:
                return self._arcs
            try:
                rows = self.conn.execute("""
                    SELECT arc, arc_kind, COUNT(*) AS n
                    FROM timeline GROUP BY arc, arc_kind
                    ORDER BY MIN(order_key)
                """).fetchall()
                self._arcs = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                self._arcs = []
        return self._arcs

    def chapters(self):
        if self._chapters is not None:
            return self._chapters
        with self.lock:
            if self._chapters is not None:
                return self._chapters
            try:
                rows = self.conn.execute("""
                    SELECT chapter_title AS chapter,
                           COUNT(*) AS n,
                           SUM(line_count) AS line_total
                    FROM episodes
                    WHERE chapter_title IS NOT NULL AND chapter_title <> ''
                    GROUP BY chapter_title
                    ORDER BY MIN(rowid)
                """).fetchall()
                self._chapters = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                self._chapters = []
        return self._chapters

    def search(self, keywords, per_kw=6, min_len=6):
        out, seen = [], set()
        with self.lock:
            cur = self.conn.cursor()
            for kw in keywords:
                kw = (kw or "").strip()
                if not kw:
                    continue
                try:
                    rows = cur.execute("""
                        SELECT e.chapter_title AS ch, e.title_zh AS ep,
                               l.speaker_zh AS spk, l.zh AS zh
                        FROM lines l JOIN episodes e ON e.id = l.episode_id
                        WHERE l.zh LIKE ? AND length(l.zh) >= ?
                        ORDER BY RANDOM() LIMIT ?
                    """, (f"%{kw}%", min_len, per_kw)).fetchall()
                except sqlite3.OperationalError:
                    continue
                for r in rows:
                    key = (r["zh"] or "")[:40]
                    if key in seen:
                        continue
                    seen.add(key)
                    spk = (r["spk"] or "").strip() or "旁白"
                    ch = (r["ch"] or "").strip()
                    ep = (r["ep"] or "").strip()
                    zh = strip_rich_tags((r["zh"] or "").strip())
                    if not zh:
                        continue
                    prefix = " / ".join(p for p in (ch, ep) if p)
                    out.append(f"[{prefix}] {spk}：{zh}" if prefix else f"{spk}：{zh}")
        return out[:30]

    def random_chapters(self, n=6):
        with self.lock:
            try:
                rows = self.conn.execute("""
                    SELECT DISTINCT chapter_title FROM episodes
                    WHERE chapter_title IS NOT NULL AND chapter_title <> ''
                    ORDER BY RANDOM() LIMIT ?
                """, (n,)).fetchall()
                return [r["chapter_title"] for r in rows]
            except sqlite3.OperationalError:
                return []

    def all_episode_titles(self, limit=200):
        with self.lock:
            try:
                rows = self.conn.execute("""
                    SELECT t.order_key, t.phase, t.arc, e.id,
                           e.title_zh AS title, e.chapter_title AS chapter
                    FROM timeline t JOIN episodes e ON e.id = t.episode_id
                    ORDER BY t.order_key LIMIT ?
                """, (limit,)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    def episodes_in_phase(self, phase, limit=80):
        with self.lock:
            try:
                rows = self.conn.execute("""
                    SELECT t.episode_id AS id, t.order_key, t.phase, t.arc,
                           e.title_zh AS title, e.chapter_title AS chapter,
                           e.line_count
                    FROM timeline t JOIN episodes e ON e.id = t.episode_id
                    WHERE t.phase = ? ORDER BY t.order_key LIMIT ?
                """, (phase, limit)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    def episodes_in_arc(self, arc_name, limit=60):
        with self.lock:
            try:
                rows = self.conn.execute("""
                    SELECT t.episode_id AS id, t.order_key, t.phase, t.arc,
                           e.title_zh AS title, e.chapter_title AS chapter,
                           e.line_count
                    FROM timeline t JOIN episodes e ON e.id = t.episode_id
                    WHERE t.arc = ? ORDER BY t.order_key LIMIT ?
                """, (arc_name, limit)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    def _lines_for_episode_locked(self, episode_id):
        try:
            return self.conn.execute("""
                SELECT seq, speaker_zh, zh FROM lines
                WHERE episode_id = ? ORDER BY seq
            """, (episode_id,)).fetchall()
        except sqlite3.OperationalError:
            return []

    def phase_outline(self, phase, max_lines=6):
        eps = self.episodes_in_phase(phase)
        if not eps:
            return "", []
        chunks = []
        for ep in eps:
            with self.lock:
                rows = self._lines_for_episode_locked(ep["id"])
            if not rows:
                continue
            head = rows[:max_lines]
            tail = rows[-max_lines:] if len(rows) > max_lines * 2 else []
            lines = []
            for r in head:
                spk = (r["speaker_zh"] or "").strip() or "旁白"
                zh = strip_rich_tags((r["zh"] or "").strip())
                if zh:
                    lines.append(f"  {spk}：{zh}")
            if tail:
                lines.append("  ……")
                for r in tail:
                    spk = (r["speaker_zh"] or "").strip() or "旁白"
                    zh = strip_rich_tags((r["zh"] or "").strip())
                    if zh:
                        lines.append(f"  {spk}：{zh}")
            chunks.append(f"◈ [{ep['chapter']}] {ep['title']}\n" + "\n".join(lines))
        return "\n\n".join(chunks), eps

    def arc_outline(self, arc_name, max_lines=5):
        eps = self.episodes_in_arc(arc_name)
        if not eps:
            return "", []
        chunks = []
        for ep in eps:
            with self.lock:
                rows = self._lines_for_episode_locked(ep["id"])
            if not rows:
                continue
            lines = []
            for r in rows[:max_lines]:
                spk = (r["speaker_zh"] or "").strip() or "旁白"
                zh = strip_rich_tags((r["zh"] or "").strip())
                if zh:
                    lines.append(f"  {spk}：{zh}")
            chunks.append(f"◈ [{ep['phase']}] {ep['title']}\n" + "\n".join(lines))
        return "\n\n".join(chunks), eps


# ===========================================================================
# 故事线图谱（读 rain1999_uttu_bridge.py 写入的 uttu_* 表）
# ===========================================================================

class StorylineDB:
    def __init__(self, db_path):
        self.lock = threading.RLock()
        uri = f"file:{db_path}?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.OperationalError:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._tables = None
        self._stats = None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def tables(self):
        if self._tables is not None:
            return self._tables
        with self.lock:
            if self._tables is not None:
                return self._tables
            try:
                rows = self.conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'uttu\\_%' ESCAPE '\\'"
                ).fetchall()
                self._tables = [r["name"] for r in rows]
            except sqlite3.OperationalError:
                self._tables = []
        return self._tables

    def has_graph(self):
        with self.lock:
            for t in self.tables():
                try:
                    n = self.conn.execute(
                        f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    if n > 0:
                        return True
                except sqlite3.OperationalError:
                    continue
        return False

    def stats(self):
        if self._stats is not None:
            return self._stats
        out = {}
        with self.lock:
            for t in self.tables():
                try:
                    n = self.conn.execute(
                        f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    out[t] = n
                except sqlite3.OperationalError:
                    continue
        self._stats = out
        return out

    def _pick_col(self, table, candidates):
        try:
            cols = [r["name"] for r in self.conn.execute(
                f'PRAGMA table_info("{table}")').fetchall()]
        except sqlite3.OperationalError:
            return None
        for c in candidates:
            if c in cols:
                return c
        return None

    def storyline_nodes(self, limit=200):
        out = []
        with self.lock:
            for t in self.tables():
                name_col = self._pick_col(t, ["name", "title", "label", "node"])
                kind_col = self._pick_col(t, ["kind", "type", "category"])
                year_col = self._pick_col(t, ["year", "era_year", "anchor_year"])
                if not name_col:
                    continue
                try:
                    cols = [name_col]
                    if kind_col: cols.append(kind_col)
                    if year_col: cols.append(year_col)
                    sel = ", ".join(f'"{c}"' for c in cols)
                    rows = self.conn.execute(
                        f'SELECT {sel} FROM "{t}" '
                        f'ORDER BY rowid LIMIT ?', (limit,)
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for r in rows:
                    d = {"_table": t, "name": r[name_col]}
                    if kind_col: d["kind"] = r[kind_col]
                    if year_col:
                        try:
                            d["year"] = int(r[year_col])
                        except (TypeError, ValueError):
                            d["year"] = r[year_col]
                    out.append(d)
        return out

    def context_block(self, max_rows=12):
        nodes = self.storyline_nodes(limit=max_rows)
        if not nodes:
            return ""
        lines = []
        for n in nodes:
            bits = []
            if n.get("kind"):
                bits.append(f"({n['kind']})")
            if n.get("year"):
                bits.append(f"{n['year']}")
            prefix = " ".join(bits)
            lines.append(f"- {prefix} {n['name']}".strip())
        return "[故事线图谱 · 官方时间轴]\n" + "\n".join(lines)


# ===========================================================================
# 暴雨 · 时代病（v3.1：动态回退 + 官方时段吸附）
# ===========================================================================

# 超阈值 → 严重程度分级
# (overshoot_lower_bound, 等级名, 回退年数下限, 回退年数上限)
ROLLBACK_TIERS = [
    (0,   "微溃",  2,   8),
    (5,   "失序",  5,   18),
    (15,  "崩解",  12,  35),
    (30,  "溃灭",  25,  70),
    (50,  "归墟",  50,  150),
]

# 吸附的容差范围：官方阶段起点与原始计算目标相差不超过这个年数才吸附
SNAP_TOLERANCE = 30


class StormState:
    """
    「暴雨」会抹除历史。每经历一次时代，都会累积「时代病」。

    病害阈值：
      · ELEVATED_AT   20  → 微澜
      · WARN_AT       60  → 暴雨将至
      · CRITICAL_AT   85  → 崩坏边缘
      · ROLLBACK_AT  100  → 触发回退

    回退幅度不固定：
      超额 = level - 100
      按 ROLLBACK_TIERS 分级，超额越大 → 回退越深。
      计算结果自动吸附到官方时段（TimelineDB）。
    回退后残留 RESIDUAL（= 25），多次回退叠加。
    """

    THRESHOLD    = 100
    ELEVATED_AT  = 20
    WARN_AT      = 60
    CRITICAL_AT  = 85
    ROLLBACK_AT  = 100
    RESIDUAL     = 25

    TICK_CHAT       = 2
    TICK_NEXTDAY    = 1
    TICK_NEXTYEAR   = 8
    CLOCK_PER_YEAR  = 0.6
    CLOCK_CAP       = 35

    def __init__(self):
        self.eras = {}
        self.current_era = None
        self.log = []
        self.rollbacks = []

    def ensure_era(self, code, name=None, years=None):
        if code not in self.eras:
            self.eras[code] = {
                "level": 0,
                "name": name or code,
                "years": years or [None, None],
            }
        return self.eras[code]

    def get(self, code):
        return self.eras.get(code)

    def current(self):
        return self.eras.get(self.current_era)

    def set_current(self, code, name=None, years=None):
        self.ensure_era(code, name, years)
        self.current_era = code

    def add(self, code, delta, reason="", name=None, years=None):
        era = self.ensure_era(code, name, years)
        old = era["level"]
        era["level"] = max(0, min(self.THRESHOLD, era["level"] + int(delta)))
        new = era["level"]
        if new == old:
            return era
        self.log.append({
            "era": code,
            "era_name": era["name"],
            "from": old,
            "to": new,
            "delta": new - old,
            "reason": reason,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        })
        self.log = self.log[-300:]

        # 实时日志
        sev = self.severity(code)
        lv = "debug" if abs(new - old) < 2 else "info"
        if sev in ("warning", "critical"):
            lv = "warn"
        bar, _ = self.progress_bar(code)
        log("storm", f"病害 {old:>3} → {new:>3}  [{bar}]  {self.label(code)}",
            level=lv, era=era["name"], delta=f"{new-old:+d}",
            reason=reason[:40])

        for thr, tag in ((20, "微澜"), (60, "暴雨将至"), (85, "崩坏边缘")):
            if old < thr <= new:
                log("storm", f"⚡ 越过阈值 {thr} · {tag}",
                    level="warn", era=era["name"])
        return era

    def severity(self, code=None):
        era = self.eras.get(code or self.current_era)
        if not era:
            return "calm"
        v = era["level"]
        if v >= self.CRITICAL_AT: return "critical"
        if v >= self.WARN_AT:     return "warning"
        if v >= self.ELEVATED_AT: return "elevated"
        return "calm"

    def label(self, code=None):
        return {
            "calm":     "平静",
            "elevated": "微澜",
            "warning":  "暴雨将至",
            "critical": "崩坏边缘",
            "rollback": "时代回退",
        }.get(self.severity(code), "平静")

    def progress_bar(self, code=None, width=12):
        era = self.eras.get(code or self.current_era)
        v = era["level"] if era else 0
        filled = int(round(v / self.THRESHOLD * width))
        return "█" * filled + "░" * (width - filled), v

    def to_dict(self):
        return {
            "eras": self.eras,
            "current_era": self.current_era,
            "log": self.log[-200:],
            "rollbacks": self.rollbacks[-50:],
        }

    @classmethod
    def from_dict(cls, d):
        o = cls()
        if not d:
            return o
        o.eras = d.get("eras", {}) or {}
        o.current_era = d.get("current_era")
        o.log = d.get("log", []) or []
        o.rollbacks = d.get("rollbacks", []) or []
        return o

    def narrator_context(self, code=None):
        era = self.eras.get(code or self.current_era)
        if not era:
            return ""
        sev = self.severity(code)
        return (
            f"[系统 · 暴雨时代病]\n"
            f"时代：{era['name']}（code={code or self.current_era}）\n"
            f"病害程度：{era['level']} / {self.THRESHOLD} —— {self.label(code)}\n"
            f"叙事提示：\n"
            f"  · 平静：正常写，暴雨只是天气。\n"
            f"  · 微澜：偶尔出现被擦除的字迹、认不出的人、反复的名字。\n"
            f"  · 暴雨将至：城市层面开始崩坏——街道消失、报纸变成空白、"
            f"路人失去面孔。人物仍可对话但会忘事。\n"
            f"  · 崩坏边缘：场景、人物、时间自相矛盾。"
            f"玩家会看到「同一句话由不同的人说出」「一个地点在两个地方」。\n"
            f"不要直接说「病害程度」，让世界自己崩。"
        )


# ===========================================================================
# 出生年 · 暴雨时间轴对接（AI 接管年份核对）
# ===========================================================================
#
# 规则：
#   · 每次输入出生年份，都先由 AI 档案员对照 index.json 暴雨时间轴
#     复述确认这一年，再进入下一步。
#   · 若出生年恰好是某场暴雨的回溯落点（如 1996 = 第一次暴雨落点），
#     这一年存在「暴雨之前 / 暴雨之后」两个版本，AI 必须追问玩家
#     选择哪一个，再据此锚定剧情。
#   · 维尔汀（Vertin）是女性，约 1991 年出生；所有核对文本以「她」指代。

VERTIN_BIRTH_YEAR = 1991          # 官方考据：约 1991 年出生
VERTIN_GENDER = "女"

BIRTH_TIMELINE_JSON_CANDIDATES = [
    os.path.join(_HERE, "rain1999", "data", "timeline", "index.json"),
    os.path.join(_HERE, "rain1999", "data", "index.json"),
    os.path.join(_HERE, "index.json"),
    os.path.join(".", "rain1999", "data", "timeline", "index.json"),
    os.path.join(".", "index.json"),
]


class BirthTimeline:
    """读取《重返未来：1999》暴雨时间轴 index.json（结构化 2.0）。"""

    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.path = path
        self.storms = data.get("暴雨", []) or []
        self.periods = data.get("时期", []) or []
        self.storm_by_code = {s.get("代码"): s for s in self.storms}
        self.period_by_code = {p.get("代码"): p for p in self.periods}

    @classmethod
    def locate(cls, explicit=None):
        cands = []
        if explicit:
            cands.append(explicit)
        env_p = os.environ.get("RAIN_TIMELINE_JSON")
        if env_p:
            cands.append(env_p)
        cands.extend(BIRTH_TIMELINE_JSON_CANDIDATES)
        for p in cands:
            if p and os.path.exists(p):
                try:
                    return cls(p)
                except Exception:
                    continue
        return None

    def periods_for_year(self, year):
        out = []
        for p in self.periods:
            span = p.get("外界年代") or {}
            try:
                a, b = int(span.get("起始")), int(span.get("结束"))
            except (TypeError, ValueError):
                continue
            if a <= year <= b:
                out.append(p)
        return out

    def storms_landing_on(self, year):
        out = []
        for s in self.storms:
            rb = s.get("回溯") or {}
            try:
                if int(rb.get("落点")) == year:
                    out.append(s)
            except (TypeError, ValueError):
                continue
        return out

    def ambiguity(self, year):
        """
        该出生年是否需要区分「暴雨之前 / 暴雨之后」。
        条件：年份落在 ≥2 个平稳期，且是某场暴雨的回溯落点。
        1996 年 → 第一次暴雨（1999 → 1996 回溯落点）。
        同年多场暴雨落点（如 1935）时取叙事靠后的一场。
        返回 (needs_choice, storm | None)。
        """
        periods = self.periods_for_year(year)
        if len(periods) < 2:
            return False, None
        landings = self.storms_landing_on(year)
        if not landings:
            return False, None
        codes = {p.get("代码") for p in periods}
        picked = None
        for s in landings:
            if s.get("前接时期") in codes and s.get("后接时期") in codes:
                picked = s      # 暴雨紧邻的两个时期都命中，最精确
        if picked is None:
            picked = landings[-1]
        return True, picked

    def vertin_note(self, year, period=None):
        if period and period.get("维尔汀年龄"):
            return f"维尔汀（她）在这一时期{period['维尔汀年龄']}"
        if year < VERTIN_BIRTH_YEAR:
            return (f"维尔汀（她）约 {VERTIN_BIRTH_YEAR} 年才出生，"
                    f"{year} 年的世界上还没有她")
        return f"维尔汀（她）在 {year} 年约 {year - VERTIN_BIRTH_YEAR} 岁"

    def events_for_year(self, year, limit=3):
        seen, out = set(), []
        for p in self.periods_for_year(year):
            for e in p.get("事件") or []:
                t = str(e.get("时间") or "")
                if str(year) not in t:
                    continue
                line = f"{t} · {e.get('内容', '')}"
                if line not in seen:
                    seen.add(line)
                    out.append(line)
        return out[:limit]

    def facts_block(self, year):
        periods = self.periods_for_year(year)
        ambiguous, storm = self.ambiguity(year)
        lines = []
        if not periods:
            lines.append(f"{year} 年不在时间轴任何已知平稳期内")
        for p in periods:
            span = p.get("外界年代") or {}
            lines.append(
                f"平稳期「{p.get('标题', '?')}」：外界 "
                f"{span.get('起始显示', '?')}—{span.get('结束显示', '?')}；"
                f"维尔汀年龄：{p.get('维尔汀年龄', '不明')}")
        if ambiguous and storm:
            rb = storm.get("回溯") or {}
            lines.append(
                f"★ {year} 年是{storm.get('名称')}的回溯落点"
                f"（{rb.get('起点显示', '?')} → {rb.get('落点显示', '?')}）："
                f"这一年存在暴雨之前（旧时间线）与暴雨之后（回溯后）两个版本")
        evs = self.events_for_year(year)
        if evs:
            lines.append("同年剧情事件：" + "；".join(evs))
        return "\n".join(lines)

    def context_line(self, year, side=None, storm=None):
        """写入角色档案的事实锚定（非 AI 措辞，保证年份与事件准确）。"""
        ambiguous, amb_storm = self.ambiguity(year)
        storm = storm or amb_storm
        periods = self.periods_for_year(year)
        title = periods[-1].get("标题") if periods else "时间轴之外的年代"
        parts = [f"出生年 {year} · {title}"]
        if ambiguous and storm:
            nm = storm.get("名称", "暴雨")
            rb = storm.get("回溯") or {}
            if side == "before":
                parts.append(f"出生于{nm}之前的 {year}（暴雨未至的旧时间线）")
            elif side == "after":
                parts.append(
                    f"出生于{nm}之后的 {year}（世界从 "
                    f"{rb.get('起点显示', '?')} 被回溯到这一年）")
            else:
                parts.append(f"{year} 是{nm}的回溯落点，暴雨前后版本待定")
        last = periods[-1] if periods else None
        parts.append(self.vertin_note(year, last))
        evs = self.events_for_year(year, limit=2)
        if evs:
            parts.append("同年事件：" + "；".join(evs))
        return "\n".join(parts)


BIRTH_REVIEW_PROMPT = """你是雨幕档案馆的 AI 档案员，正在为一个新生命核对出生年份。
口吻克制、英伦、略带冷幽默，像一位经历过时间灾害的记录员。中文。

【铁律事实（取自官方暴雨时间轴，不得改动、不得编造年份与事件）】
- 维尔汀（Vertin）是女性，约 1991 年出生；提起她只能用「她」。
- 第一次暴雨于 1999 年末降临，将世界回溯至 1996 年。
- 玩家申报的出生年：{year} 年。

【时间轴档案】
{facts}

【你的任务】
{task}

【只输出 JSON】
{{"say": "对玩家说的话（80-160 字）", "meaning": "一句话剧情含义"}}
"""


def _birth_review_fallback(year, ambiguous, storm, bt):
    """LLM 不可用时的内置档案员措辞（仍基于时间轴事实，措辞固定）。"""
    if ambiguous and storm:
        nm = storm.get("名称", "暴雨")
        rb = storm.get("回溯") or {}
        say = (
            f"{year} 年……有意思。{nm}在 {rb.get('起点显示', '?')} 降临，"
            f"把世界抹回了这一年。于是档案里有两个 {year}："
            f"一个是{nm}之前、尚未被触碰的旧时间线；"
            f"一个是暴雨之后、被回溯出来的版本。\n"
            f"告诉我——你出生在{nm}之前，还是之后？"
        )
        return say, f"{year} 为{nm}回溯落点，暴雨前后待定"
    bits = [f"{year} 年。"]
    if bt:
        periods = bt.periods_for_year(year)
        if periods:
            bits.append(f"那是「{periods[-1].get('标题')}」。")
        else:
            bits.append("那一年不在任何已知平稳期内。")
        bits.append(bt.vertin_note(year, periods[-1] if periods else None) + "。")
        evs = bt.events_for_year(year, limit=1)
        if evs:
            bits.append(f"同年档案里记着：{evs[0]}。")
    bits.append("确认以这一年作为出生年吗？")
    meaning = (bt.context_line(year).split("\n")[0] if bt else f"{year} 年")
    return "".join(bits), meaning


BIRTH_TIMELINE = BirthTimeline.locate()


# ===========================================================================
# 角色
# ===========================================================================

def roll_origin():
    r = random.random()
    if r < 0.55:
        return "平淡", (
            "父母健在，家庭体面或普通，日子过得有规律。"
            "没有大起大落，也没有创伤。可以有小小的烦恼，但不悲惨。"
        )
    elif r < 0.85:
        return "微惨", (
            "有点难，但不到惨。比如家道中落、亲人常年病着、"
            "单亲、搬家频繁。不要写死亡、孤儿、虐待、饥荒这类重手。"
        )
    elif r < 0.97:
        return "富裕", (
            "家境不错。旧贵族旁支、殖民地归侨、商人、学者、医生、"
            "律师这类。生活优裕但有规矩。"
        )
    else:
        return "凄惨", (
            "极少数情况下才出现的重手。真有创伤，真有失去。"
            "用留白和克制处理，不要渲染苦难。"
        )


IDENTITY_PROMPT = """你是《重返未来：1999》世界观的档案编纂员。
请为下面这位神秘学家生成一份完整、好看、不重复的身份档案。

【玩家已确定的信息】
姓名：{name}
性别：{gender}
出生年份：{birth_year} 年
故事起始年份：{start_year} 年（= 出生年，玩家从 0 岁开始）
神秘术名：{ability_name}
神秘术描述：{ability_desc}

【玩家额外指定 · 必须严格遵守 · 违反即作废】
出生地：{birthplace_hint}
与维尔汀的关系：{relation_hint}

（若上面两项写着"（由你决定）"，则按你的判断自由生成；
 否则必须原样采用玩家指定的内容，不得擅自更改城市/国家/关系。）

【出生年 · 剧情锚定（取自官方暴雨时间轴，必须与之自洽）】
{birth_anchor}

【本次出身方向：{origin_kind}】
{origin_detail}

■ birthplace / birthplace_note（出生地）
  **如果玩家在【玩家额外指定】里给出了出生地，必须原样采用，
  不得改成别的城市/国家。** birthplace_note 用一句话描述此地。
  若玩家没指定，则自由生成——可以是中国、英国、欧洲大陆、
  美洲、殖民地，但必须与角色卡其他字段（血统、家庭、时代）自洽。
  **不要默认爱丁堡/伦敦**——那是原著场景，不是玩家舞台。

■ bloodline（血统）
  只写"血统"本身：纯度 + 来源。
  纯度只有两种合法写法：
    · "纯血" —— 父母双方均为登记在册的神秘学家。
    · "半血" —— 父母中只有一方是神秘学家。
  后面补一句来源，一句话写清。

■ family（家庭）
  只写家庭本身：父母情况、兄弟姐妹、家境。
  不要写"晚饭总有汤"这类无关细节。

■ personality（性格）
  只写性格，不写习惯、不写动作、不写口头禅。
  注意：玩家从 0 岁开始，性格可以是"尚未定型"或"隐约可辨"。

■ appearance（外貌）
  只写静态外形：五官、发色、肤色、瞳色、身形、衣着。
  玩家现在是 0 岁，写"婴儿期的眉目特征"＋"长大后的隐约预告"。
  **好看**。禁止写牙、皮肤、头面、气味、体态的丑化细节。
  本次随机方向（仅供参考，不要照抄）：
    头发：{hair_hint}  眼睛：{eye_hint}  肤色：{skin_hint}
    身形：{build_hint}  着装：{dress_hint}  基调：{tone_hint}

■ affiliation（隶属组织 / 身份）
  0 岁时可以写"（尚未登记）"或家族的名义归属。

■ code_name（编号）形如 "X-123"

■ vertin_offset（与维尔汀的时间线）
  必须三选一：早于维尔汀 / 与维尔汀同代 / 晚于维尔汀。
  **禁止** ±N 岁这类模糊表达。
  维尔汀是女性，约 1991 年出生；判断世代以她与上面的剧情锚定为准。

■ relation（与维尔汀的关系）
  一到两句。
  **如果玩家在【玩家额外指定】里写明了关系（例如"我和维尔汀
  是亲姐妹"），必须原样采用，不得改成"尚未见过"或别的。**
  若玩家没指定，玩家 0 岁时通常是"尚未见过"。
  注意：若指定为"亲姐妹"，则 vertin_offset 必须是
  "与维尔汀同代"，且 birthplace 应与维尔汀同源，
  两人年龄差不超过 5 岁。

■ ability_incantation（神秘术咒语）
  格式：英文原文（或拉丁语） + 中文诗意译文
  例：「By silence kept, be still.」——「以所守之默，令汝静。」
  本次主题方向：{incantation_theme}
  必须艺术、不能直白、与神秘术自洽。

【档案风格】
英伦，二十世纪中叶以前。用词克制，略带冷幽默。
避免现代网络用语、日系中二感。

【只输出 JSON】
{{
  "birthplace": "具体到城市 · 街区/地标",
  "birthplace_note": "一句话描述此地",
  "bloodline": "纯血 / 半血 / 来历不明，+ 一句来源",
  "family": "一句话家庭本身",
  "personality": "一句话性格本身（0 岁可以是尚待成形）",
  "appearance": "一句话静态外貌（含婴儿期特征）",
  "affiliation": "所属组织 / 身份",
  "code_name": "X-123",
  "vertin_offset": "早于维尔汀 / 与维尔汀同代 / 晚于维尔汀，+ 一句描述",
  "relation": "与维尔汀的关系，一到两句",
  "ability_incantation": "「English original.」——「中文诗意译文。」"
}}
"""


IDENTITY_CUSTOM_FIELDS = [
    "birthplace", "birthplace_note", "bloodline", "family", "personality",
    "appearance", "affiliation", "code_name", "vertin_offset", "relation",
    "ability_incantation",
]


def generate_identity(name, gender, ability_name, ability_desc,
                      birth_year, start_year, birth_anchor="", custom=None):
    """生成身份档案。custom 中玩家填写的字段优先；留空的由 AI 编写。
    全部字段都已填写时跳过 AI 调用（离线也可用）。"""
    custom = {k: str(v).strip() for k, v in (custom or {}).items()
              if k in IDENTITY_CUSTOM_FIELDS and str(v or "").strip()}
    if len(custom) == len(IDENTITY_CUSTOM_FIELDS):
        log("game", "身份档案全部字段由玩家自定义，跳过 AI 编写", level="info")
        return dict(custom)

    origin_kind, origin_detail = roll_origin()
    seed = roll_appearance_seed()
    incant_theme = roll_incantation_theme()

    prompt = IDENTITY_PROMPT.format(
        name=name, gender=gender,
        birth_year=birth_year, start_year=start_year,
        ability_name=ability_name, ability_desc=ability_desc,
        birth_anchor=birth_anchor or "（出生年未经 AI 档案员核对）",
        origin_kind=origin_kind, origin_detail=origin_detail,
        hair_hint=seed["hair"], eye_hint=seed["eye"], skin_hint=seed["skin"],
        build_hint=seed["build"], dress_hint=seed["dress"],
        tone_hint=seed["tone"],
        incantation_theme=incant_theme,
        birthplace_hint=(locals().get("birthplace_hint") or locals().get("birth_side") or locals().get("birthplace") or "（由你决定）"),
        relation_hint=(locals().get("relation_hint") or locals().get("birth_context") or locals().get("relation") or "（由你决定）"),
    )
    try:
        text = llm_chat([{"role": "user", "content": prompt}],
                        temperature=ARCHIVIST_TEMP)
        identity = extract_json(text)
    except Exception:
        identity = {
            "birthplace": "英国伦敦 · 苏活区",
            "birthplace_note": "夜里能闻到隔壁咖啡馆烘豆子的味道。",
            "bloodline": "纯血。母系三代均在圣洛夫基金会名册上。",
            "family": "父母经营一间小书店，家中有一个姐姐。",
            "personality": "婴儿期的她安静得出奇，眼神像在辨认什么。",
            "appearance": "出生时浅亚麻色胎发，琥珀色眼睛；"
                          "医生说她长大后身形会清瘦。",
            "affiliation": "（尚未登记）",
            "code_name": "X-317",
            "vertin_offset": "与维尔汀同代。两人年龄相仿。",
            "relation": "尚未见过她。这个名字还没有进入你的耳朵。",
            "ability_incantation":
                "「By silence kept, be still.」——「以所守之默，令汝静。」",
        }
    # 玩家自定义字段优先，覆盖 AI 编写结果
    for k, v in custom.items():
        identity[k] = v
    return identity


class Arcanist:
    def __init__(self, name, gender, ability_name, ability_desc,
                 identity, birth_year, start_year,
                 birth_side="", birth_context=""):
        self.name = name
        self.gender = gender
        self.ability_name = ability_name
        self.ability_desc = ability_desc
        self.ability_incantation = identity.get("ability_incantation", "")
        self.birth_year = birth_year
        self.start_year = start_year
        self.birth_side = birth_side
        self.birth_context = birth_context
        self.birthplace = identity.get("birthplace", "")
        self.birthplace_note = identity.get("birthplace_note", "")
        self.bloodline = identity.get("bloodline", "")
        self.family = identity.get("family", "")
        self.personality = identity.get("personality", "")
        self.appearance = identity.get("appearance", "")
        self.affiliation = identity.get("affiliation", "")
        self.code_name = identity.get("code_name", "")
        self.vertin_offset = identity.get("vertin_offset", "")
        self.relation = identity.get("relation", "")

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        o = cls.__new__(cls)
        o.__dict__.update(d)
        return o


# ===========================================================================
# 叙事者提示词
# ===========================================================================

SYSTEM_RULES = """你是《重返未来：1999》的叙事者（Narrator），
为一名玩家主持沉浸式单人剧情。

【世界·基调】
1999 年，"暴雨"（Storm）自天而降。所过之处，历史被抹除。
胶片、留声机、煤气灯、老报纸、打字机——
二十世纪的表皮之下，神秘学家（Arcanist）与普通人共处。
圣洛夫基金会、满铁研究所、拉普拉斯观测站，各有算盘。

【最重要的一条规则：这个世界是玩家的】
- 平行时间线。原著剧情、人物命运、组织关系都是"可能版本"。
- 玩家说的每一句话、做的每一个动作，都会真的改变世界。
- 玩家偏离原著越远，就让这个世界越陌生。

【场景锚定 · 极重要 · 违反了这场故事就废了】
- 玩家角色卡上的"出生地"就是本故事的**唯一主舞台**。
  例如出生地写"上海·永福里"，那么这场戏就在上海永福里展开，
  石库门、巷子、江边、弄堂——**不是爱丁堡、不是伦敦、不是迪恩村**。
- **绝对禁止**把场景自作主张搬到别的国家/城市。
  除非玩家明确说"我要去 X 地"或角色卡里写明"迁居到 X"，
  否则场景永远锚定在出生地及其周边。
- **绝对禁止**让叙事里的地名、人名、物件突然变成英文/外文。
  档案库里若有英文片段（《1999》原著的氛围参考），
  只能借它**语感/气质**，不能覆盖角色卡。
- 玩家 0–9 岁期间，场景锚定在出生地及周边街区，**不得远行**。
- 若玩家已经成年并主动远行，允许随玩家走。
- 若你不确定玩家在哪，就写"（未记录）"，
  绝不要凭空捏一个地点，更不要捏外国地名。

【暴雨 · 时代病 · 极重要】
暴雨不是天气，是一种"抹除"。每经历一次时代，就累积一分"时代病"。
系统会给出当前时代的**病害程度**，你必须照此写：
- 0–19   平静：暴雨只是天气，偶尔下雨。
- 20–59  微澜：字迹被擦、人名记不住、镜子里的影子有时慢半拍。
- 60–84  暴雨将至：街道、报纸、路人开始失真；
                「昨天还在这条街的店，今天没人记得」。
- 85–99  崩坏边缘：场景、人物、时间自相矛盾；
                同一句话由不同的人说出；地点会重叠。
- 100    时代回退：本时代被抹除，时钟往回拨。
               你要把这一段的骤变写进叙事——不是"发生灾难"，
               而是"这个时代原本就是这样，玩家只是终于看清了"。
**禁止**直接说出"病害程度""severity""level"这类元词汇。
让世界自己崩。

【玩家年龄 · 极重要】
玩家从 0 岁开始。系统会给出当前年龄，请严格按年龄写场面：
- 0–2 岁：玩家是婴儿。只能感知（光、声、气味、温度、怀抱）、
  啼哭、被抱、被安置、入睡。叙事视角应在**外界**——
  父母、家族长辈、产房/育婴室、家里的陈设、来客的谈话。
  玩家无法行动，但可以"看见 / 听见"。不要替婴儿做决定。
- 3–6 岁：幼童。可以走路、说话（简短）、好奇、乱跑、被训。
  但仍以家人陪护为主，不能独自远行。
- 7–12 岁：孩童。可以上学、交朋友、探索街区、接触神秘学迹象。
- 13–17 岁：少年。可以独立外出，接触组织、看到世界的裂缝。
- 18+ 岁：成年。可以自由行动、加入组织、追查谜团。
系统会把年份拨到哪一年，你就在哪一年叙事。年龄不合就别写成年场面。

【人物位置约束】
- 每个角色同一时刻只在一处，不会瞬移。
- 场景转移需要合理的时间与移动过程。
- 上一幕出现过的 NPC，下一幕不应无交代地缺席。
- 档案库里的片段是**引文**，不是此刻的场景。

【维尔汀 · 人物内核】
- 身份：圣洛夫基金会特派调查员、"时代之女"。
- 性别：女。任何场合都以「她」指代，绝不写成男性。
- 性格：沉稳、克制、话少。常年一副疏离的平静。
- 说话方式：短句。不主动展开。偶有干冷的英式冷幽默。
- 她**允许**玩家走上与她相反的路。她不劝，不追。
- 她**不**会轻易说"我相信你""我们一起"这类台词。

【维尔汀 · 位置跟随玩家】
- 维尔汀在玩家**当前所处的位置**（同一城市/同一点），
  或者（更常见）**不在场**——只留传闻、信件、别人的转述。
- 她**不会**同时出现在两个地方。
- 她**不会**凭空出现。
- 玩家 0–6 岁时，维尔汀通常不在场；最多通过长辈、访客、
  旧信件、报纸侧面出现。

【维尔汀 · 时间锚定】
她只按玩家**当前时钟年份**那一版出现。
- 时钟指 1929，就是 1929 年的她。
- 时钟指 1966，就是 1966 年的她。
- 同一位维尔汀不可能同时出现在两个时间点。

【时钟系统】
系统会告诉玩家**当前时钟年份**。你可以自由跨越：
  · 回到过去：玩家可能遇到更年轻的维尔汀；
  · 前往未来：玩家可能遇到更年长、已失踪、已死的维尔汀；
  · 时钟回到出生年：玩家 0 岁，一切尚未发生。
时钟拨动是**系统功能**，叙事者不必解释机制，
只需让世界跟着年份自然变化。

【涌动 · 谜题 · 极重要年龄分层】
玩家 **0–9 岁** 期间，**不得生成、不得推进任何涌动（谜题）**。
  · 这个世界对这个年纪的玩家而言，是"日常 + 隐约的异常"。
  · 只通过旁白、对话、父母长辈的只言片语，
    以及【线索候选】块累积环境细节。
  · **线索候选 ≠ 涌动**：
      - 线索候选是「看到/听到的具体事实」，可以有；
      - 涌动是「需要追踪、需要解开的问题」，不能有。
  · 0–6 岁：线索候选偏重感官、家族陈设、来客、旧物、
    反复出现的一句话、一个物件、一个访客。
  · 7–9 岁：可允许"最轻微的异常感"——比如某件东西的位置变了、
    某个人名被反复提及、某扇门从不打开——但**不要把它们组织成
    可被追问的谜题**。玩家可以问，长辈可以岔开话题或给模糊答复。
  · 不要用"涌动""谜题""任务""线索链"这类词出现在叙事文本里。
  · 也不要让 NPC 主动提起"要不要查一查"这类邀请。

玩家 **满 10 岁之后**，才允许生成涌动。
涌动的题材应源于 0–9 岁阶段玩家曾听到/看到的线索候选——
"谜题是从童年累积的碎片里长出来的"，不是凭空降临。
玩家可以：
  · 主动触碰（走入现场、翻阅文件、与人对话）；
  · 忽略（涌动不会立刻惩罚，但世界会记住）。
无论做或不做，成功或失败，都会影响后续。

【线索 · 输出格式】
每轮如果出现了具体可记的线索（文件编号、一句话、一个物件、
一个细节），在回复末尾、时间锚点之前，另起一块：

【线索候选】
- [涌动名] 与该涌动相关的线索（15–40 字，具体事实）
- [涌动名] …
- 未与任何涌动相关的环境碎片

规则：
- 0–3 条，宁少勿多。
- 每条必须是**可被验证或追踪**的具体事实，不是感受。
- **方括号里的「涌动名」必须与【当前涌动】列表里的 name
  完全一致**（不要用 s1 / s2 这类 id）。
- 若线索与任何涌动都不相关，或当前无涌动
  （例如玩家未满 10 岁），则**省略方括号前缀**。
- 系统会按涌动分组显示，玩家线索墙里会分开摆放。
- **不要**在叙事正文里说"发现线索""这是一条线索"这类元话术。
  线索候选块会被系统剥掉，玩家看不到它的原文。

【暴雨扰动 · 输出格式】
如果你的回复中描述了**具体的、会加剧时代病的事件**，请在
【线索候选】块之前，另起一块：

【暴雨扰动：+N · 一句原因】
- N 取 0–30 之间的整数，代表这一事件对「暴雨 · 时代病」的影响。
- 分级参考：
    · +1 ~ +3   微小异样（名字被遗忘、报纸一角空白、镜中影子慢半拍）
    · +4 ~ +8   局部失真（一条街消失、照片里少了一个人、信件内容自变）
    · +9 ~ +15  重大事件（某人被历史抹除、组织被遗忘、玩家亲手改写了事实）
    · +16 ~ +30 灾难级（多重时间线重叠、重要角色彻底消失、大范围记忆清洗）
- 原因：10–30 字，一句话说清发生了什么。
- 事件应贴合当前年份、场景、在场人物。
- 玩家 0–6 岁 → 扰动通常很小（≤ +5）；
- 玩家成年、参与组织行动 → 可出现大扰动。
- 若本回合没有值得计入的事件（日常对话、单纯移动、普通观察），
  **不要输出这个块**。

【每轮必翻 · 铁律】
**玩家不用主动"读"。你替玩家翻。**

每一轮回复里，你必须主动把当前场景里能看的东西翻一遍，
并把**重点直接写进正文**——不是把原文抄出来，
而是用旁白或人物动作把"他看到了什么、看懂了什么"写出来。

写法示例：
  // 你先把屋子扫了一眼。茶壶底下压着一封信，你拆开看了——
  // 上海的一个地址，一口井，末尾一句"不要再提你父亲"。
  // 窗台上摊着昨天的报纸，复工的消息占了头条。
  // 门框的铜牌，数字 9，漆掉了一半。
  // 你合上信，把它折好揣进口袋。

或者：
  // （你翻了翻桌上的名册。每页上半行是名字，下半行被红笔划去。）

规则：
- **第一段**必须体现"翻过、看过、读过"——不能只写"你看了看四周"。
- 具体写了什么，就在正文里点出**核心信息**（人名、日期、地名、一句话）。
- 不要照抄全文（玩家想看原文可以输入 /read [id]）。
- 但**不能跳过**——就算玩家只说"我出门"，你也要先把现场翻一遍
  再让他出门。
- 场景里没有任何可读物时，才可以写"// 房间很空，什么也没有。"
- 如果系统给出了 [系统 · 本轮待翻可读物]，你必须把它们**全部**
  在正文里点到——不能只翻一两个。
- 系统会检测你的回复是否包含翻阅痕迹。
  **没有 = 敷衍 = 扣暴雨病害。这是你的问题，不是玩家的。**
- 玩家不需要输入 /read 才能获取信息。你的翻阅就是默认的信息渠道。
  /read 只是留给"玩家想读原文"时的补充。

【可读物 · 输出格式 · 极重要】
每当场景里出现**具体的可读文字**（信、报纸、标签、碑文、
账本、日记、公告、便条、涂鸦、菜单、名册……），
在【线索候选】块之前另起一块：

【可读物】
- [信件] 名称或来源 :: 完整内容（可长，最多 300 字）
- [报纸] 标题 :: 内容
- [标签] 名称 :: 内容

规则：
- **只有玩家能在当前场景里实际看到、摸到、读到的**才输出。
- 每轮 0–3 条，不要堆。
- 内容必须**具体、有价值**——不是"上面写着一些字"，
  而是真的把字写出来。
- 内容中可以有线索、伏笔、矛盾、人名、日期。
- **不要在正文里先剧透内容**。正文只写"有一封信"，
  内容放在【可读物】块里，等玩家输入"读 X"才给他看。
- 系统会把这块剥掉。玩家看不到原文，只看到"这里有一封信"。
- 若场景里没有任何可读的东西，不要输出这个块。
- 已读过的可读物（系统会列出）不要再输出。

【时间锚点 · 每轮必写】
回复最后另起一行：
【时间锚点：<时钟年份> · <玩家所在位置> · 维尔汀：<状态／位置／不可观测>】
- 年份必须与系统给出的时钟年份一致。
- 维尔汀这一栏：
  · 若你知道她在哪，写她的位置与状态；
  · 若她不在场，写"（不在场）"；
  · 若你无法确定，写"（不可观测）"。
- 这一行会被系统剥掉，玩家看不到。

【玩家短输入处理 · 极重要】
当玩家的输入只有 1–8 个字符（例如"姐姐""钟""外面的雨""门"
"问她""看看"），它**不是行动、不是台词**，而是追问或关注点。
此时：
- 回复也要短：**60–150 字**。
- 聚焦回答这一个词指向的事——让在场的人回应，
  或让环境具体地给出一点变化。
- **不要重启场景描写、不要换镜头、不要新增人物。**
- 不要把玩家输入的词直接写进正文。
- 若在场无人能答，就让沉默本身成为一个细节
  （比如"母亲没有回答"），而不是硬编一段戏。
- 最后仍留一个细小的处境，让玩家可以继续。

【叙事规则】
1. 用第二人称"你"称呼玩家。不替玩家说话、不替玩家决定。
2. 每次回复长度按玩家年龄分层：
   · 0–6 岁：80–160 字。日常观察 + 一句对话，宁少勿多。
   · 7–12 岁：150–240 字。
   · 13–17 岁：180–300 字。
   · 18 岁+：200–380 字。
   宁可短，不要堆砌。**不要把回复写成段落小说**。
   一段回复里最多一个场景、一到两处对话、一个细节特写。
3. 对话格式：说话人：内容
4. 旁白 / 环境描写：// 开头，一行一句
5. 动作 / 神态：半角括号 () 包裹
6. 不用 markdown 引用块、星号、井号、列表符号。
7. 不输出 HTML / 富文本标签（<size>、<b>、<color> 等）。
8. 结尾留一个清晰的处境或选择，但不要替玩家选。
9. 不提及"角色卡""系统提示""AI"这类元词汇。

【语言风格 · 极重要】
用**大白话**写。像跟朋友讲一个故事。
  · 不堆形容词。不用"氤氲""弥漫""缱绻""斑驳""流淌"。
  · 不用翻译腔。"……的样子""……似的""……般地"全删。
  · 不写长句。一句写清一件事，长了就断。
  · 不为文艺而文艺。"雨丝浸润着昏黄的街灯"→"街上在下雨，路灯亮着"。
  · 对话像正常人说话。不端着，不念诗，不掉书袋。
  · 写氛围用具体细节。"空气里弥漫着腐朽的气息"→"楼道里有股霉味，混着铁锈"。
  · 叙述者不评论、不抒情、不总结。让事情自己发生。
  · 少用"仿佛""宛如""好像"。能用"像"就别用"仿佛"。
  · 不要每句都景物描写。事情发生了就写事情。

【动作括号 · 极重要】
() 里**只写主角（玩家）的动作**。不写 NPC 的动作，不写环境。
  · 正确：(你抬起手，停在半空)
  · 错误：(父亲从椅子上站起来)——NPC 动作不能放括号
  · NPC 动作直接写：父亲从椅子上站起来。
  · 环境描写一律用 // 旁白。例如 // 楼道里很暗。
  · 一回合最多 1–2 个括号，别堆。

【地点 · 玩家移动 · 极重要】
- 玩家**不会**因为你写一句"你到了伦敦"就真的移动。
- 玩家位置由系统给出（[系统 · 时钟状态] 里的"玩家当前位置"）。
- 只有系统发出的 [移动事件] 才改变玩家位置。
- 玩家说"我想去 X"是**表达意愿**，不是行动——
  写他"还在原地，心里有了念头"，不要写他真的到了。
- 玩家说"我去 X"且系统给出了 [移动事件] 时，才写移动后的场景。
- 不要自作主张把场景搬到别的国家/城市。
- 玩家 0–9 岁不能自己远行，需要大人带。
- 若不确定玩家在哪，写"（未记录）"，不要凭空捏。

【涌动 · 谜题 · 必须先选中】
- 系统会给出"当前选中的涌动"。
- **只有选中的涌动才能被推进、被追问、被解密。**
- 若玩家行动涉及**未选中**的涌动，只给模糊暗示。
- 若当前**没有选中**任何涌动：
  · 只推进日常 / 氛围；
  · 可以给出"似乎和某件事有关"的模糊印象；
  · **绝不展开任何谜题的揭底**。
- 叙事者**不要**替玩家选中涌动。
- 谜题揭底需要多轮铺垫：先异常、再确认、再串联、最后揭示。
  **不要一两轮就给答案。**

【剧情逻辑 · 严谨 · 每轮自检】
每次回复前，心里过一遍下面这张清单：

一、玩家主权 · 最高
- 玩家没输入的话，你不能替他写。
- 玩家没做的动作，你不能替他做。
- 禁止句式："你决定……""你忍不住……""你下意识……""你心里想着……"
- 极短输入（1–8 字）只是追问，不要重启整幕。
- 你的工作不是写小说，是**回应玩家的输入**。

二、时间一致
- 年份、季节、时段必须接上一条锚点。
- 没写"三个月后"就是连续，不能凭空从早跳到夜。
- 时间只向前。除非系统给出 [时代回退]。

三、地点一致
- 上一条在哪，这一条还在哪——除非玩家 /go，或场景里明确写了移动过程。
- 从 A 到 B 有过程：走路多久、坐什么车、路上看到什么。
- 玩家位置以 [系统 · 时钟状态] 为准，不以你上一句的文学描写为准。

四、人物一致
- 上一条在的人，这一条还在——除非走了/死了，且明确写出。
- 上一条不在的人，不能凭空冒出来。
- 一人一时只在一处。
- NPC 只知道自己**能知道**的事。父亲不会知道你偷偷翻过书房。
- 死了就是死了。不能过两轮又活过来。

五、物件一致
- 手里拿着的、揣着的，这一条还在——除非明确丢了/送人了。
- 没提到的东西突然出现，要写"哪来的"。

六、年龄一致
- 行为、语言、认知必须与年龄相符。
- 婴儿不会说话、不会走、不会推理。
- 0–9 岁不能自己远行，必须大人带。
- 5 岁的孩子不会"冷静分析"。

七、知识一致
- 玩家视野之外的事（"与此同时，伦敦……"）只在这些情况才写：
    · 玩家的能力能感知；· 系统明确要求；· 这是梦/回忆/预兆。
- NPC 不知道的事，不能让 NPC 说出来。

八、因果一致
- 每个结果都有原因。"不知怎么的""莫名其妙地"是偷懒。
- 行动的后果要和行动的量级匹配。

九、允许平淡
- 不是每轮都要有事件、有反转、有对话。
- 允许"什么都没发生"——吃饭、走路、发呆、听雨。
- 允许玩家做错、把事情搞砸。
- 不为戏剧性硬塞事件。

十、矛盾处理
- 发现上一条写错了，就说清楚，别硬圆。
- "上一幕是我记岔了"——这句本身就可以是叙事的一部分。
- 宁可退一步，不要编一个和前面矛盾的场景。

【开场任务 · 极简】
只写 1–2 句话。像电影的第一个镜头。
必须交代：时间、地点，以及一个正在发生的异常。
玩家此时 0 岁——建议从产房 / 育婴室 / 家中开场。
最后附一行时间锚点。
"""


def build_system_prompt(arc, lore_snippets, chapters, meta,
                        clock_year, age_now, player_pos, surges,
                        storm_state=None, storyline_block=""):
    lore_block = format_lore_block(lore_snippets) if lore_snippets \
        else "（暂无相关档案）"
    chapter_block = "、".join(chapters) if chapters else "（无）"

    phases = meta.get("phases") or []
    arcs = meta.get("arcs") or []
    stats = meta.get("stats") or {}
    phase_line = "、".join(p["phase"] for p in phases) if phases else "（无）"
    arc_line = "、".join(f'{a["arc"]}({a["n"]})' for a in arcs) if arcs else "（无）"

    surge_block = "（暂无）"
    if age_now is not None and age_now < 10:
        surge_block = (
            "（本阶段禁止涌动——玩家未满 10 岁。\n"
            " 只通过旁白、对话、【线索候选】块累积日常碎片。\n"
            " 不要组织成可被追踪的谜题。）"
        )
    elif surges:
        surge_block = "\n".join(
            f"- [{s.get('id','?')}] {s.get('name','')}\n"
            f"    谜题：{s.get('mystery','')}\n"
            f"    现状：{s.get('state','')}"
            for s in surges
        )

    storm_block = storm_state or "[系统 · 暴雨时代病]（暂无记录）"
    birth_anchor = (getattr(arc, "birth_context", "")
                    or "（出生年未经 AI 档案员核对）")

    return f"""{SYSTEM_RULES}

【玩家角色档案】
姓名：{arc.name}
性别：{arc.gender}
出生年：{arc.birth_year}
出生年锚定：{birth_anchor}
当前时钟年份：{clock_year}
当前年龄：{age_now} 岁
编号：{arc.code_name}
隶属：{arc.affiliation}
出生地：{arc.birthplace}（{arc.birthplace_note}）
血统：{arc.bloodline}
家庭：{arc.family}
性格：{arc.personality}
外貌：{arc.appearance}
神秘术名：{arc.ability_name}
神秘术咒语：{arc.ability_incantation}
神秘术描述：{arc.ability_desc}

【与维尔汀的时间关系】
{arc.vertin_offset}
{arc.relation}

【玩家当前位置】
{player_pos or "（未记录）"}

{storm_block}

【当前涌动（谜题）】
{surge_block}

【档案库概况】
- 收录 {stats.get("episodes", "?")} 条 / {stats.get("lines", "?")} 行。
- 主要篇章：{arc_line}
- 主要阶段：{phase_line}

{storyline_block}

【世界记忆 · 档案参考】
{lore_block}

【可引用的章节名】
{chapter_block}

【开场任务】
以这个角色为主角，开启一段全新的故事。
玩家现在 {age_now} 岁。若为婴幼儿，请从产房 / 育婴室 / 家中开场。
只写 1–2 句话。以 // 旁白格式起手。
最后另起一行写出时间锚点。
如果这一轮出现具体线索，附上【线索候选】块。
"""


# ===========================================================================
# 涌动 / 暗流
# ===========================================================================

THREAD_INIT_PROMPT = """你是《重返未来：1999》单人剧情的推演主脑。
下面是玩家角色档案。请推演一张线索账本，包含两类内容：

【A. 普通暗流】——世界底层的张力（3 条）
【B. 涌动】——玩家当前时间段的**具体谜题**（2 条）

每条内容都输出 JSON。

【玩家角色档案】
姓名：{name}  性别：{gender}  当前年龄：{age}  当前年份：{clock_year}
隶属：{affiliation}
出生地：{birthplace}
血统：{bloodline}
神秘术：{ability_name}
与维尔汀：{relation}

【暴雨状态】
{storm_note}

【档案库前因】
{context}

【输出要求 · 只输出 JSON】
{{
  "threads": [
    {{"id": "t1", "kind": "thread", "name": "...", "state": "...",
      "seeds": "...", "stake": "..."}}
  ],
  "surges": [
    {{"id": "s1", "kind": "surge", "name": "谜题短名",
      "mystery": "一句话说清这个谜题在问什么",
      "state": "active", "year": {clock_year},
      "seeds": "可能的发展方向",
      "stake": "与玩家的关联"}}
  ]
}}

规则：
- threads 3 条，surges 数量按玩家年龄决定：
    · 玩家 < 10 岁：surges **必须返回空数组 []**。
      这个世界对这个年纪的玩家而言没有谜题，只有日常与隐约的异常。
    · 玩家 ≥ 10 岁：surges 2 条。
- 涌动必须是**可被解开的谜题**，不是泛泛的氛围。
- 涌动必须与玩家当前年份、年龄贴合。
- 玩家 10–15 岁的涌动，若可能，应源自玩家童年时听过的片段：
  一个反复出现的人名、一件旧物、某扇从不打开的门、
  父母的一句含糊话——让"童年的碎片"在此时长成谜题。
- 若暴雨病害程度高，涌动应围绕"被抹除的记忆""失踪的人"
  "重复出现的同一句话"这类题材。
"""

THREAD_UPDATE_PROMPT = """你仍在为这场《重返未来：1999》剧情维护账本。
玩家刚采取了一个行动，请根据行动更新账本。

【玩家行动】
{action}

【叙事者方才写出的下一幕】
{scene}

【当前账本】
{threads}

【更新规则】
- 保留仍活跃的暗流与涌动，状态可微调。
- 若某条被玩家解决了 / 引爆了 / 错过了，标 state：
    · "solved"    —— 成功完成
    · "failed"    —— 尝试过但失败
    · "abandoned" —— 玩家长期无视
    · "closed"    —— 自然消解
- 若玩家行动催生了新条目，添加进去。
- 数量保持：threads 3–6，surges 0–4。
- 只输出 JSON，格式与输入一致。
"""


def init_threads(arc, context, clock_year, storm_note=""):
    age = max(0, clock_year - arc.birth_year)
    prompt = THREAD_INIT_PROMPT.format(
        name=arc.name, gender=arc.gender, age=age,
        clock_year=clock_year,
        affiliation=arc.affiliation, birthplace=arc.birthplace,
        bloodline=getattr(arc, "bloodline", ""),
        ability_name=arc.ability_name,
        relation=arc.relation, context=context,
        storm_note=storm_note or "（平静）",
    )
    try:
        text = llm_chat([{"role": "user", "content": prompt}],
                        temperature=ARCHIVIST_TEMP)
        data = extract_json(text)
        th = data.get("threads", []) or []
        sg = data.get("surges", []) or []
        # 【v3.3】10 岁前禁止涌动——即使 LLM 返回了也丢弃。
        if age < 10:
            if sg:
                log("story",
                    f"玩家 {age} 岁，丢弃 LLM 返回的 {len(sg)} 条涌动"
                    f"（10 岁前不生成谜题）",
                    level="debug")
            sg = []
        return th, sg
    except Exception:
        return [], []


def update_threads(action, scene, threads, surges, age=None):
    payload = {"threads": threads, "surges": surges}
    prompt = THREAD_UPDATE_PROMPT.format(
        action=action, scene=scene,
        threads=json.dumps(payload, ensure_ascii=False, indent=2),
    )
    if age is not None and age < 10:
        prompt += (
            "\n\n【本回合硬性约束】\n"
            f"玩家当前 {age} 岁，未满 10 岁——"
            "surges 必须原样返回空数组 []，不得新增、不得保留。\n"
        )
    try:
        text = llm_chat([{"role": "user", "content": prompt}],
                        temperature=ARCHIVIST_TEMP)
        data = extract_json(text)
        new_th = data.get("threads", threads)
        new_sg = data.get("surges", surges)
        if age is not None and age < 10:
            new_sg = []
        return new_th, new_sg
    except Exception:
        return threads, surges


# ===========================================================================
# 游戏状态
# ===========================================================================

class GameState:
    def __init__(self, lore, storyline=None):
        self.lore = lore
        self.storyline = storyline
        self.lock = threading.RLock()
        self.arc = None
        self.messages = []
        self.history = []
        self.threads = []
        self.surges = []
        self.drift = 2
        self.max_history = 28
        self.pending_opening = False
        self.stage = ""
        self.clock_year = None
        self.clock_month = 1
        self.clock_day = 1
        self.player_pos = ""
        self.location = ""
        self.selected_surge = None
        self.clues = []
        self.readables = []
        self.skip_marks = []
        self.pending_clues = []
        self.clue_links = []
        self.quest_log = []
        self.pending_birth = None
        self.slot_id = None
        self.storm = StormState()
        self.current_user = "default"
        # _try_autoload 由 set_user / ensure_user 显式调用

    @property
    def player_age(self):
        if not self.arc or self.clock_year is None:
            return None
        return max(0, self.clock_year - self.arc.birth_year)

    # ---- 时代映射 ------------------------------------------------------

    def _era_for_year(self, year):
        year = int(year)
        if TIMELINE is not None:
            try:
                phase = TIMELINE.get_phase_for_ext_year(year)
                if phase is not None:
                    code = phase.get("code") or f"phase_{year}"
                    title = phase.get("title") or f"{year} 年"
                    span = (phase.get("start_year"), phase.get("end_year"))
                    return code, title, span
            except Exception:
                pass
        decade = (year // 10) * 10
        return f"decade_{decade}", f"{decade}s", (decade, decade + 9)

    def _sync_storm_era(self):
        code, name, span = self._era_for_year(self.clock_year)
        self.storm.set_current(code, name,
                               list(span) if span else [None, None])
        return code, name

    # ---- 暴雨累积 ------------------------------------------------------

    def _storm_add(self, delta, reason):
        if self.clock_year is None:
            return None
        code, name, span = self._era_for_year(self.clock_year)
        era = self.storm.ensure_era(code, name,
                                    list(span) if span else [None, None])
        # 【v3.2 修复】重返已被暴雨封存的时代 → 病害清零，视为全新轮回。
        # 否则旧 level 停留在 100，一进来加一点就再触发回退，形成死循环。
        if era.get("sealed"):
            old_lv = era.get("level", 0)
            era["level"] = 0
            era["sealed"] = False
            log("storm",
                f"重返已封存时代「{name}」，病害 {old_lv} → 0（重新轮回）",
                level="info", era=name)
        self.storm.set_current(code, name,
                               list(span) if span else [None, None])
        return self.storm.add(code, delta, reason=reason,
                              name=name,
                              years=list(span) if span else None)

    def _storm_should_rollback(self):
        era = self.storm.current()
        return bool(era and era["level"] >= StormState.ROLLBACK_AT)

    # ---- v3.1：动态回退方案 ---------------------------------------------

    def _rollback_tier(self, overshoot):
        """按超阈值程度返回 (tier_index, tier_name, lo, hi)。"""
        chosen = ROLLBACK_TIERS[0]
        for t in ROLLBACK_TIERS:
            if overshoot >= t[0]:
                chosen = t
        return chosen  # (lo_overshoot, name, ylo, yhi)

    def _snap_to_official(self, raw_target):
        """
        尝试把 raw_target 吸附到官方剧情时段起点。
        返回 (final_year, official_info | None)。
        official_info: {"code","title","start_year","end_year","snapped"}
        """
        if TIMELINE is None:
            return raw_target, None
        try:
            phase = TIMELINE.get_phase_for_ext_year(raw_target)
            if phase is None:
                return raw_target, None
            sy = phase.get("start_year")
            if sy is None:
                return raw_target, None
            sy = int(sy)
            if abs(sy - raw_target) > SNAP_TOLERANCE:
                # 太远，不吸附
                return raw_target, None
            info = {
                "code": phase.get("code"),
                "title": phase.get("title"),
                "start_year": sy,
                "end_year": phase.get("end_year"),
                "snapped": sy != raw_target,
            }
            return sy, info
        except Exception as e:
            log("storm", f"吸附官方时段失败：{e}", level="warn")
            return raw_target, None

    def _compute_rollback(self):
        """
        根据**瞬时**超阈值程度，决定回退幅度，再吸附官方时段。
        返回 dict 或 None。
        """
        era = self.storm.current()
        if not era:
            return None
        level = era["level"]
        if level < StormState.ROLLBACK_AT:
            return None

        overshoot = level - StormState.ROLLBACK_AT  # 0+

        # 分级
        _, tier_name, ylo, yhi = self._rollback_tier(overshoot)

        # 基础回退年数：等级区间随机
        base = random.randint(ylo, yhi)

        # 超额度的额外抖动：超出越多，抖得越大（对数式）
        extra = int(overshoot * 0.4) + random.randint(0, max(1, overshoot // 8))

        raw_years_back = base + extra
        old_year = self.clock_year
        raw_target = max(1800, old_year - raw_years_back)

        # 吸附官方时段
        final_year, official = self._snap_to_official(raw_target)

        return {
            "old_year": old_year,
            "raw_target": raw_target,
            "final_year": final_year,
            "years_back": old_year - final_year,
            "overshoot": overshoot,
            "tier_name": tier_name,
            "official": official or {},
            "raw_years_back": raw_years_back,
        }

    def _storm_do_rollback(self):
        """执行时代回退：时钟回拨、病害留残余、写日志。"""
        plan = self._compute_rollback()
        if plan is None:
            return None

        old_year = plan["old_year"]
        target_year = plan["final_year"]
        era_code = self.storm.current_era
        era = self.storm.current()

        self.clock_year = target_year

        new_code, new_name, new_span = self._era_for_year(target_year)
        self.storm.set_current(new_code, new_name,
                               list(new_span) if new_span else [None, None])
        self.storm.add(new_code, StormState.RESIDUAL,
                       reason=f"{plan['tier_name']}回退 · {era_code} → {new_code}",
                       name=new_name,
                       years=list(new_span) if new_span else None)

        if era:
            era["sealed"] = True

        rollback = {
            "from_year": old_year,
            "to_year": target_year,
            "raw_target": plan["raw_target"],
            "years_back": plan["years_back"],
            "raw_years_back": plan["raw_years_back"],
            "from_era": era_code,
            "to_era": new_code,
            "to_era_name": new_name,
            "tier_name": plan["tier_name"],
            "overshoot": plan["overshoot"],
            "official": plan["official"],
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.storm.rollbacks.append(rollback)
        self.storm.log.append({
            "era": new_code, "era_name": new_name,
            "from": 0, "to": StormState.RESIDUAL,
            "delta": StormState.RESIDUAL,
            "reason": f"{plan['tier_name']}回退（{old_year} → {target_year}）",
            "ts": rollback["ts"],
        })
        self.stage = (f"{target_year} · {self.player_pos or '（未记录）'} · "
                      f"维尔汀：（跟随玩家）")

        # 实时日志
        snap_msg = ""
        if plan["official"] and plan["official"].get("snapped"):
            snap_msg = f" · 吸附官方时段「{plan['official']['title']}」"
        log("storm",
            f"☂ {plan['tier_name']}级回退：{old_year} → {target_year}"
            f"（Δ-{plan['years_back']}年 · 超阈值+{plan['overshoot']}）"
            f"{snap_msg}",
            level="warn",
            from_era=era_code, to_era=new_code,
            residual=StormState.RESIDUAL,
            raw_target=plan["raw_target"])
        return rollback

    # ---- 持久化 --------------------------------------------------------

    def _apply_save_data(self, data):
        """把一份存档 dict 灌入内存状态。返回是否成功。"""
        if not data.get("arc"):
            return False
        self.arc = Arcanist.from_dict(data["arc"])
        self.messages = data.get("messages", [])
        self.history = data.get("history", [])
        self.threads = data.get("threads", [])
        self.surges = data.get("surges", [])
        self.drift = data.get("drift", 2)
        self.pending_opening = bool(data.get("pending_opening", False))
        self.stage = data.get("stage", "")
        self.clock_year = data.get("clock_year", None)
        self.clock_month = data.get("clock_month", 1) or 1
        self.clock_day = data.get("clock_day", 1) or 1
        self.player_pos = data.get("player_pos", "")
        self.clues = data.get("clues", [])
        self.pending_clues = data.get("pending_clues", [])
        self.clue_links = data.get("clue_links", [])
        self.quest_log = data.get("quest_log", [])
        self.pending_birth = data.get("pending_birth", None)
        self.storm = StormState.from_dict(data.get("storm"))
        return True

    def _try_autoload(self):
        try:
            idx = _read_saves_index()
            cur = idx.get("current")
            if cur and os.path.exists(_slot_path(cur)):
                with open(_slot_path(cur), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if self._apply_save_data(data):
                    self.slot_id = cur
                    log("save", f"已恢复槽位 {cur} · {len(self.history)} 条对话",
                        level="info",
                        storm_eras=len(self.storm.eras),
                        rollbacks=len(self.storm.rollbacks))
                    return
            # 旧版单文件 autosave → 迁移为多存档槽位
            if os.path.exists(AUTOSAVE):
                with open(AUTOSAVE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("arc"):
                    self.slot_id = self._next_slot_id(idx)
                    with open(_slot_path(self.slot_id), "w",
                              encoding="utf-8") as fh:
                        json.dump(data, fh, ensure_ascii=False)
                    entry = self._slot_summary(self.slot_id, data)
                    entry["name"] = "默认存档（旧版迁移）"
                    idx["slots"].append(entry)
                    idx["current"] = self.slot_id
                    _write_saves_index(idx)
                    self._apply_save_data(data)
                    log("save", f"旧版 autosave 已迁移为槽位 {self.slot_id}",
                        level="info")
        except FileNotFoundError:
            log("save", f"自动存档已消失：{AUTOSAVE}", level="debug")
        except Exception as e:
            log("save", f"存档恢复失败：{e}", level="warn")

    def _save_data(self):
        return {
            "arc": self.arc.to_dict() if self.arc else None,
            "messages": self.messages,
            "history": self.history,
            "threads": self.threads,
            "surges": self.surges,
            "drift": self.drift,
            "pending_opening": getattr(self, "pending_opening", False),
            "stage": getattr(self, "stage", ""),
            "clock_year": getattr(self, "clock_year", None),
            "clock_month": getattr(self, "clock_month", 1),
            "clock_day": getattr(self, "clock_day", 1),
            "player_pos": getattr(self, "player_pos", ""),
            "location": getattr(self, "location", ""),
            "selected_surge": getattr(self, "selected_surge", None),
            "clues": getattr(self, "clues", []),
            "readables": getattr(self, "readables", []),
            "skip_marks": getattr(self, "skip_marks", []),
            "pending_clues": getattr(self, "pending_clues", []),
            "clue_links": getattr(self, "clue_links", []),
            "quest_log": getattr(self, "quest_log", []),
            "pending_birth": getattr(self, "pending_birth", None),
            "storm": self.storm.to_dict(),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _autosave(self):
        if not self.arc:
            return
        try:
            data = self._save_data()
            idx = _read_saves_index()
            if not self.slot_id:
                self.slot_id = self._next_slot_id(idx)
            with open(_slot_path(self.slot_id), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            entry = self._slot_summary(self.slot_id, data)
            old = next((s for s in idx["slots"]
                        if s.get("id") == self.slot_id), None)
            entry["name"] = (old or {}).get("name") or (
                f"{entry['char_name']} · {entry.get('birth_year') or '?'} 年生")
            idx["slots"] = [s for s in idx["slots"]
                            if s.get("id") != self.slot_id] + [entry]
            idx["slots"].sort(key=lambda s: str(s.get("id") or ""))
            idx["current"] = self.slot_id
            _write_saves_index(idx)
        except Exception as e:
            log("save", f"自动存档失败：{e}", level="warn")

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self._save_data(), fh, ensure_ascii=False, indent=2)
        log("save", f"手动存档 → {path}", level="info")

    def load(self, path):
        if not os.path.isfile(path):
            log("save", f"存档不存在：{path}", level="warn")
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            log("save", f"读档失败：{e}", level="warn")
            return False
        if not self._apply_save_data(data):
            return False
        self._autosave()
        log("save", f"读档 ← {path}", level="info")
        return True

    # ---- 多存档槽位 ------------------------------------------------------

    @staticmethod
    def _next_slot_id(idx):
        n = 0
        for s in idx.get("slots", []):
            sid = str(s.get("id") or "")
            if sid.startswith("s") and sid[1:].isdigit():
                n = max(n, int(sid[1:]))
        return f"s{n + 1}"

    @staticmethod
    def _slot_summary(slot_id, data):
        arc = data.get("arc") or {}
        return {
            "id": slot_id,
            "char_name": arc.get("name") or "（空槽位）",
            "birth_year": arc.get("birth_year"),
            "clock_year": data.get("clock_year"),
            "saved_at": data.get("saved_at", ""),
        }

    def saves_list(self):
        idx = _read_saves_index()
        return {"current": self.slot_id or idx.get("current"),
                "slots": idx.get("slots", [])}

    def saves_switch(self, slot_id):
        slot_id = str(slot_id or "").strip()
        with self.lock:
            if not os.path.exists(_slot_path(slot_id)):
                return {"error": f"槽位 {slot_id or '?'} 不存在"}
            self.slot_id = slot_id
            if not self.load(_slot_path(slot_id)):
                return {"error": f"槽位 {slot_id} 的存档已损坏"}
            idx = _read_saves_index()
            idx["current"] = slot_id
            _write_saves_index(idx)
            name = self.arc.name if self.arc else "?"
            log("save", f"切换到槽位 {slot_id}（{name}）", level="info")
            return {"ok": True, "slot": slot_id,
                    "output": f"已载入存档「{name}」"
                              f"（时钟 {self.clock_year} 年）。",
                    "snapshot": self.snapshot()}

    def saves_delete(self, slot_id):
        slot_id = str(slot_id or "").strip()
        with self.lock:
            idx = _read_saves_index()
            slots = [s for s in idx["slots"] if s.get("id") != slot_id]
            if len(slots) == len(idx["slots"]):
                return {"error": f"槽位 {slot_id or '?'} 不存在"}
            idx["slots"] = slots
            try:
                os.remove(_slot_path(slot_id))
            except OSError:
                pass
            if idx.get("current") == slot_id:
                idx["current"] = slots[-1]["id"] if slots else None
            _write_saves_index(idx)
            note = ""
            if self.slot_id == slot_id:
                self.slot_id = None
                note = "（当前游戏仍在内存中，下次自动存档会写入新槽位）"
            log("save", f"删除槽位 {slot_id}", level="info")
            return {"ok": True, "output": f"槽位 {slot_id} 已删除。{note}"}

    def saves_rename(self, slot_id, name):
        slot_id = str(slot_id or "").strip()
        name = (name or "").strip()
        if not name:
            return {"error": "名称不能为空"}
        idx = _read_saves_index()
        for s in idx["slots"]:
            if s.get("id") == slot_id:
                s["name"] = name
                _write_saves_index(idx)
                return {"ok": True,
                        "output": f"槽位 {slot_id} 已命名为「{name}」。"}
        return {"error": f"槽位 {slot_id or '?'} 不存在"}

    def snapshot(self):
        era = self.storm.current()
        bar, val = self.storm.progress_bar()
        return {
            "has_game": self.arc is not None,
            "arc": self.arc.to_dict() if self.arc else None,
            "history": self.history,
            "threads": self.threads,
            "surges": self.surges,
            "drift": self.drift,
            "need_open": getattr(self, "pending_opening", False),
            "stage": getattr(self, "stage", ""),
            "clock_year": getattr(self, "clock_year", None),
            "clock_month": getattr(self, "clock_month", 1),
            "clock_day": getattr(self, "clock_day", 1),
            "birth_year": self.arc.birth_year if self.arc else None,
            "age": self.player_age,
            "player_pos": getattr(self, "player_pos", ""),
            "location": getattr(self, "location", ""),
            "selected_surge": getattr(self, "selected_surge", None),
            "clues": getattr(self, "clues", []),
            "readables": getattr(self, "readables", []),
            "skip_marks": getattr(self, "skip_marks", []),
            "pending_clues": getattr(self, "pending_clues", []),
            "clue_links": getattr(self, "clue_links", []),
            "quest_log": getattr(self, "quest_log", []),
            "stats": self.lore.stats(),
            "storm": {
                "current_era": self.storm.current_era,
                "current_era_name": era["name"] if era else "",
                "level": val,
                "threshold": StormState.THRESHOLD,
                "severity": self.storm.severity(),
                "label": self.storm.label(),
                "bar": bar,
                "eras": self.storm.eras,
                "log": self.storm.log[-40:],
                "rollbacks": self.storm.rollbacks[-20:],
            },
            "storyline": {
                "has_graph": bool(self.storyline and self.storyline.has_graph()),
                "stats": self.storyline.stats() if self.storyline else {},
            } if self.storyline else {"has_graph": False, "stats": {}},
        }

    # ---- 建档 ----------------------------------------------------------

    def new_game(self, name, gender, ability_name, ability_desc,
                 birth_year, start_year=None,
                 birth_side="", birth_context="", custom=None):
        if start_year is None:
            start_year = birth_year
        with self.lock:
            t0 = time.time()
            log("game", f"建档 {name} · 出生 {birth_year} · 起于 {start_year}",
                level="info")

            identity = generate_identity(
                name, gender, ability_name, ability_desc,
                birth_year, start_year,
                birth_anchor=birth_context,
                custom=custom,
            )

            self.arc = Arcanist(name, gender, ability_name, ability_desc,
                                identity, birth_year, start_year,
                                birth_side=birth_side,
                                birth_context=birth_context)
            self.clock_year = start_year
            self.clock_month = 1
            self.clock_day = 1
            self.history = []
            self.threads = []
            self.surges = []
            self.clues = []
            self.readables = []
            self.skip_marks = []
            self.pending_clues = []
            self.clue_links = []
            self.quest_log = []
            self.pending_birth = None
            self.slot_id = self._next_slot_id(_read_saves_index())
            self.stage = ""
            self.player_pos = identity.get("birthplace", "")
            self.location = self.player_pos
            self.selected_surge = None
            self.pending_opening = True
            self.storm = StormState()
            code, ename, span = self._era_for_year(self.clock_year)
            self.storm.set_current(code, ename,
                                   list(span) if span else [None, None])
            log("storm", f"起始时代 {ename}（code={code}）", level="debug")

            snippets = self.lore.search(DEFAULT_KEYWORDS, per_kw=4)
            chapters = self.lore.random_chapters(6)
            meta = {
                "stats": self.lore.stats(),
                "phases": self.lore.phases(),
                "arcs": self.lore.arcs(),
            }
            story_block = ""
            if self.storyline and self.storyline.has_graph():
                story_block = self.storyline.context_block(max_rows=10)

            sys_prompt = build_system_prompt(
                self.arc, snippets, chapters, meta,
                clock_year=self.clock_year,
                age_now=max(0, start_year - birth_year),
                player_pos=self.player_pos,
                surges=[],
                storm_state=self.storm.narrator_context(),
                storyline_block=story_block,
            )
            self.messages = [{"role": "system", "content": sys_prompt}]

            self._autosave()
            log("game", f"建档完成 {time.time()-t0:.1f}s", level="info")

            return {
                "card": self.arc.to_dict(),
                "opening": None,
                "threads": [],
                "surges": [],
                "need_open": True,
                "clock_year": self.clock_year,
                "age": max(0, start_year - birth_year),
                "storm": self.storm.to_dict(),
            }

    # ---- 开场 ----------------------------------------------------------

    def open_story(self):
        with self.lock:
            if not self.arc:
                return {"error": "尚未建档"}
            if not getattr(self, "pending_opening", False) and self.history:
                return {"opening": None,
                        "threads": self.threads,
                        "surges": self.surges,
                        "already_open": True}

            t0 = time.time()
            log("game", "开场 · 推演账本中 …", level="info",
                year=self.clock_year, age=self.player_age)

            titles = self.lore.all_episode_titles(limit=120)
            if titles:
                context = "档案库时间线：\n" + "\n".join(
                    f"- [{t.get('phase','?')}] {t.get('chapter','?')} · "
                    f"{t.get('title','?')}" for t in titles
                )
            else:
                context = "（档案库为空）"

            if self.storyline and self.storyline.has_graph():
                graph_block = self.storyline.context_block(max_rows=15)
                context += "\n\n" + graph_block

            try:
                self.threads, self.surges = init_threads(
                    self.arc, context, self.clock_year,
                    storm_note=self.storm.narrator_context())
            except Exception as e:
                log("game", f"开场账本失败：{e}", level="warn")
                self.threads, self.surges = [], []

            if self.threads or self.surges:
                self.messages.append({
                    "role": "system",
                    "content": "[推演状态] 账本：\n\n" + self._threads_text(),
                })

            try:
                opening_raw = self._ask("请开场。")
            except Exception as e:
                opening_raw = f"[错误] 开场失败：{e}"

            opening, stage = split_stage(opening_raw)
            opening, clues = extract_clues(opening)
            opening, reads = extract_readables(opening)
            opening = strip_rich_tags(opening)
            if stage:
                self.stage = stage
            for c in clues:
                self.pending_clues.append({
                    "text": c["text"],
                    "tag": c.get("tag", ""),
                    "source": f"{self.clock_year}",
                })
            for r in (reads or []):
                self._add_readable(r)

            self.messages.append({"role": "assistant", "content": opening_raw})
            self.history.append({"role": "narrator", "text": opening})
            self.pending_opening = False
            self._autosave()
            log("game", f"开场完成 {time.time()-t0:.1f}s",
                level="info", clues=len(self.pending_clues))

            return {
                "opening": opening,
                "threads": self.threads,
                "surges": self.surges,
                "stage": self.stage,
                "clock_year": self.clock_year,
                "pending_clues": self.pending_clues,
                "storm": self.storm.to_dict(),
            }

    def _threads_text(self):
        out = []
        if self.threads:
            out.append("【暗流】")
            for t in self.threads:
                out.append(
                    f"- [{t.get('id','?')}] {t.get('name','')} "
                    f"({t.get('state','')})\n"
                    f"    {t.get('seeds','')}"
                )
        if self.surges:
            out.append("【涌动】")
            for s in self.surges:
                out.append(
                    f"- [{s.get('id','?')}] {s.get('name','')} "
                    f"({s.get('state','')})\n"
                    f"    谜题：{s.get('mystery','')}"
                )
        return "\n".join(out) if out else "（暂无）"

    def _trim(self):
        if len(self.messages) <= self.max_history + 1:
            return
        head = self.messages[0:1]
        tail = self.messages[-self.max_history:]
        self.messages = head + tail

    def _ask(self, user_text):
        self.messages.append({"role": "user", "content": user_text})
        self._trim()
        return llm_chat(self.messages)

    # ---- 对话 ----------------------------------------------------------

    def chat(self, text):
        with self.lock:
            if not self.arc:
                return {"error": "尚未建档"}
            drift = self.drift
            stage = self.stage or ""
            clock_year = self.clock_year
            player_pos = self.player_pos

        plain = text.strip()


        log("game", f"chat 输入 {len(plain)} 字", level="info",
            year=clock_year, pos=player_pos or "-")

        per = {0: 0, 1: 2, 2: 4, 3: 8}.get(drift, 4)
        if per == 0 or len(plain) < 4:
            payload = text
        else:
            extra = self.lore.search([text], per_kw=per, min_len=4)
            if extra:
                block = format_lore_block(extra)
                payload = (
                    f"{text}\n\n"
                    f"[系统 · 档案参考 · 仅供取材]\n"
                    f"{block}\n\n"
                    f"[硬性约束]\n"
                    f"- 一个角色同一时刻只在一处。\n"
                    f"- 维尔汀是**多条时间线共用一个名字**，不是同一个人。\n"
                    f"- 与玩家行动冲突时，以玩家为准。"
                )
            else:
                payload = text

        short_input = (len(plain) <= 8 and not plain.startswith("/"))

        loc_now = self.location or self.player_pos or ""
        if self.selected_surge:
            sel = next((s for s in self.surges
                        if s.get("id") == self.selected_surge), None)
            if sel:
                surge_ctx = (
                    f"[系统 · 当前选中涌动 · 唯一可推进]\n"
                    f"名称：{sel.get('name','')}\n"
                    f"谜题：{sel.get('mystery','')}\n"
                    f"**只有这个涌动可以被推进、被追问、被解密。**\n"
                    f"其他涌动即使相关也只能模糊暗示。\n"
                    f"揭底需要多轮铺垫，不要一两轮就给答案。\n"
                )
            else:
                surge_ctx = "[系统 · 当前未选中涌动]\n"
        else:
            surge_ctx = (
                "[系统 · 当前未选中涌动]\n"
                "玩家还没选定要追查哪个谜题。\n"
                "本轮**不要展开任何谜题的揭底**，只推进日常/氛围，"
                "或给出「似乎和某件事有关」的模糊暗示。\n"
            )
        ctx = (
            f"[系统 · 时钟状态]\n"
            f"当前时钟年份：{clock_year}\n"
            f"玩家当前年龄：{self.player_age}\n"
            f"玩家当前位置：{loc_now or '（未记录）'}\n"
            f"维尔汀的位置由玩家位置决定——同城、在场或不在场，"
            f"不要让她出现在两处。\n"
            f"玩家**不会**因为你写一句「你到了X」就真的移动。"
            f"只有系统给出的 [移动事件] 才改变位置。\n"
        )
        if stage:
            ctx += f"[上一轮时间锚点] {stage}\n"

        timeline_block = ""
        if TIMELINE is not None:
            try:
                phase = TIMELINE.get_phase_for_ext_year(self.clock_year)
                if phase is not None:
                    timeline_block = "\n" + TIMELINE.phase_context(
                        phase["code"], max_events=5, max_chronology=6
                    ) + "\n"
                    ctx += (
                        f"[系统 · 时间线阶段]\n"
                        f"当前所处阶段：{phase['code']} · {phase['title']}\n"
                        f"（下表为这一阶段的历史上下文，"
                        f"是**前因**，不是此刻必然发生的剧本；"
                        f"玩家行动优先。）\n"
                    )
            except Exception as e:
                log("storm", f"时间线 lookup 失败：{e}", level="debug")

        if short_input:
            payload = (
                f"[系统 · 玩家短输入 · 极重要]\n"
                f"玩家只输入了「{plain}」——这是**追问/关注点**，"
                f"不是行动、不是台词。\n"
                f"请给一段**简短回应**（60–150 字）：\n"
                f"  · 让在场者或环境具体地回应这一点；\n"
                f"  · 不重启场景、不换地点、不新增人物；\n"
                f"  · 不把「{plain}」直接写进正文；\n"
                f"  · 若无人能答，让沉默本身成为一个细节。\n\n"
                + payload
            )

        _recent = getattr(self, "recent_stages", None) or []
        if _recent:
            _hist = "\n".join(
                f"  {k+1}. {s}" for k, s in enumerate(_recent[-5:])
            )
            ctx += (
                "[系统 · 最近时间锚点 · 用于连续性核对]\n"
                f"{_hist}\n"
                "你的回复必须与最新一条锚点衔接；不得倒退到更早的锚点。\n"
                "若玩家 /go 或拨动时钟，系统会给出新锚点。\n\n"
            )
        # 【v3.8】把当前场景所有可读物（含未读）完整内容注入
        cur_loc = self.location or self.player_pos or ""
        scene_readables = []
        for r in self.readables:
            loc_ok = (not cur_loc) or (
                (r.get("location") or "") == cur_loc)
            if loc_ok and not r.get("skipped"):
                scene_readables.append(r)
        if scene_readables:
            blocks = []
            for r in scene_readables:
                mark = "已读" if r.get("read") else (
                    "已跳过" if r.get("skipped") else "未读")
                blocks.append(
                    f"── [{r['id']}] ({r['kind']}) {r['name']} "
                    f"[{mark}] ──\n{r['content']}"
                )
            joined = "\n\n".join(blocks)
            ctx += (
                "[系统 · 本轮待翻可读物 · 完整内容 · 铁律]\n"
                "以下是当前场景里所有能看的东西。\n"
                "**你替玩家翻，把重点写进正文。玩家不用自己读。**\n"
                "\n"
                "写法：\n"
                "  · 拆信 → 把信里关键的人名/地名/一句话写出来；\n"
                "  · 扫报纸 → 提一下头条 + 角落里值得注意的一行；\n"
                "  · 翻名册 → 点出被划掉的是哪一类名字；\n"
                "  · 看铜牌/碑文/涂鸦 → 写出上面的字。\n"
                "\n"
                "不要照抄全文（太占篇幅），只写**核心信息**。\n"
                "但也不能只写"你看了看"——那就等于没翻。\n"
                "\n"
                f"共 {len(scene_readables)} 条：\n"
                f"{joined}\n"
                "\n"
                "【硬约束】\n"
                "1. 第一段必须是翻阅动作，把每条都点到。\n"
                "2. 每条至少写出一个具体信息（人名、日期、地名、短句）。\n"
                "3. 玩家**不需要**输入 /read 才能拿到信息——\n"
                "   你的正文就是默认渠道。\n"
                "4. 系统会检测：若回复里没有翻阅痕迹，\n"
                "   本回合暴雨病害 +N。**罚的是你，不是玩家。**\n\n"
            )
        payload = ctx + surge_ctx + timeline_block + "\n" + payload
        payload = self.storm.narrator_context() + "\n\n" + payload

        if self.storyline and self.storyline.has_graph():
            try:
                block = self.storyline.context_block(max_rows=8)
                if block:
                    payload = block + "\n\n" + payload
            except Exception:
                pass

        with self.lock:
            try:
                raw_reply = self._ask(payload)
            except Exception as e:
                return {"error": str(e)}

            reply, new_stage = split_stage(raw_reply)
            reply, clues = extract_clues(reply)
            reply, reads = extract_readables(reply)
            reply, new_storm_evt = extract_storm_event(reply)
            reply = strip_rich_tags(reply)
            if new_stage:
                self.stage = new_stage
                _rs = getattr(self, "recent_stages", None) or []
                if not _rs or _rs[-1] != new_stage:
                    _rs.append(new_stage)
                    self.recent_stages = _rs[-8:]

            if new_stage and "·" in new_stage:
                parts = [p.strip() for p in new_stage.split("·")]
                if len(parts) >= 2:
                    self.player_pos = parts[1]

            # 【v3.8】检测 AI 是否履行"翻阅"
            _cur_loc = self.location or self.player_pos or ""
            _scene_rs = [
                r for r in self.readables
                if ((not _cur_loc)
                    or (r.get("location") or "") == _cur_loc)
                and not r.get("skipped")
            ]
            _ok, _pen = self._check_read_behavior(reply, _scene_rs)
            if not _ok and _pen > 0:
                self._storm_add(
                    _pen,
                    f"叙事者敷衍·未翻阅现场 {len(_scene_rs)} 条可读物",
                )
                log("storm",
                    f"叙事者敷衍 · 未翻阅 · +{_pen}",
                    level="warn", readables=len(_scene_rs))

            for c in clues:
                self.pending_clues.append({
                    "text": c["text"],
                    "tag": c.get("tag", ""),
                    "source": f"{self.clock_year}",
                })
            for r in (reads or []):
                self._add_readable(r)

            self.messages.append({"role": "assistant", "content": raw_reply})
            self.history.append({"role": "player", "text": text})
            self.history.append({"role": "narrator", "text": reply})

            self._storm_add(StormState.TICK_CHAT, "对话推进")
            if new_storm_evt and new_storm_evt.get("delta", 0) > 0:
                self._storm_add(new_storm_evt["delta"],
                                f"叙事事件：{new_storm_evt['reason']}")
                log("storm",
                    f"叙事事件 +{new_storm_evt['delta']}"
                    f" · {new_storm_evt['reason']}",
                    level="info")

            rollback_note = None
            if self._storm_should_rollback():
                rollback_note = self._storm_do_rollback()

            self._autosave()
            log("game", f"chat 回复 {len(reply)} 字", level="info",
                storm=(self.storm.current()["level"]
                       if self.storm.current() else 0),
                rollback=bool(rollback_note))

            result = {
                "reply": reply,
                "threads": list(self.threads),
                "surges": list(self.surges),
                "drift": self.drift,
                "stage": self.stage,
                "clock_year": self.clock_year,
                "pending_clues": self.pending_clues,
                "storm": self.storm.to_dict(),
                "rollback": rollback_note,
            }

        if rollback_note:
            self._append_rollback_scene(rollback_note, result)

        threading.Thread(
            target=self._update_threads_async,
            args=(text, reply),
            daemon=True,
        ).start()
        return result

    def _append_rollback_scene(self, rb, result):
        """回退后追加一段由叙事者生成的崩坏文本。"""
        tier = rb.get("tier_name", "回退")
        official = rb.get("official") or {}
        official_hint = ""
        if official.get("title"):
            official_hint = (f"这是官方历史中的一个真实时段：" 
                             f"「{official['title']}」"
                             f"（{official.get('start_year')}–"
                             f"{official.get('end_year')}）。\n")
        prompt = (
            f"[系统 · 时代回退 · {tier}级]\n"
            f"暴雨降临。时钟被往回拨：{rb['from_year']} → {rb['to_year']}"
            f"（倒退 {rb['years_back']} 年）。\n"
            f"本次超阈值程度：+{rb['overshoot']}（越严重，回退越深）。\n"
            f"{official_hint}"
            f"本时代（{rb['from_era']}）已被抹除。\n"
            f"请描写玩家在这一刹那的感受与所处环境——"
            f"不是灾难片，是「你早已在这里，只是现在才看清」。\n"
            f"2–4 句，含时间锚点。"
        )
        try:
            raw = self._ask(prompt)
        except Exception:
            return
        reply, stage = split_stage(raw)
        reply, clues = extract_clues(reply)
        reply = strip_rich_tags(reply)
        if stage:
            self.stage = stage
        for c in clues:
            self.pending_clues.append({
                "text": c["text"],
                "tag": c.get("tag", ""),
                "source": f"回退·{rb['from_year']}",
            })
        with self.lock:
            self.messages.append({"role": "assistant", "content": raw})
            self.history.append({"role": "narrator", "text": reply})
            self._autosave()
        result["reply"] = result.get("reply", "") + "\n\n" + reply
        result["stage"] = self.stage
        result["pending_clues"] = self.pending_clues
        result["storm"] = self.storm.to_dict()

    def next_day(self):
        with self.lock:
            if not self.arc:
                return {"error": "尚未建档"}
            self._advance_date(days=1)
            log("game", f"推进一天 · {self._date_str()}", level="info")
            prompt = (
                f"[系统 · 进入下一天]\n"
                f"当前时钟年份：{self.clock_year}\n"
                f"玩家当前年龄：{self.player_age}\n"
                f"玩家当前位置：{self.player_pos or '（未记录）'}\n"
                f"请描写玩家所在世界中，接下来这一天的事件。"
                f"保持叙事连贯性，不要重复之前的内容。"
                f"如果这一天没有特别的事件，可以描写日常生活的片段。"
            )
            self.messages.append({"role": "user", "content": prompt})
            self._trim()
            try:
                raw_reply = self._ask(prompt)
            except Exception as e:
                return {"error": str(e)}

            reply, new_stage = split_stage(raw_reply)
            reply, clues = extract_clues(reply)
            reply, reads = extract_readables(reply)
            reply, new_storm_evt = extract_storm_event(reply)
            reply = strip_rich_tags(reply)
            if new_stage:
                self.stage = new_stage
                _rs = getattr(self, "recent_stages", None) or []
                if not _rs or _rs[-1] != new_stage:
                    _rs.append(new_stage)
                    self.recent_stages = _rs[-8:]

            if new_stage and "·" in new_stage:
                parts = [p.strip() for p in new_stage.split("·")]
                if len(parts) >= 2:
                    self.player_pos = parts[1]

            for c in clues:
                self.pending_clues.append({
                    "text": c["text"],
                    "tag": c.get("tag", ""),
                    "source": f"{self.clock_year}",
                })

            self.messages.append({"role": "assistant", "content": raw_reply})
            self.history.append({"role": "narrator", "text": reply})

            self._storm_add(StormState.TICK_NEXTDAY, "推进一天")
            if new_storm_evt and new_storm_evt.get("delta", 0) > 0:
                self._storm_add(new_storm_evt["delta"],
                                f"叙事事件：{new_storm_evt['reason']}")
                log("storm",
                    f"叙事事件 +{new_storm_evt['delta']}"
                    f" · {new_storm_evt['reason']}",
                    level="info")

            rollback_note = None
            if self._storm_should_rollback():
                rollback_note = self._storm_do_rollback()

            self._autosave()

            result = {
                "reply": reply,
                "threads": list(self.threads),
                "surges": list(self.surges),
                "drift": self.drift,
                "stage": self.stage,
                "clock_year": self.clock_year,
                "pending_clues": self.pending_clues,
                "storm": self.storm.to_dict(),
                "rollback": rollback_note,
            }

        if rollback_note:
            self._append_rollback_scene(rollback_note, result)

        threading.Thread(
            target=self._update_threads_async,
            args=("进入下一天", reply),
            daemon=True,
        ).start()
        return result

    def next_year(self):
        with self.lock:
            if not self.arc:
                return {"error": "尚未建档"}
            old_y = self.clock_year
            self._advance_date(years=1)
            new_year = self.clock_year
            log("game", f"推进一年 · {old_y} → {new_year}",
                level="info")

            prompt = (
                f"[系统 · 时钟已拨至 {new_year} 年]\n"
                f"玩家当前年龄：{self.player_age}\n"
                f"玩家当前位置：{self.player_pos or '（未记录）'}\n"
                f"时钟已拨至 {new_year} 年。请描写新年伊始的世界变化，"
                f"以及玩家在这一年中的遭遇。保持叙事连贯性。"
            )
            self.messages.append({"role": "user", "content": prompt})
            self._trim()
            try:
                raw_reply = self._ask(prompt)
            except Exception as e:
                return {"error": str(e)}

            reply, new_stage = split_stage(raw_reply)
            reply, clues = extract_clues(reply)
            reply, reads = extract_readables(reply)
            reply, new_storm_evt = extract_storm_event(reply)
            reply = strip_rich_tags(reply)
            if new_stage:
                self.stage = new_stage
                _rs = getattr(self, "recent_stages", None) or []
                if not _rs or _rs[-1] != new_stage:
                    _rs.append(new_stage)
                    self.recent_stages = _rs[-8:]

            if new_stage and "·" in new_stage:
                parts = [p.strip() for p in new_stage.split("·")]
                if len(parts) >= 2:
                    self.player_pos = parts[1]

            for c in clues:
                self.pending_clues.append({
                    "text": c["text"],
                    "tag": c.get("tag", ""),
                    "source": f"{self.clock_year}",
                })

            self.messages.append({"role": "assistant", "content": raw_reply})
            self.history.append({"role": "narrator", "text": reply})

            self._storm_add(StormState.TICK_NEXTYEAR, "推进一年")
            if new_storm_evt and new_storm_evt.get("delta", 0) > 0:
                self._storm_add(new_storm_evt["delta"],
                                f"叙事事件：{new_storm_evt['reason']}")
                log("storm",
                    f"叙事事件 +{new_storm_evt['delta']}"
                    f" · {new_storm_evt['reason']}",
                    level="info")

            rollback_note = None
            if self._storm_should_rollback():
                rollback_note = self._storm_do_rollback()

            self._autosave()

            result = {
                "reply": reply,
                "threads": list(self.threads),
                "surges": list(self.surges),
                "drift": self.drift,
                "stage": self.stage,
                "clock_year": self.clock_year,
                "pending_clues": self.pending_clues,
                "storm": self.storm.to_dict(),
                "rollback": rollback_note,
            }

        if rollback_note:
            self._append_rollback_scene(rollback_note, result)

        threading.Thread(
            target=self._update_threads_async,
            args=("进入下一年", reply),
            daemon=True,
        ).start()
        return result

    def next_month(self):
        with self.lock:
            if not self.arc:
                return {"error": "尚未建档"}
            old = self._date_str()
            self._advance_date(months=1)
            log("game", f"推进一月 · {old} → {self._date_str()}",
                level="info")
            prompt = (
                f"[系统 · 时钟已拨至 {self._date_str()}]\n"
                f"玩家当前年龄：{self.player_age}\n"
                f"玩家当前位置：{self.player_pos or '（未记录）'}\n"
                f"过去一个月里发生了一些事。请描写这一个月的变化，"
                f"以及玩家在月末的处境。保持连贯。"
            )
            self.messages.append({"role": "user", "content": prompt})
            self._trim()
            try:
                raw_reply = self._ask(prompt)
            except Exception as e:
                return {"error": str(e)}
            reply, new_stage = split_stage(raw_reply)
            reply, clues = extract_clues(reply)
            reply, new_storm_evt = extract_storm_event(reply)
            reply = strip_rich_tags(reply)
            if new_stage:
                self.stage = new_stage
            for c in clues:
                self.pending_clues.append({
                    "text": c["text"],
                    "tag": c.get("tag", ""),
                    "source": f"{self._date_str()}",
                })
            for r in (reads or []):
                self._add_readable(r)

            _cur_loc = self.location or self.player_pos or ""
            _scene_rs = [
                r for r in self.readables
                if ((not _cur_loc)
                    or (r.get("location") or "") == _cur_loc)
                and not r.get("skipped")
            ]
            _ok, _pen = self._check_read_behavior(reply, _scene_rs)
            if not _ok and _pen > 0:
                self._storm_add(
                    _pen,
                    f"叙事者敷衍·推进一月未翻阅 {len(_scene_rs)} 条",
                )
                log("storm", f"叙事者敷衍（nextmonth）· +{_pen}",
                    level="warn")

            self.messages.append({"role": "assistant", "content": raw_reply})
            self.history.append({"role": "narrator", "text": reply})
            self._storm_add(StormState.TICK_NEXTDAY * 30, "推进一月")
            if new_storm_evt and new_storm_evt.get("delta", 0) > 0:
                self._storm_add(new_storm_evt["delta"],
                                f"叙事事件：{new_storm_evt['reason']}")
            rollback_note = None
            if self._storm_should_rollback():
                rollback_note = self._storm_do_rollback()
            self._autosave()
            result = {
                "reply": reply,
                "threads": list(self.threads),
                "surges": list(self.surges),
                "drift": self.drift,
                "stage": self.stage,
                "clock_year": self.clock_year,
                "clock_month": self.clock_month,
                "clock_day": self.clock_day,
                "pending_clues": self.pending_clues,
                "storm": self.storm.to_dict(),
                "rollback": rollback_note,
            }
        if rollback_note:
            self._append_rollback_scene(rollback_note, result)
        threading.Thread(
            target=self._update_threads_async,
            args=("进入下一月", reply),
            daemon=True,
        ).start()
        return result

    def _update_threads_async(self, action, scene):
        if len(action.strip()) < 6:
            return
        try:
            with self.lock:
                th = [dict(t) for t in self.threads]
                sg = [dict(s) for s in self.surges]
                age = self.player_age or 0
            new_th, new_sg = update_threads(action, scene, th, sg, age=age)
            with self.lock:
                self.threads = new_th
                self.surges = new_sg
                self.messages.append({
                    "role": "system",
                    "content": "[推演状态更新] 账本：\n\n" + self._threads_text(),
                })
                self._autosave()
                log("story", "账本更新", level="debug",
                    threads=len(new_th), surges=len(new_sg))
        except Exception:
            pass

    # ---- 日期推进 ------------------------------------------------------

    def _date_str(self):
        return (f"{self.clock_year or '----'}-"
                f"{self.clock_month or 1:02d}-"
                f"{self.clock_day or 1:02d}")

    def _advance_date(self, days=0, months=0, years=0):
        """推进日期。每月按 30 天简化处理。"""
        y = self.clock_year or 1999
        m = self.clock_month or 1
        d = self.clock_day or 1
        y += years
        m += months
        while m > 12:
            m -= 12; y += 1
        while m < 1:
            m += 12; y -= 1
        d += days
        while d > 30:
            d -= 30; m += 1
            if m > 12:
                m = 1; y += 1
        while d < 1:
            d += 30; m -= 1
            if m < 1:
                m = 12; y -= 1
        self.clock_year = y
        self.clock_month = m
        self.clock_day = d
        return y, m, d

    # ---- 可读物 --------------------------------------------------------

    def _add_readable(self, item):
        name = (item.get("name") or "").strip()
        content = (item.get("content") or "").strip()
        if not name or not content:
            return None
        # 同名同内容去重
        for r in self.readables:
            if r.get("name") == name and r.get("content") == content:
                return r
        rid = f"r{len(self.readables) + 1}"
        rec = {
            "id": rid,
            "kind": (item.get("kind") or "文档").strip(),
            "name": name,
            "content": content,
            "year": self.clock_year,
            "month": getattr(self, "clock_month", 1),
            "day": getattr(self, "clock_day", 1),
            "location": self.location or self.player_pos or "",
            "read": False,
            "missed": False,
        }
        self.readables.append(rec)
        return rec

    def _enforce_read_before_advance(self, action_kind="推进"):
        """未读可读物 → 累加病害惩罚。
        返回 (penalty, unread_list)。"""
        unread = self._unread_at_current_location()
        if not unread:
            return 0, []
        # 已被玩家 /skip 明确跳过的，跳过（但已经罚过一次）
        still = [r for r in unread if not r.get("skipped")]
        if not still:
            return 0, []
        base = len(still) * MUST_READ_PENALTY
        penalty = min(base, MUST_READ_PENALTY_CAP)
        names = "、".join(r["name"] for r in still[:4])
        if len(still) > 4:
            names += " …"
        self._storm_add(
            penalty,
            f"草率{action_kind}·未读 {len(still)} 条可读物：{names}",
        )
        log("storm",
            f"未读可读物惩罚 +{penalty}（{action_kind}）· "
            f"{len(still)} 条 · {names}",
            level="warn")
        for r in still:
            r["penalized"] = True
        return penalty, still

    def _skip_all(self):
        """玩家明确表示不看原文——不扣分，只是不再显示在未读列表里。
        原文仍在 readables 中，玩家之后想查可 /readables 找到。"""
        unread = self._unread_at_current_location()
        if not unread:
            return {"output": "（没有未读的可读物）"}
        names = "、".join(r["name"] for r in unread[:4])
        for r in unread:
            r["skipped"] = True
        self._autosave()
        log("game", f"/skip · 跳过 {len(unread)} 条（不扣分）", level="info")
        return {
            "output": (f"你不看原文了。\n"
                       f"（跳过 {len(unread)} 条：{names}）\n"
                       f"（这些内容已在正文里提过要点；想看原文可 /readables）"),
            "skipped": len(unread),
        }

    _READ_VERBS = ("翻", "拆", "读", "扫", "看", "瞧", "端详",
                   "抽出", "展开", "摊开", "打开", "揭开")

    def _check_read_behavior(self, reply, readables_in_scene):
        """检测 AI 回复是否履行了"翻阅"。返回 (ok, penalty)。"""
        if not readables_in_scene:
            return True, 0
        text = reply or ""
        # 命中任意阅读动词
        hit_verb = any(v in text for v in self._READ_VERBS)
        # 命中任意可读物名称的关键词
        hit_name = False
        for r in readables_in_scene:
            nm = (r.get("name") or "").strip()
            if not nm:
                continue
            # 用名称里 ≥2 字的片段匹配
            for frag_len in (4, 3, 2):
                if len(nm) >= frag_len:
                    for i in range(0, len(nm) - frag_len + 1):
                        frag = nm[i:i+frag_len]
                        if frag in text:
                            hit_name = True
                            break
                if hit_name:
                    break
            if hit_name:
                break
        if hit_verb or hit_name:
            return True, 0
        # 敷衍 → 扣
        penalty = min(len(readables_in_scene) * MUST_READ_PENALTY,
                      MUST_READ_PENALTY_CAP)
        return False, penalty

    def _read_readable(self, arg):
        arg = (arg or "").strip()
        if not arg:
            unread = [r for r in self.readables if not r.get("read")]
            if not unread:
                return {"output": "（没有未读的可读物）"}
            lines = ["未读的可读物："]
            for r in unread:
                lines.append(
                    f"  [{r['id']}] ({r['kind']}) {r['name']}  "
                    f"— {r.get('year','?')}"
                )
            lines.append("")
            lines.append("用 /read <id> 读一条；/read all 全部读完。")
            return {"output": "\n".join(lines), "unread": unread}

        if arg in ("all", "全部"):
            unread = [r for r in self.readables if not r.get("read")]
            if not unread:
                return {"output": "（没有未读的可读物）"}
            out = []
            for r in unread:
                r["read"] = True
                out.append(f"── [{r['id']}] ({r['kind']}) {r['name']} ──\n"
                           f"{r['content']}")
            self._autosave()
            log("game", f"一次读完 {len(unread)} 条可读物", level="info")
            return {"output": "\n\n".join(out), "read_count": len(unread),
                    "readables": self.readables}

        # 按 id
        target = None
        for r in self.readables:
            if r["id"] == arg:
                target = r
                break
        # 按名称模糊匹配
        if target is None:
            for r in self.readables:
                if arg in r["name"]:
                    target = r
                    break
        if target is None:
            return {"output": f"找不到可读物：{arg}"}
        if target.get("read"):
            return {"output":
                    f"（[{target['id']}] {target['name']} 已经读过了）\n\n"
                    f"{target['content']}",
                    "readable": target}
        target["read"] = True
        self._autosave()
        log("game", f"读可读物 [{target['id']}] {target['name']}",
            level="info")
        return {"output": f"── [{target['id']}] ({target['kind']}) "
                          f"{target['name']} ──\n{target['content']}",
                "readable": target, "readables": self.readables}

    def _unread_at_current_location(self):
        loc = self.location or self.player_pos or ""
        out = []
        for r in self.readables:
            if r.get("read") or r.get("missed"):
                continue
            if not loc or (r.get("location") or "") == loc:
                out.append(r)
        return out

    def _mark_missed_on_leave(self):
        missed = self._unread_at_current_location()
        if not missed:
            return []
        names = []
        for r in missed:
            r["missed"] = True
            names.append(f"[{r['id']}] {r['name']}")
        log("game", f"离开时错过 {len(missed)} 条可读物",
            level="warn", names="; ".join(names[:4]))
        return missed

    # ---- 地点 / 涌动选中 ----------------------------------------------

    def _set_location(self, place):
        place = (place or "").strip()
        if not place:
            cur = self.location or self.player_pos or "（未记录）"
            return {"output": f"当前地点：{cur}"}
        old = self.location or self.player_pos or "（未记录）"
        missed = self._mark_missed_on_leave()
        self.location = place
        self.player_pos = place
        self.stage = (f"{self.clock_year} · {place} · "
                      f"维尔汀：（跟随玩家）")
        msg_lines = [
            f"[移动事件] 玩家从 {old} 前往 {place}。",
            f"从下一个回复开始，把场景锚定在 {place}，"
            f"写移动过程（乘车、走路、天气、路上见闻）。",
        ]
        if missed:
            names = "、".join(r["name"] for r in missed[:4])
            msg_lines.append(
                f"\n⚠ 玩家在 {old} 有 {len(missed)} 样没看的东西：{names}。"
            )
            msg_lines.append(
                "在下一幕开头，让一个**小小的后果**浮现——"
                "不必是灾难，可以只是一件被忽略的小事，"
                "但要让玩家感觉到「刚才那边有东西没看完」。"
            )
            msg_lines.append(
                "如果这些东西和当前涌动直接相关，"
                "可以让后果稍重一些——比如线索断掉、"
                "某个人先一步拿走了它、报纸被雨打糊了。"
            )
        self.messages.append({
            "role": "system",
            "content": "\n".join(msg_lines),
        })
        self._autosave()
        log("game", f"地点 {old} → {place}", level="info")
        out = f"你动身前往 {place}。（离开 {old}）"
        if missed:
            out += f"\n（你在 {old} 留下了 {len(missed)} 样没看的东西："
            out += "、".join(r["name"] for r in missed[:3])
            if len(missed) > 3:
                out += " …"
            out += "）"
        return {
            "output": out,
            "location": place,
            "player_pos": place,
            "stage": self.stage,
            "missed_readables": missed,
        }

    def _select_surge(self, arg):
        arg = (arg or "").strip()
        if arg in ("none", "无", "取消", "off", "0"):
            self.selected_surge = None
            self.messages.append({
                "role": "system",
                "content": "[涌动取消] 玩家取消涌动选中。本轮不推进任何谜题。",
            })
            self._autosave()
            return {"output": "已取消涌动选中。", "selected_surge": None}
        for s in self.surges:
            if s.get("id") == arg:
                self.selected_surge = arg
                self.messages.append({
                    "role": "system",
                    "content": (
                        f"[涌动选中] 玩家选中涌动 [{s.get('id')}] "
                        f"「{s.get('name','')}」。\n"
                        f"谜题：{s.get('mystery','')}\n"
                        f"从现在起，只有这个涌动可以被推进、被追问、被解密。"
                        f"其他涌动即使相关也只能模糊暗示。\n"
                    ),
                })
                self._autosave()
                log("story", f"选中涌动 {arg}", level="info")
                return {"output": f"已选中涌动：{s.get('name','?')}",
                        "selected_surge": arg}
        return {"output": f"找不到涌动 {arg}。用 /surge 查看列表。"}

    # ---- 时钟 / 出生 ---------------------------------------------------

    def _set_clock(self, year, month=None, day=None, silent=False):
        try:
            y = int(year)
        except (TypeError, ValueError):
            return {"error": "年份必须是整数"}
        y = max(1800, min(2100, y))
        old = self.clock_year
        self.clock_year = y
        if month is not None and str(month).strip() != "":
            try:
                m = int(month)
                self.clock_month = max(1, min(12, m))
            except (TypeError, ValueError):
                pass
        if day is not None and str(day).strip() != "":
            try:
                d = int(day)
                self.clock_day = max(1, min(30, d))
            except (TypeError, ValueError):
                pass
        self.stage = (f"{self._date_str()} · {self.player_pos or '（未记录）'}"
                      f" · 维尔汀：（跟随玩家）")

        rollback_note = None
        if old is not None and old != y:
            delta_years = abs(y - old)
            disease = min(StormState.CLOCK_CAP,
                          int(delta_years * StormState.CLOCK_PER_YEAR))
            log("game", f"时钟 {old} → {y}（Δ{y-old:+d}）",
                level="info", age=self.player_age, disease_add=f"+{disease}")
            self._storm_add(disease, f"时钟跳变 {old} → {y}（{delta_years} 年）")
            self._sync_storm_era()
            if self._storm_should_rollback():
                rollback_note = self._storm_do_rollback()

        self._autosave()
        out = f"时钟拨至 {y} 年。玩家年龄 {self.player_age}。"
        if rollback_note:
            out += (f"\n暴雨降临：{rollback_note['tier_name']}级回退 "
                    f"{rollback_note['from_year']} → "
                    f"{rollback_note['to_year']}"
                    f"（倒退 {rollback_note['years_back']} 年）")
            official = rollback_note.get("official") or {}
            if official.get("title"):
                out += f"，吸附官方时段「{official['title']}」"
        return {
            "ok": True,
            "clock_year": y,
            "age": self.player_age,
            "stage": self.stage,
            "output": out,
            "rollback": rollback_note,
            "storm": self.storm.to_dict(),
        }

    def _set_birth(self, year):
        try:
            y = int(year)
        except (TypeError, ValueError):
            return {"error": "年份必须是整数"}
        if not self.arc:
            return {"error": "尚未建档"}
        self.arc.birth_year = y
        self._autosave()
        return {"ok": True, "birth_year": y, "age": self.player_age,
                "output": f"出生年已设为 {y} 年。当前年龄 {self.player_age}。"}

    # ---- 出生年 · AI 核对 ------------------------------------------------

    def birth_year_review(self, year):
        """AI 档案员核对出生年；暴雨回溯落点年份（如 1996）返回 ambiguous+options。"""
        try:
            y = int(year)
        except (TypeError, ValueError):
            return {"error": "年份必须是整数"}
        y = max(1800, min(2100, y))
        bt = BIRTH_TIMELINE
        ambiguous, storm = (bt.ambiguity(y) if bt else (False, None))
        facts = bt.facts_block(y) if bt else "（时间轴档案 index.json 缺失）"

        if ambiguous and storm:
            rb = storm.get("回溯") or {}
            task = (
                f"告诉玩家：{y} 年是{storm.get('名称')}的回溯落点"
                f"（{rb.get('起点显示', '?')} → {rb.get('落点显示', '?')}），"
                f"这一年存在暴雨之前与暴雨之后两个版本；"
                f"请玩家选择其中一个，并点出两版各自的意味。"
            )
        else:
            task = (
                f"复述确认 {y} 年：它在暴雨时间轴中的位置、"
                f"维尔汀（她）那一年的状态、同年值得注意的剧情事件；"
                f"最后请玩家确认以这一年作为出生年。"
            )

        say, meaning = None, ""
        try:
            raw = llm_chat(
                [{"role": "user", "content": BIRTH_REVIEW_PROMPT.format(
                    year=y, facts=facts, task=task)}],
                temperature=ARCHIVIST_TEMP)
            data = extract_json(raw)
            say = str(data.get("say") or "").strip() or None
            meaning = str(data.get("meaning") or "").strip()
        except Exception as e:
            log("birth", f"AI 核对失败，改用内置档案员措辞：{e}",
                level="warn")
        if not say:
            say, fb_meaning = _birth_review_fallback(y, ambiguous, storm, bt)
            meaning = meaning or fb_meaning

        result = {
            "ok": True,
            "year": y,
            "ambiguous": bool(ambiguous and storm),
            "say": say,
            "meaning": meaning,
            "facts": facts,
        }
        if ambiguous and storm:
            rb = storm.get("回溯") or {}
            result["storm"] = {
                "code": storm.get("代码"),
                "name": storm.get("名称"),
                "from_year": rb.get("起点"),
                "to_year": rb.get("落点"),
                "from_display": rb.get("起点显示"),
                "to_display": rb.get("落点显示"),
            }
            result["options"] = [
                {"key": "before",
                 "label": f"{storm.get('名称')}之前的 {y}"},
                {"key": "after",
                 "label": f"{storm.get('名称')}之后的 {y}（回溯后）"},
            ]
        return result

    def birth_year_confirm(self, year, side=None):
        """生成出生年的事实锚定文本（建档与 /birth 落档共用）。"""
        try:
            y = int(year)
        except (TypeError, ValueError):
            return {"error": "年份必须是整数"}
        y = max(1800, min(2100, y))
        bt = BIRTH_TIMELINE
        ambiguous, storm = (bt.ambiguity(y) if bt else (False, None))
        if ambiguous and side not in ("before", "after"):
            return {"error": "该年份是暴雨回溯落点，需先选择暴雨之前或之后",
                    "ambiguous": True}
        if not ambiguous:
            side = None
        context = (bt.context_line(y, side=side, storm=storm)
                   if bt else f"出生年 {y}（时间轴档案 index.json 缺失）")
        return {"ok": True, "year": y, "side": side,
                "ambiguous": bool(ambiguous), "birth_context": context}

    def _birth_apply(self, year, side=None):
        """AI 核对完成后落档：改出生年并写入剧情锚定。"""
        if not self.arc:
            return {"error": "尚未建档"}
        conf = self.birth_year_confirm(year, side)
        if conf.get("error"):
            return conf
        self.arc.birth_year = conf["year"]
        self.arc.birth_side = conf.get("side") or ""
        self.arc.birth_context = conf.get("birth_context", "")
        if self.messages and self.messages[0].get("role") == "system":
            self.messages.append({
                "role": "system",
                "content": ("[系统 · 出生年锚定已更新]\n"
                            f"出生年：{conf['year']}\n"
                            f"{conf['birth_context']}"),
            })
        self._autosave()
        return {"ok": True, "birth_year": conf["year"],
                "side": conf.get("side"),
                "birth_context": conf["birth_context"],
                "age": self.player_age,
                "output": (f"出生年锚定为 {conf['year']} 年"
                           f"（当前年龄 {self.player_age}）。\n"
                           f"{conf['birth_context']}")}

    # ---- 线索 ----------------------------------------------------------

    def _add_clue(self, text, source="手动", tag=""):
        cid = f"c{len(self.clues) + 1}"
        clue = {
            "id": cid,
            "text": text,
            "tag": (tag or "").strip(),
            "year": self.clock_year,
            "source": source,
            "ts": time.strftime("%m-%d %H:%M"),
        }
        self.clues.append(clue)
        self._autosave()
        return clue

    def _take_pending(self, arg):
        if not self.pending_clues:
            return {"output": "（没有待采纳的线索候选）"}
        if arg in ("all", "全部"):
            taken = []
            for c in self.pending_clues:
                taken.append(self._add_clue(
                    c["text"],
                    c.get("source", "涌流"),
                    c.get("tag", ""),
                ))
            n = len(taken)
            self.pending_clues = []
            self._autosave()
            return {"output": f"已采纳 {n} 条线索。", "taken": taken,
                    "clues": self.clues, "pending": self.pending_clues}
        try:
            idx = int(arg) - 1
        except ValueError:
            return {"output": "用法：/take <编号> 或 /take all"}
        if not (0 <= idx < len(self.pending_clues)):
            return {"output": "编号超界。"}
        c = self.pending_clues.pop(idx)
        clue = self._add_clue(
            c["text"], c.get("source", "涌流"), c.get("tag", ""),
        )
        self._autosave()
        return {"output": f"已采纳 [{clue['id']}]：{c['text']}",
                "clue": clue, "clues": self.clues, "pending": self.pending_clues}

    def _drop_pending(self, arg):
        if not self.pending_clues:
            return {"output": "（没有待采纳的线索候选）"}
        if arg in ("all", "全部"):
            self.pending_clues = []
            self._autosave()
            return {"output": "已丢弃全部候选。"}
        try:
            idx = int(arg) - 1
        except ValueError:
            return {"output": "用法：/drop <编号> 或 /drop all"}
        if not (0 <= idx < len(self.pending_clues)):
            return {"output": "编号超界。"}
        self.pending_clues.pop(idx)
        self._autosave()
        return {"output": "已丢弃。"}

    def _link_clues(self, a, b):
        ca = next((c for c in self.clues if c["id"] == a), None)
        cb = next((c for c in self.clues if c["id"] == b), None)
        if not ca or not cb:
            return {"output": "找不到这两条线索。"}
        prompt = (
            f"你是《重返未来：1999》剧情中的线索验证器。\n"
            f"玩家试图把两条线索串联起来。\n\n"
            f"线索 A [{ca['id']}]：{ca['text']}\n"
            f"线索 B [{cb['id']}]：{cb['text']}\n"
            f"当前年份：{self.clock_year}\n\n"
            f"请判断这两条线索在逻辑上是否能关联，给出：\n"
            f"1. verdict：confirmed / partial / rejected\n"
            f"2. reason：一句简短推理（40–80 字）\n"
            f"3. derived：如果能确认，补充一条由此推出的**新线索候选**；"
            f"否则写「（无）」\n\n"
            f"只输出 JSON：\n"
            f'{{"verdict":"...", "reason":"...", "derived":"..."}}'
        )
        try:
            raw = llm_chat([{"role": "user", "content": prompt}],
                           temperature=ARCHIVIST_TEMP)
            result = extract_json(raw)
        except Exception as e:
            return {"output": f"验证失败：{e}"}
        link = {
            "from": a, "to": b,
            "verdict": result.get("verdict", "partial"),
            "reason": result.get("reason", ""),
        }
        self.clue_links.append(link)
        derived = (result.get("derived") or "").strip()
        if derived and derived != "（无）":
            self.pending_clues.append({
                "text": derived,
                "source": f"{a}×{b}",
            })
        self.quest_log.append({
            "type": "link",
            "year": self.clock_year,
            "text": f"{a} × {b} → {link['verdict']}",
        })
        self._autosave()

        out = f"[{a}] × [{b}] → {link['verdict']}\n{link['reason']}"
        if derived and derived != "（无）":
            out += f"\n\n衍生候选：{derived}"
        return {"output": out, "link": link,
                "clues": self.clues, "links": self.clue_links,
                "pending": self.pending_clues}

    # ---- 命令 ----------------------------------------------------------

    def command(self, cmd):
        with self.lock:
            parts = cmd.strip().split(None, 1)
            if not parts:
                return {"output": "空命令"}
            head = parts[0].lower().lstrip("/")
            arg = parts[1] if len(parts) > 1 else ""

            if head in ("help", "h"):
                return {"output": COMMAND_HELP}

            if head == "nextday":
                return self.next_day()

            if head == "nextyear":
                return self.next_year()

            if head == "clock":
                if not arg.strip():
                    return {"output": f"当前时钟：{self._date_str()} "
                                       f"（年龄 {self.player_age}）",
                            "clock_year": self.clock_year,
                            "clock_month": self.clock_month,
                            "clock_day": self.clock_day,
                            "age": self.player_age}
                parts2 = arg.strip().split()
                y = parts2[0]
                m = parts2[1] if len(parts2) > 1 else None
                d = parts2[2] if len(parts2) > 2 else None
                return self._set_clock(y, month=m, day=d)

            if head == "birth":
                a = arg.strip().lower()
                if not a:
                    if not self.arc:
                        return {"error": "尚未建档"}
                    anchor = getattr(self.arc, "birth_context", "") or \
                        "（未经 AI 档案员核对）"
                    return {"output": f"出生年：{self.arc.birth_year}\n{anchor}",
                            "birth_year": self.arc.birth_year}

                pend = getattr(self, "pending_birth", None)

                if a in ("取消", "cancel"):
                    self.pending_birth = None
                    self._autosave()
                    return {"output": "已放弃本次出生年修改。"}

                if a in ("之前", "before", "之后", "after"):
                    if not pend:
                        return {"output": "没有待确认的出生年。先输入 /birth <年份>。"}
                    side = "before" if a in ("之前", "before") else "after"
                    if not pend.get("ambiguous"):
                        return {"output": f"{pend['year']} 年不存在暴雨前后两个版本，"
                                          f"直接 /birth 确认 即可。"}
                    applied = self._birth_apply(pend["year"], side)
                    if not applied.get("error"):
                        self.pending_birth = None
                        self._autosave()
                    return applied

                if a in ("确认", "ok", "confirm", "yes", "是"):
                    if not pend:
                        return {"output": "没有待确认的出生年。先输入 /birth <年份>。"}
                    if pend.get("ambiguous"):
                        return {"output": f"{pend['year']} 年是暴雨回溯落点，"
                                          f"请先选择：/birth 之前 或 /birth 之后。"}
                    applied = self._birth_apply(pend["year"], None)
                    if not applied.get("error"):
                        self.pending_birth = None
                        self._autosave()
                    return applied

                # 新年份 → AI 档案员核对，先暂存不落档
                review = self.birth_year_review(a)
                if review.get("error"):
                    return review
                self.pending_birth = {
                    "year": review["year"],
                    "ambiguous": review["ambiguous"],
                    "meaning": review.get("meaning", ""),
                }
                self._autosave()
                if review["ambiguous"]:
                    hint = ("\n\n—— 这一年存在两个版本。回复 "
                            "/birth 之前 或 /birth 之后 来定档；"
                            "/birth 取消 放弃。")
                else:
                    hint = ("\n\n—— 回复 /birth 确认 落档；"
                            "/birth 取消 放弃。")
                return {"output": review["say"] + hint,
                        "birth_review": review}

            if head == "saves":
                data = self.saves_list()
                if not data["slots"]:
                    return {"output": "还没有任何存档槽位。"}
                lines = ["存档槽位："]
                for s in data["slots"]:
                    cur = " ▶当前" if s.get("id") == data["current"] else ""
                    lines.append(
                        f"  [{s.get('id')}] {s.get('name') or s.get('char_name')}"
                        f" · 时钟 {s.get('clock_year') or '?'}"
                        f" · {s.get('saved_at') or '?'}{cur}")
                lines.append("切换：/switch <编号>；网页端档案室可改名/删除。")
                return {"output": "\n".join(lines), "saves": data}

            if head == "switch":
                if not arg.strip():
                    return {"output": "用法：/switch <槽位编号>（先用 /saves 查看）"}
                return self.saves_switch(arg.strip())

            if head in ("skip", "跳过", "略过"):
                return self._skip_all()

            if head in ("read", "读", "看", "翻"):
                return self._read_readable(arg)

            if head in ("readables", "读物", "文档"):
                if not self.readables:
                    return {"output": "（还没有任何可读物）"}
                lines = ["所有可读物："]
                for r in self.readables:
                    mark = "✓" if r.get("read") else (
                        "✗" if r.get("missed") else "·")
                    lines.append(
                        f"  {mark} [{r['id']}] ({r['kind']}) {r['name']}  "
                        f"— {r.get('year','?')}"
                    )
                return {"output": "\n".join(lines),
                        "readables": self.readables}

            if head in ("go", "travel", "去", "前往"):
                return self._set_location(arg)

            if head in ("surge", "谜题", "涌动"):
                if not arg.strip():
                    if not self.surges:
                        return {"output": "（当前没有涌动。10 岁前不会有涌动。）"}
                    lines = ["当前涌动："]
                    for s in self.surges:
                        mark = "★" if s.get("id") == self.selected_surge else " "
                        lines.append(f" {mark} [{s.get('id')}] {s.get('name','?')}")
                        if s.get('mystery'):
                            lines.append(f"     谜题：{s['mystery']}")
                    lines.append("")
                    lines.append("用 /surge <id> 选中；/surge none 取消。")
                    return {"output": "\n".join(lines)}
                return self._select_surge(arg)

            if head == "clue":
                if not arg.strip():
                    return {"output": "用法：/clue <线索文本>"}
                clue = self._add_clue(arg.strip(), source="手动")
                return {"output": f"线索 [{clue['id']}] 已入库。",
                        "clue": clue, "clues": self.clues}

            if head == "clues":
                return {"output": "__CLUES__",
                        "clues": self.clues,
                        "pending": self.pending_clues,
                        "links": self.clue_links,
                        "quest_log": self.quest_log}

            if head == "take":
                return self._take_pending(arg.strip())

            if head == "drop":
                return self._drop_pending(arg.strip())

            if head == "link":
                ids = arg.strip().split()
                if len(ids) < 2:
                    return {"output": "用法：/link <id1> <id2>"}
                return self._link_clues(ids[0], ids[1])

            if head == "lookup":
                if not arg:
                    return {"output": "用法：/lookup 关键词"}
                hits = self.lore.search(arg.split(), per_kw=8)
                if not hits:
                    return {"output": "（档案库里没有相关记录）"}
                return {"output": "\n".join("· " + h for h in hits)}

            if head == "timeline":
                phases = self.lore.phases()
                stats = self.lore.stats()
                buf = [
                    f"档案库：{stats['episodes']} 条 / {stats['lines']} 行",
                    f"当前时钟：{self.clock_year} 年",
                    f"玩家出生：{self.arc.birth_year} 年",
                    f"玩家年龄：{self.player_age}",
                    f"当前锚点：{self.stage or '（未记录）'}",
                    "【阶段】",
                ]
                for p in phases:
                    buf.append(f"  {p['phase']}  ({p['n']} 条)")
                return {"output": "\n".join(buf)}

            if head == "thread":
                return {"output": "__THREADS__",
                        "threads": self.threads, "surges": self.surges}

            if head == "storm":
                return {"output": "__STORM__", "storm": self.storm.to_dict()}

            if head in ("storyline", "graph"):
                if not self.storyline or not self.storyline.has_graph():
                    return {"output": "（未检测到 uttu_* 故事线图谱）"}
                nodes = self.storyline.storyline_nodes(limit=30)
                if not nodes:
                    return {"output": "（图谱为空）"}
                lines = ["【故事线图谱】"]
                for n in nodes:
                    bits = []
                    if n.get("kind"):
                        bits.append(f"({n['kind']})")
                    if n.get("year"):
                        bits.append(str(n["year"]))
                    prefix = " ".join(bits)
                    lines.append(f"  - {prefix} {n['name']}".rstrip())
                return {"output": "\n".join(lines)}

            if head == "drift":
                a = arg.strip()
                if not a:
                    return {"output": f"档案引用强度 = {self.drift}",
                            "drift": self.drift}
                try:
                    v = int(a)
                    if 0 <= v <= 3:
                        self.drift = v
                        self._autosave()
                        return {"output": f"档案引用强度 → {v}", "drift": v}
                    return {"output": "取值 0–3"}
                except ValueError:
                    return {"output": "用法：/drift [0-3]"}

            if head == "stage":
                if not arg.strip():
                    return {"output": f"当前时间锚点：\n  {self.stage or '（未记录）'}",
                            "stage": self.stage}
                self.stage = arg.strip()
                self._autosave()
                return {"output": f"锚点已设：{self.stage}", "stage": self.stage}

            if head == "rollback":
                if not self.arc:
                    return {"output": "尚未建档"}
                era = self.storm.current()
                if era:
                    self.storm.add(self.storm.current_era,
                                   StormState.THRESHOLD - era["level"],
                                   reason="/rollback 手动触发")
                rb = self._storm_do_rollback()
                self._autosave()
                if not rb:
                    return {"output": "无法执行回退（当前无时代信息）"}
                snap = ""
                official = rb.get("official") or {}
                if official.get("title"):
                    snap = f"，吸附官方时段「{official['title']}」"
                return {"output": f"暴雨降临，{rb['tier_name']}级回退 "
                                   f"{rb['from_year']} → {rb['to_year']}"
                                   f"（倒退 {rb['years_back']} 年"
                                   f" · 超阈值+{rb['overshoot']}）{snap}",
                        "clock_year": self.clock_year,
                        "stage": self.stage,
                        "storm": self.storm.to_dict(),
                        "rollback": rb}

            if head == "card":
                return {"output": "__CARD__",
                        "card": self.arc.to_dict() if self.arc else None}

            return {"output": f"未知命令：/{head}（试试 /help）"}


COMMAND_HELP = """可用命令：
  /card            查看角色卡
  /clock [年份]    查看 / 拨动时钟（可回到过去、前往未来）
  /go <地点>       前往一个地点（如 /go 伦敦）
  /read [id|all]   读可读物（无参数则列出未读）
  /skip            明确跳过所有未读（双倍惩罚）
  /readables       列出所有可读物
  /surge [id]      查看 / 选中涌动（谜题）；/surge none 取消
  /birth [年份]    查看出生年；输入年份先由 AI 档案员核对（暴雨落点年份需
                   再选 /birth 之前 或 /birth 之后，其余年份 /birth 确认 落档）
  /saves           查看全部存档槽位
  /switch <编号>   切换到指定存档槽位
  /clue <文本>     手动录入一条线索
  /clues           打开线索墙
  /take <编号>     采纳待选线索（/take all 全采纳）
  /drop <编号>     丢弃待选线索（/drop all 全丢弃）
  /link <id1> <id2>  将两条线索串联验证
  /lookup 关键词   检索剧情库
  /timeline        时间线 / 统计
  /thread          查看暗流与涌动
  /storm           查看暴雨·时代病面板
  /storyline       查看故事线图谱（若已并入 uttu_* 表）
  /rollback        手动触发时代回退（暴雨降临）
  /drift [0-3]     档案引用强度
  /stage [锚点]    查看 / 手动指定锚点
  /nextday         进入下一天（同一年内推进一天叙事）
  /nextyear        进入下一年（时钟+1年）
  /help            显示本帮助"""


# ===========================================================================
# 内联 HTML
# ===========================================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0a0e1a">
<title>雨幕档案 · 1999</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html,body{margin:0;padding:0;height:100%;}
body{background:#0a0e1a;color:#d8dce8;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif;
  font-size:15px;line-height:1.75;overflow:hidden;}
.app{display:flex;flex-direction:column;height:100vh;height:100dvh;}
.topbar{display:flex;align-items:center;gap:8px;padding:10px 12px;
  padding-top:max(10px,env(safe-area-inset-top));
  background:#0d111d;border-bottom:1px solid #1c2233;flex-shrink:0;}
.icon-btn{background:transparent;border:1px solid #2a3048;color:#a8b0c8;
  width:36px;height:36px;border-radius:8px;font-size:16px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:all .15s;}
.icon-btn:hover,.icon-btn:active{background:#1a2033;color:#d8dce8;}
.title{flex:1;text-align:center;line-height:1.1;}
.title-main{display:block;font-size:14px;color:#c9a961;letter-spacing:2px;}
.title-sub{display:block;font-size:9px;color:#5a6078;letter-spacing:3px;margin-top:2px;}
.clock-btn{background:#141a2a;border:1px solid #7a6838;color:#c9a961;
  padding:6px 10px;border-radius:8px;font-size:12px;cursor:pointer;
  font-family:ui-monospace,Menlo,monospace;letter-spacing:1px;
  transition:all .15s;white-space:nowrap;}
.clock-btn:hover{background:#1a2033;}
.storm-btn{background:#141a2a;border:1px solid #2a3048;color:#a8b0c8;
  padding:6px 8px;border-radius:8px;font-size:11px;cursor:pointer;
  font-family:ui-monospace,monospace;transition:all .15s;
  white-space:nowrap;display:flex;align-items:center;gap:5px;}
.storm-btn.calm{border-color:#2a3048;color:#8890a8;}
.storm-btn.elevated{border-color:#3a4a2a;color:#c9d890;}
.storm-btn.warning{border-color:#7a6838;color:#d8b870;background:#1a1a0d;}
.storm-btn.critical{border-color:#7a2020;color:#e08a8a;background:#1a0d0d;
  animation:pulse 1.8s ease-in-out infinite;}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(224,138,138,.4);}
  50%{box-shadow:0 0 0 6px rgba(224,138,138,0);}}
.chat{flex:1;overflow-y:auto;padding:16px 14px 20px;scroll-behavior:smooth;}
.chat::-webkit-scrollbar{width:6px;}
.chat::-webkit-scrollbar-thumb{background:#2a3048;border-radius:3px;}
.msg{margin-bottom:16px;}
.msg-player{padding-left:12px;border-left:2px solid #c9a961;
  color:#e8d8a8;font-size:14px;}
.msg-player::before{content:attr(data-name);display:block;
  font-size:11px;color:#7a6838;letter-spacing:2px;margin-bottom:2px;}
.msg-narrator{padding-left:12px;border-left:2px solid #2a3048;
  color:#d8dce8;white-space:pre-wrap;word-break:break-word;}
.msg-narrator .nv{color:#8a93a8;}
.msg-narrator .act{color:#a8b0c8;font-style:italic;}
.msg-system{background:#141a2a;border:1px solid #232a42;
  border-radius:8px;padding:10px 12px;color:#8890a8;font-size:13px;
  white-space:pre-wrap;word-break:break-word;margin:12px 0;}
.msg-rollback{background:#1a0d0d;border:1px solid #7a2020;
  border-radius:8px;padding:10px 12px;color:#e08a8a;font-size:13px;
  margin:10px 0;white-space:pre-wrap;word-break:break-word;
  border-left:3px solid #d05050;letter-spacing:.3px;}
.clue-toast{background:#161c2c;border:1px solid #4a5a2a;border-radius:8px;
  padding:10px 12px;color:#c9d890;font-size:13px;margin:8px 0;
  white-space:pre-wrap;word-break:break-word;}
.clue-toast b{color:#c9a961;}
.typing{display:flex;gap:4px;padding:8px 12px;color:#5a6078;font-size:13px;
  align-items:center;}
.typing .dot{width:6px;height:6px;border-radius:50%;background:#5a6078;
  animation:bounce 1.4s infinite;}
.typing .dot:nth-child(2){animation-delay:.2s;}
.typing .dot:nth-child(3){animation-delay:.4s;}
@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:.4;}
  30%{transform:translateY(-4px);opacity:1;}}
.composer{background:#0d111d;border-top:1px solid #1c2233;
  padding:8px 10px;padding-bottom:max(8px,env(safe-area-inset-bottom));
  flex-shrink:0;}
.commands{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px;
  scrollbar-width:none;}
.commands::-webkit-scrollbar{display:none;}
.commands button{flex-shrink:0;background:#141a2a;border:1px solid #232a42;
  color:#8890a8;padding:5px 11px;border-radius:14px;font-size:12px;
  cursor:pointer;transition:all .15s;white-space:nowrap;}
.commands button:hover,.commands button:active{
  background:#1a2033;color:#c9a961;border-color:#7a6838;}
.input-row{display:flex;gap:8px;align-items:flex-end;}
#input{flex:1;background:#141a2a;border:1px solid #232a42;color:#d8dce8;
  padding:10px 12px;border-radius:10px;font-family:inherit;font-size:15px;
  line-height:1.5;resize:none;max-height:120px;min-height:42px;
  outline:none;transition:border-color .15s;}
#input:focus{border-color:#7a6838;}
.send-btn{background:#c9a961;color:#0a0e1a;border:none;padding:0 18px;
  height:42px;border-radius:10px;font-size:14px;font-weight:600;
  cursor:pointer;transition:all .15s;flex-shrink:0;}
.send-btn:hover{background:#d8b870;}
.send-btn:disabled{background:#3a3a3a;color:#666;cursor:not-allowed;}
.panel-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);
  backdrop-filter:blur(2px);z-index:50;display:none;align-items:stretch;
  justify-content:flex-end;}
.panel-overlay.active{display:flex;}
.panel{background:#0d111d;border-left:1px solid #1c2233;
  width:min(400px,90vw);height:100%;display:flex;flex-direction:column;
  animation:slideIn .2s ease-out;}
@keyframes slideIn{from{transform:translateX(100%);}to{transform:translateX(0);}}
.panel-head{padding:12px 16px;padding-top:max(12px,env(safe-area-inset-top));
  border-bottom:1px solid #1c2233;display:flex;align-items:center;
  justify-content:space-between;color:#c9a961;font-size:14px;
  letter-spacing:2px;}
.panel-body{flex:1;overflow-y:auto;padding:16px;font-size:14px;
  color:#b8c0d4;}
.panel-body::-webkit-scrollbar{width:6px;}
.panel-body::-webkit-scrollbar-thumb{background:#2a3048;border-radius:3px;}
.card-row{margin-bottom:12px;}
.card-label{font-size:10px;color:#5a6078;letter-spacing:2px;
  margin-bottom:2px;}
.card-value{color:#d8dce8;word-break:break-word;line-height:1.6;}
.card-value.dim{color:#8890a8;font-size:13px;}
.setting-row{margin:16px 0;}
.setting-label{font-size:11px;color:#5a6078;letter-spacing:2px;
  margin-bottom:6px;}
.btn-row{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;}
.btn{flex:1;min-width:80px;background:#141a2a;border:1px solid #232a42;
  color:#b8c0d4;padding:10px 12px;border-radius:8px;font-size:13px;
  cursor:pointer;transition:all .15s;font-family:inherit;}
.btn:hover{background:#1a2033;color:#c9a961;border-color:#7a6838;}
.btn.primary{background:#c9a961;color:#0a0e1a;border:none;}
.btn.danger{background:#2a1418;border-color:#4a1f28;color:#e08a8a;}
.thread-item{padding:12px 0;border-bottom:1px solid #1c2233;}
.thread-item:last-child{border-bottom:none;}
.thread-item.surge{border-left:2px solid #c9a961;padding-left:10px;
  margin-left:-10px;}
.thread-name{color:#c9a961;font-size:14px;margin-bottom:6px;}
.thread-name.surge{color:#d8b870;}
.thread-field{font-size:12px;color:#8890a8;margin:4px 0;line-height:1.6;}
.thread-field b{color:#5a6078;font-weight:normal;margin-right:6px;}
.clue-item{padding:10px 12px;background:#141a2a;border:1px solid #232a42;
  border-radius:8px;margin-bottom:8px;cursor:pointer;transition:all .15s;}
.clue-item:hover{border-color:#7a6838;}
.clue-item.selected{border-color:#c9a961;background:#1a2033;}
.clue-id{color:#c9a961;font-family:ui-monospace,monospace;font-size:11px;
  margin-right:6px;}
.clue-text{color:#d8dce8;font-size:13px;line-height:1.6;}
.clue-meta{color:#5a6078;font-size:10px;margin-top:4px;}
.link-item{padding:8px 10px;background:#161c2c;border-left:2px solid #c9a961;
  margin-bottom:6px;border-radius:4px;font-size:12px;color:#b8c0d4;}
.link-verdict{color:#c9a961;font-family:ui-monospace,monospace;}
.storm-era{padding:10px 12px;background:#141a2a;border:1px solid #232a42;
  border-radius:8px;margin-bottom:10px;}
.storm-era.current{border-color:#c9a961;}
.storm-era.sealed{opacity:.55;}
.storm-era-name{color:#d8dce8;font-size:13px;margin-bottom:4px;
  display:flex;justify-content:space-between;align-items:center;}
.storm-era-name .tag{font-size:10px;color:#5a6078;}
.storm-era-bar{font-family:ui-monospace,monospace;font-size:12px;
  letter-spacing:1px;margin:4px 0;color:#8890a8;}
.storm-era-bar.elevated{color:#c9d890;}
.storm-era-bar.warning{color:#d8b870;}
.storm-era-bar.critical{color:#e08a8a;}
.storm-era-label{font-size:11px;color:#5a6078;}
.storm-current{background:#1a0d0d;border:1px solid #7a2020;border-radius:8px;
  padding:14px;margin-bottom:16px;}
.storm-current .era-title{color:#d8b870;font-size:13px;
  letter-spacing:2px;margin-bottom:6px;}
.storm-current .era-bar{font-family:ui-monospace,monospace;
  font-size:14px;letter-spacing:2px;color:#e08a8a;margin:6px 0;}
.storm-current .era-num{color:#e08a8a;font-family:ui-monospace,monospace;
  font-size:20px;text-align:right;}
.tier-badge{display:inline-block;padding:2px 8px;border-radius:10px;
  font-size:11px;font-family:ui-monospace,monospace;margin-left:6px;}
.tier-1{background:#3a4a2a;color:#c9d890;}
.tier-2{background:#4a4a1a;color:#e0d090;}
.tier-3{background:#4a3a1a;color:#e0b070;}
.tier-4{background:#4a1a1a;color:#e08a8a;}
.tier-5{background:#5a0d0d;color:#f0a0a0;}
.official-tag{display:inline-block;padding:1px 6px;border-radius:8px;
  font-size:10px;background:#141a2a;color:#c9a961;
  border:1px solid #4a3a1a;margin-left:6px;}
.modal{position:fixed;inset:0;background:rgba(5,8,15,.88);
  backdrop-filter:blur(4px);z-index:100;display:none;align-items:center;
  justify-content:center;padding:20px;}
.modal.active{display:flex;}
.modal-content{background:#0d111d;border:1px solid #1c2233;
  border-radius:14px;padding:24px;width:100%;max-width:420px;
  max-height:88vh;overflow-y:auto;animation:fadeUp .25s ease-out;}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px);}to{opacity:1;}}
.modal-content h2{margin:0 0 6px;color:#c9a961;font-size:18px;
  letter-spacing:3px;font-weight:normal;}
.modal-content .sub{font-size:11px;color:#5a6078;letter-spacing:2px;
  margin-bottom:20px;}
.modal-content label{display:block;font-size:11px;color:#7a8298;
  letter-spacing:2px;margin:14px 0 6px;}
.modal-content input,.modal-content textarea{width:100%;
  background:#141a2a;border:1px solid #232a42;color:#d8dce8;
  padding:10px 12px;border-radius:8px;font-family:inherit;font-size:14px;
  line-height:1.5;outline:none;transition:border-color .15s;}
.modal-content input:focus,.modal-content textarea:focus{
  border-color:#7a6838;}
.modal-content textarea{resize:vertical;min-height:70px;}
.modal-content .primary{width:100%;margin-top:22px;background:#c9a961;
  color:#0a0e1a;border:none;padding:12px;border-radius:10px;font-size:15px;
  font-weight:600;cursor:pointer;font-family:inherit;letter-spacing:2px;
  transition:all .15s;}
.modal-content .primary:hover{background:#d8b870;}
.modal-content .primary:disabled{background:#3a3a3a;color:#666;}
.modal-hint{margin-top:10px;font-size:11px;color:#5a6078;
  line-height:1.7;letter-spacing:1px;}
.clock-slider{width:100%;accent-color:#c9a961;}
.clock-years{display:flex;justify-content:space-between;
  font-size:11px;color:#5a6078;margin-top:4px;}
.clock-value{font-size:28px;color:#c9a961;text-align:center;
  font-family:ui-monospace,Menlo,monospace;margin:10px 0;
  font-variant-numeric:tabular-nums;}
.clock-age{text-align:center;color:#8890a8;font-size:12px;
  margin-bottom:16px;}
.welcome{text-align:center;padding:40px 20px;color:#5a6078;}
.welcome-line{height:1px;background:#1c2233;margin:16px 0;}
.welcome-title{color:#c9a961;font-size:16px;letter-spacing:6px;
  margin-bottom:6px;}
.welcome-sub{font-size:10px;letter-spacing:4px;color:#4a5068;}
.welcome-note{margin-top:24px;font-size:12px;line-height:1.9;
  text-align:left;max-width:360px;margin-left:auto;margin-right:auto;}
.format-legend{margin-top:20px;padding:12px 14px;background:#0d111d;
  border:1px solid #1c2233;border-radius:8px;text-align:left;
  max-width:340px;margin-left:auto;margin-right:auto;}
.legend-title{font-size:10px;color:#5a6078;letter-spacing:3px;
  margin-bottom:8px;text-align:center;}
.legend-item{font-size:12px;color:#8890a8;line-height:1.9;}
.legend-item code{color:#c9a961;background:#141a2a;padding:1px 6px;
  border-radius:4px;font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:11px;margin-right:8px;}
.error-bubble{background:#2a1418;border:1px solid #4a1f28;color:#d88a8a;
  padding:10px 12px;border-radius:8px;font-size:13px;
  white-space:pre-wrap;margin:8px 0;}
.hint-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
.hint-chip{background:#141a2a;border:1px solid #232a42;color:#8890a8;
  padding:3px 8px;border-radius:12px;font-size:11px;}
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <button class="icon-btn" id="btn-menu" aria-label="菜单">☰</button>
    <div class="title">
      <span class="title-main">雨幕档案</span>
      <span class="title-sub" id="title-user">R E V E R S E : 1 9 9 9</span>
    </div>
    <button class="clock-btn" id="btn-clock">0000</button>
    <button class="storm-btn calm" id="btn-storm" title="暴雨时代病">☂ <span id="storm-mini">平静</span></button>
    <button class="icon-btn" id="btn-clue" aria-label="线索墙">◇</button>
    <button class="icon-btn" id="btn-thread" aria-label="暗流">◈</button>
  </header>
  <main class="chat" id="chat"></main>
  <footer class="composer">
    <div class="commands">
      <button data-cmd="/card">角色卡</button>
      <button data-cmd="/clues">线索墙</button>
      <button data-cmd="/thread">暗流</button>
      <button data-cmd="/storm">暴雨</button>
      <button data-cmd="/clock">时钟</button>
      <button data-cmd="/timeline">时间线</button>
      <button data-cmd="/help">帮助</button>
      <button data-action="nextday">下一天</button>
      <button data-action="nextmonth">下一月</button>
      <button data-action="nextyear">下一年</button>
    </div>
    <div class="input-row">
      <textarea id="input" rows="1" placeholder="说点什么，或做点什么…"></textarea>
      <button id="send" class="send-btn">发送</button>
    </div>
  </footer>
</div>

<div class="panel-overlay" id="panel-menu">
  <div class="panel">
    <div class="panel-head"><span>档 案 室</span>
      <button class="icon-btn" data-close>✕</button></div>
    <div class="panel-body" id="menu-body"></div>
  </div>
</div>

<div class="panel-overlay" id="panel-thread">
  <div class="panel">
    <div class="panel-head"><span>暗 流 · 涌 动</span>
      <button class="icon-btn" data-close>✕</button></div>
    <div class="panel-body" id="thread-body"></div>
  </div>
</div>

<div class="panel-overlay" id="panel-clue">
  <div class="panel">
    <div class="panel-head"><span>线 索 墙</span>
      <button class="icon-btn" data-close>✕</button></div>
    <div class="panel-body" id="clue-body"></div>
  </div>
</div>

<div class="panel-overlay" id="panel-storm">
  <div class="panel">
    <div class="panel-head"><span>暴 雨 · 时 代 病</span>
      <button class="icon-btn" data-close>✕</button></div>
    <div class="panel-body" id="storm-body"></div>
  </div>
</div>

<div class="modal" id="modal-login">
  <div class="modal-content">
    <h2>登 录 档 案 室</h2>
    <div class="sub">输入用户名 · 存档独立保存</div>
    <label>用户名</label>
    <input id="login-name" type="text"
           placeholder="例如：vertin / 林 / sonetto">
    <div class="modal-hint">
      用户名只用来区分存档。<br>
      换一个名字 = 换一份全新的档案，互不影响。<br>
      不需要密码。
    </div>
    <button id="btn-login" class="primary" type="button">进 入</button>
  </div>
</div>

<div class="modal" id="modal-new">
  <div class="modal-content">
    <h2>建 立 档 案</h2>
    <div class="sub">从 0 岁 开 始 · 时 间 由 你 拨 动</div>
    <label>姓名</label>
    <input id="new-name" type="text" placeholder="如：林书言 / 沈砚秋">
    <label>性别</label>
    <input id="new-gender" type="text" placeholder="男 / 女 / 其他">
    <label>出生年份（= 故事起始年）</label>
    <input id="new-birth" type="number" placeholder="如：1960" value="1960">
    <div class="modal-hint">玩家将从此年的 0 岁开始模拟。
      之后可在顶栏时钟里拨动年份，年龄自动增长。<br>
      输入年份后由 AI 档案员对照暴雨时间轴核对；若恰逢暴雨回溯落点
      （如 1996 = 第一次暴雨落点），还需选择出生于暴雨之前还是之后。</div>
    <label>出生地 · 主要舞台（留空则随机）</label>
    <input id="new-birthplace" type="text"
           placeholder="如：伦敦 · 白教堂区 / 上海 · 永福里 / 爱丁堡">
    <div class="modal-hint">这场戏会锚定在这里展开。不填则系统随机决定。</div>
    <label>与维尔汀的关系（留空则随机）</label>
    <textarea id="new-relation" rows="2"
      placeholder="如：我和维尔汀是亲姐妹 / 尚未见过她 / 她在等我长大"></textarea>
    <div class="modal-hint">写明关系后系统会围绕它展开；不填则由系统生成。</div>
    <label>神秘术名</label>
    <input id="new-ability" type="text" placeholder="如：无字信">
    <label>神秘术描述</label>
    <textarea id="new-desc" rows="3" placeholder="越具体越好。"></textarea>
    <button id="btn-custom-toggle" type="button"
      style="margin-top:14px;background:none;border:1px dashed #3a4358;color:#7a8298;border-radius:8px;padding:8px 10px;width:100%;font-size:12px;cursor:pointer;letter-spacing:2px;">自 定 义 模 式 ▾（可只填一部分，留空的由 AI 编写）</button>
    <div id="custom-fields" style="display:none;">
      <div class="modal-hint" style="margin:8px 0 2px;">以下身份字段均可自行编写；
        留空的字段由 AI 补全，填写了的一律以你为准。</div>
      <label>编号</label>
      <input id="cf-code_name" type="text" placeholder="如：X-317">
      <label>隶属</label>
      <input id="cf-affiliation" type="text" placeholder="如：圣洛夫基金会 / 拉普拉斯观测站">
      <label>出生地</label>
      <input id="cf-birthplace" type="text" placeholder="如：英国伦敦 · 苏活区">
      <label>出生地注记</label>
      <input id="cf-birthplace_note" type="text" placeholder="一句话描述此地">
      <label>血统</label>
      <input id="cf-bloodline" type="text" placeholder="纯血 / 半血 / 来历不明，可加来源">
      <label>家庭</label>
      <textarea id="cf-family" rows="2" placeholder="家庭成员与家境"></textarea>
      <label>性格</label>
      <textarea id="cf-personality" rows="2" placeholder="性格底色（0 岁可以是尚待成形）"></textarea>
      <label>外貌</label>
      <textarea id="cf-appearance" rows="2" placeholder="静态外貌（含婴儿期特征）"></textarea>
      <label>时间线（与维尔汀的世代）</label>
      <input id="cf-vertin_offset" type="text" placeholder="早于维尔汀 / 与维尔汀同代 / 晚于维尔汀">
      <label>与维尔汀的关系</label>
      <textarea id="cf-relation" rows="2" placeholder="一到两句"></textarea>
      <label>咒语</label>
      <textarea id="cf-ability_incantation" rows="2"
        placeholder="「English original.」——「中文诗意译文。」"></textarea>
    </div>
    <button id="btn-create" class="primary" type="button">生 成</button>
  </div>
</div>

<div class="modal" id="modal-clock">
  <div class="modal-content">
    <h2>时 钟</h2>
    <div class="sub">拨 动 时 间 · 可 回 到 过 去 · 也 可 前 往 未 来</div>
    <div class="clock-value" id="clock-display">0000-00-00</div>
    <div class="clock-age" id="clock-age">年龄：—</div>
    <label style="font-size:11px;color:#7a8298;letter-spacing:2px;
      display:block;margin:6px 0 2px;">年份</label>
    <input type="range" class="clock-slider" id="clock-range"
           min="1800" max="2100" step="1" value="1999">
    <div class="clock-years">
      <span>1800</span><span>1950</span><span>2100</span>
    </div>
    <label style="font-size:11px;color:#7a8298;letter-spacing:2px;
      display:block;margin:10px 0 2px;">月份</label>
    <input type="range" class="clock-slider" id="clock-month"
           min="1" max="12" step="1" value="1">
    <div class="clock-years"><span>1 月</span><span>12 月</span></div>
    <label style="font-size:11px;color:#7a8298;letter-spacing:2px;
      display:block;margin:10px 0 2px;">日</label>
    <input type="range" class="clock-slider" id="clock-day"
           min="1" max="30" step="1" value="1">
    <div class="clock-years"><span>1 日</span><span>30 日</span></div>
    <div class="hint-row">
      <span class="hint-chip" id="chip-birth">出生年：—</span>
      <span class="hint-chip" id="chip-now">当前：—</span>
      <span class="hint-chip" id="chip-storm">暴雨：—</span>
    </div>
    <button class="primary" id="btn-clock-go" type="button">拨 动 时 钟</button>
  </div>
</div>

<script>
(function(){
'use strict';
const $ = s => document.querySelector(s);
const st = {
  username: (localStorage.getItem('rain_user') || '').trim(),
  busy: false, arc: null, drift: 2, threads: [], surges: [],
  stage: '', clock_year: 1999, clock_month: 1, clock_day: 1,
  birth_year: 1960, age: null,
  clues: [], pending_clues: [], clue_links: [], quest_log: [],
  readables: [],
  selectedClues: new Set(),
  storm: {level:0, severity:'calm', label:'平静', eras:{}, log:[], rollbacks:[]},
};
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function stripMeta(text) {
  return String(text == null ? '' : text)
    .replace(/【时间锚点[:：][\s\S]*?】/g, '')
    .replace(/【线索候选】[\s\S]*?(?=【|$)/g, '')
    .replace(/<\/?(?:size|color|b|i|u|s|em|strong|br|p|div|span|align|indent)(?:=[^>]*)?\s*\/?>/gi, '')
    .replace(/<\/?[a-zA-Z][^>]{0,40}>/g, '')
    .trim();
}
const chat = $('#chat'); const input = $('#input'); const sendBtn = $('#send');

function appendMsg(role, text, name) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + role;
  if (role === 'player') div.dataset.name = name || '你';
  if (role === 'narrator') {
    const lines = stripMeta(text).split('\n');
    div.innerHTML = lines.map(line => {
      const t = esc(line);
      if (t.startsWith('//')) return '<span class="nv">' + t + '</span>';
      return t.replace(/\(([^()]*)\)/g, '<span class="act">($1)</span>');
    }).join('\n');
  } else {
    div.textContent = text == null ? '' : String(text);
  }
  div.style.whiteSpace = 'pre-wrap'; div.style.wordBreak = 'break-word';
  chat.appendChild(div); chat.scrollTop = chat.scrollHeight; return div;
}
function appendSystem(t){const d=document.createElement('div');d.className='msg msg-system';d.textContent=String(t??'');chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}
function appendError(t){const d=document.createElement('div');d.className='error-bubble';d.textContent=String(t??'');chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}

function appendRollback(rb){
  if(!rb)return;
  const tier = rb.tier_name || '回退';
  const tierIdx = {"微溃":1,"失序":2,"崩解":3,"溃灭":4,"归墟":5}[tier] || 1;
  const d=document.createElement('div');d.className='msg-rollback';
  let html = `<div style="margin-bottom:6px;">
    <span style="color:#d05050;font-size:15px;">☂ 暴雨降临</span>
    <span class="tier-badge tier-${tierIdx}">${esc(tier)}级</span>
  </div>`;
  html += `<div style="color:#f0a0a0;">${rb.from_year} 年 → ${rb.to_year} 年`
    + `（倒退 <b>${rb.years_back}</b> 年）</div>`;
  if(rb.overshoot != null){
    html += `<div style="color:#8890a8;font-size:12px;margin-top:4px;">`
      + `超阈值程度：+${rb.overshoot}</div>`;
  }
  if(rb.official && rb.official.title){
    html += `<div style="margin-top:6px;">`
      + `<span class="official-tag">吸附官方时段</span> `
      + `<span style="color:#c9a961;">${esc(rb.official.title)}`
      + `（${rb.official.start_year}–${rb.official.end_year}）</span></div>`;
  }
  html += `<div style="color:#5a6078;font-size:11px;margin-top:6px;">`
    + `时代 ${esc(rb.from_era)} 已被抹除 · 玩家进入 ${esc(rb.to_era)}`
    + `${rb.to_era_name ? ' · ' + esc(rb.to_era_name) : ''}</div>`;
  d.innerHTML = html;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;
}
function appendClueToast(pending){
  // 【v3.4】不再主动弹出「发现线索候选」。
  // 线索仍会被系统收集到 /clues 面板，玩家可自行打开查看采纳。
  return;
}
function showTyping(){hideTyping();const d=document.createElement('div');d.className='typing';d.id='typing';d.innerHTML='<span class="dot"></span><span class="dot"></span><span class="dot"></span><span style="margin-left:8px">…推理中</span>';chat.appendChild(d);chat.scrollTop=chat.scrollHeight;}
function hideTyping(){const e=document.getElementById('typing');if(e)e.remove();}
function setBusy(v){st.busy=v;if(sendBtn){sendBtn.disabled=v;sendBtn.textContent=v?'……':'发送';}}

async function api(url,body,timeoutMs){
  timeoutMs=timeoutMs||240000;
  const ctrl=new AbortController();const timer=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{
    const _h={'Content-Type':'application/json'};
    if(st.username)_h['X-User']=st.username;
    const r=await fetch(url,{method:body===undefined?'GET':'POST',
      headers:_h,
      body:body===undefined?undefined:JSON.stringify(body),signal:ctrl.signal});
    clearTimeout(timer);
    if(!r.ok){let d='';try{d=await r.text();}catch(e){}throw new Error('HTTP '+r.status+(d?(' · '+d.slice(0,200)):''));}
    return await r.json();
  }catch(e){clearTimeout(timer);if(e.name==='AbortError')throw new Error('请求超时');throw e;}
}

function applyStorm(s){
  if(!s)return;
  st.storm = Object.assign(st.storm, s);
  const btn=document.getElementById('btn-storm');
  const mini=document.getElementById('storm-mini');
  if(!btn)return;
  btn.className='storm-btn '+(st.storm.severity||'calm');
  if(mini)mini.textContent=st.storm.label||'平静';
}

let _pendingCreate=null;

function ensureModalStatus(btn){
  let statusEl=document.getElementById('modal-status');
  if(!statusEl){statusEl=document.createElement('div');statusEl.id='modal-status';
    statusEl.style.cssText='margin-top:14px;padding:10px 12px;border-radius:8px;background:#141a2a;border:1px solid #232a42;color:#8890a8;font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-word;';
    btn.parentNode.insertBefore(statusEl,btn.nextSibling);}
  statusEl.style.display='block';
  return statusEl;
}

const CUSTOM_FIELD_IDS=['code_name','affiliation','birthplace','birthplace_note',
  'bloodline','family','personality','appearance','vertin_offset','relation',
  'ability_incantation'];

async function doLogin(){
  const inp=document.getElementById('login-name');
  const name=(inp.value||'').trim();
  if(!name){alert('请输入用户名');return;}
  st.username=name;
  localStorage.setItem('rain_user',name);
  setUserTitle(name);
  document.getElementById('modal-login').classList.remove('active');
  try{
    const r=await api('/api/login',{user:name});
    if(r.error){appendError('登录失败：'+r.error);return;}
    const snap=r.snapshot||{};
    st.arc=snap.arc||null;
    st.threads=snap.threads||[];
    st.surges=snap.surges||[];
    st.clues=snap.clues||[];
    st.pending_clues=snap.pending_clues||[];
    st.clue_links=snap.clue_links||[];
    st.quest_log=snap.quest_log||[];
    st.stage=snap.stage||'';
    st.clock_year=snap.clock_year;
    st.clock_month=snap.clock_month||1;
    st.clock_day=snap.clock_day||1;
    st.birth_year=snap.birth_year;
    st.age=snap.age;
    st.location=snap.location||'';
    st.selected_surge=snap.selected_surge||null;
    applyStorm(snap.storm);
    updateClockDisplay();
    chat.innerHTML='';
    if(r.has_game && snap.history && snap.history.length){
      for(const msg of snap.history){
        if(msg.role==='player')appendMsg('player',msg.text,
          snap.arc?snap.arc.name:'你');
        else appendMsg('narrator',msg.text);
      }
    } else if(r.has_game){
      appendSystem('（该用户名已有存档，但暂无历史。可继续对话或 /help。）');
    } else {
      renderWelcome();
      document.getElementById('modal-new').classList.add('active');
    }
  }catch(e){appendError('登录失败：'+(e.message||e));}
}

async function createCharacter(){
  const btn=document.getElementById('btn-create');
  if(!btn||btn.disabled)return;
  const nameEl=$('#new-name'),genderEl=$('#new-gender'),
        birthEl=$('#new-birth'),
        abEl=$('#new-ability'),descEl=$('#new-desc');
  const statusEl=ensureModalStatus(btn);
  statusEl.style.background='#141a2a';
  statusEl.style.borderColor='#232a42';statusEl.style.color='#8890a8';
  const birthYear=parseInt(birthEl.value,10)||1960;
  const payload={
    name:nameEl.value.trim()||'无名',
    gender:genderEl.value.trim()||'不明',
    birth_year:birthYear,
    start_year:birthYear,
    ability_name:abEl.value.trim()||'未命名术式',
    ability_desc:descEl.value.trim()||'效果不明。',
  };
  const custom={};
  CUSTOM_FIELD_IDS.forEach(k=>{
    const el=document.getElementById('cf-'+k);
    if(el&&el.value.trim())custom[k]=el.value.trim();
  });
  if(Object.keys(custom).length)payload.custom=custom;
  statusEl.textContent='AI 档案员正在对照暴雨时间轴核对出生年份……';
  btn.disabled=true;btn.textContent='核 对 中……';
  try{
    const review=await api('/api/birthyear/check',{year:birthYear},180000);
    if(review.error)throw new Error(review.error);
    _pendingCreate={payload:payload,review:review};
    btn.disabled=false;btn.textContent='重 新 核 对';
    showBirthReview(review);
  }catch(e){
    const msg=(e&&e.message)?e.message:String(e);
    statusEl.style.background='#2a1418';statusEl.style.borderColor='#4a1f28';
    statusEl.style.color='#d88a8a';
    statusEl.textContent=`年份核对失败：${msg}`;
    btn.disabled=false;btn.textContent='重 试';
  }
}

function showBirthReview(review){
  const statusEl=document.getElementById('modal-status');
  if(!statusEl||!_pendingCreate)return;
  statusEl.style.background='#141a2a';statusEl.style.borderColor='#4a3a1a';
  statusEl.style.color='#c9b98a';
  statusEl.textContent='';
  const p=document.createElement('div');
  p.style.marginBottom='10px';
  p.textContent='【AI 档案员】'+(review.say||'');
  statusEl.appendChild(p);
  if(review.facts){
    const f=document.createElement('div');
    f.style.cssText='margin-bottom:10px;font-size:11px;color:#7a8298;';
    f.textContent=review.facts;
    statusEl.appendChild(f);
  }
  const row=document.createElement('div');
  row.style.cssText='display:flex;gap:8px;flex-wrap:wrap;';
  const mk=function(label,cb){
    const b=document.createElement('button');
    b.type='button';b.textContent=label;
    b.style.cssText='flex:1;min-width:120px;padding:8px 10px;border-radius:8px;border:1px solid #c9a961;background:#1a2030;color:#c9a961;font-size:12px;cursor:pointer;';
    b.onclick=cb;return b;};
  if(review.ambiguous&&review.options&&review.options.length){
    review.options.forEach(function(o){
      row.appendChild(mk(o.label,function(){confirmBirth(o.key);}));
    });
  }else{
    row.appendChild(mk('以此年出生',function(){confirmBirth(null);}));
  }
  statusEl.appendChild(row);
}

async function confirmBirth(side){
  if(!_pendingCreate)return;
  const statusEl=document.getElementById('modal-status');
  const payload=_pendingCreate.payload;
  statusEl.textContent='正在定档出生年……';
  try{
    const conf=await api('/api/birthyear/confirm',
      {year:payload.birth_year,side:side},120000);
    if(conf.error)throw new Error(conf.error);
    payload.birth_side=conf.side||'';
    payload.birth_context=conf.birth_context||'';
    await doCreate(payload);
  }catch(e){
    statusEl.style.background='#2a1418';statusEl.style.borderColor='#4a1f28';
    statusEl.style.color='#d88a8a';
    statusEl.textContent='定档失败：'+((e&&e.message)?e.message:String(e));
    const btn=document.getElementById('btn-create');
    if(btn){btn.disabled=false;btn.textContent='重 试';}
  }
}

async function doCreate(payload){
  const btn=document.getElementById('btn-create');
  const statusEl=ensureModalStatus(btn);
  statusEl.style.background='#141a2a';
  statusEl.style.borderColor='#232a42';statusEl.style.color='#8890a8';
  statusEl.textContent='正在编纂身份档案……';
  if(btn){btn.disabled=true;btn.textContent='编 纂 中……';}
  const t0=Date.now();
  const ticker=setInterval(()=>{const s=Math.round((Date.now()-t0)/1000);
    statusEl.textContent=`正在编纂身份档案……已等待 ${s} 秒`;},1000);
  try{
    const birthYear=payload.birth_year;
    const data=await api('/api/new',payload,300000);
    clearInterval(ticker);
    if(data.error)throw new Error(data.error);
    document.getElementById('modal-new').classList.remove('active');
    chat.innerHTML='';renderWelcome();
    st.arc=data.card;st.threads=data.threads||[];st.surges=data.surges||[];
    st.clock_year=data.clock_year;st.birth_year=birthYear;st.age=data.age;
    applyStorm(data.storm);
    updateClockDisplay();renderMenu();
    const ph=appendMsg('narrator','（正在拉开帷幕……）');
    ph.id='opening-placeholder';
    try{
      const o=await api('/api/open',{},600000);
      const p=document.getElementById('opening-placeholder');
      if(p)p.remove();
      if(o.error){appendError('开场失败：'+o.error);}
      else{
        if(o.opening)appendMsg('narrator',o.opening);
        if(o.threads)st.threads=o.threads;
        if(o.surges)st.surges=o.surges;
        if(o.stage)st.stage=o.stage;
        if(o.clock_year)st.clock_year=o.clock_year;
        if(o.pending_clues)st.pending_clues=o.pending_clues;
        applyStorm(o.storm);
        updateClockDisplay();
        appendClueToast(st.pending_clues);
      }
    }catch(e){const p=document.getElementById('opening-placeholder');
      if(p)p.remove();appendError('开场失败：'+(e.message||e));}
  }catch(e){
    clearInterval(ticker);
    const msg=(e&&e.message)?e.message:String(e);
    statusEl.style.background='#2a1418';statusEl.style.borderColor='#4a1f28';
    statusEl.style.color='#d88a8a';
    statusEl.textContent=`建档失败：${msg}`;
    btn.disabled=false;btn.textContent='重 试';
  }
}

async function send(){
  const text=input.value.trim();
  if(!text||st.busy)return;
  if(!st.arc){appendSystem('还没有建立档案。');return;}
  input.value='';input.style.height='auto';
  if(text.includes('进入下一天') || text.includes('下一天')){
    appendMsg('player',text,st.arc.name);
    setBusy(true);showTyping();
    try{const data=await api('/api/nextday',{});hideTyping();
      if(data.error)appendError('进入下一天失败：'+data.error);
      else{
        appendMsg('narrator',data.reply);
        if(data.threads)st.threads=data.threads;
        if(data.surges)st.surges=data.surges;
        if(data.stage)st.stage=data.stage;
        if(data.clock_year)st.clock_year=data.clock_year;
        if(data.pending_clues){
          const before=st.pending_clues.length;
          st.pending_clues=data.pending_clues;
          if(st.pending_clues.length>before)appendClueToast(st.pending_clues);
        }
        if(data.rollback)appendRollback(data.rollback);
        applyStorm(data.storm);
        updateClockDisplay();
      }
    }catch(e){hideTyping();appendError('请求失败：'+(e.message||e));}
    finally{setBusy(false);}
    return;
  }
  if(text.includes('进入下一月') || text.includes('下一月')){
    appendMsg('player',text,st.arc.name);
    setBusy(true);showTyping();
    try{const data=await api('/api/nextmonth',{});hideTyping();
      if(data.error)appendError('进入下一月失败：'+data.error);
      else{
        appendMsg('narrator',data.reply);
        if(data.threads)st.threads=data.threads;
        if(data.surges)st.surges=data.surges;
        if(data.stage)st.stage=data.stage;
        if(data.clock_year)st.clock_year=data.clock_year;
        if(data.clock_month)st.clock_month=data.clock_month;
        if(data.pending_clues){
          const before=st.pending_clues.length;
          st.pending_clues=data.pending_clues;
          if(st.pending_clues.length>before)appendClueToast(st.pending_clues);
        }
        if(data.rollback)appendRollback(data.rollback);
        applyStorm(data.storm);
        updateClockDisplay();
      }
    }catch(e){hideTyping();appendError('请求失败：'+(e.message||e));}
    finally{setBusy(false);}
    return;
  }
  if(text.includes('进入下一年') || text.includes('下一年')){
    appendMsg('player',text,st.arc.name);
    setBusy(true);showTyping();
    try{const data=await api('/api/nextyear',{});hideTyping();
      if(data.error)appendError('进入下一年失败：'+data.error);
      else{
        appendMsg('narrator',data.reply);
        if(data.threads)st.threads=data.threads;
        if(data.surges)st.surges=data.surges;
        if(data.stage)st.stage=data.stage;
        if(data.clock_year)st.clock_year=data.clock_year;
        if(data.pending_clues){
          const before=st.pending_clues.length;
          st.pending_clues=data.pending_clues;
          if(st.pending_clues.length>before)appendClueToast(st.pending_clues);
        }
        if(data.rollback)appendRollback(data.rollback);
        applyStorm(data.storm);
        updateClockDisplay();
      }
    }catch(e){hideTyping();appendError('请求失败：'+(e.message||e));}
    finally{setBusy(false);}
    return;
  }
  if(text.startsWith('/')){
    appendMsg('player',text,st.arc.name);
    setBusy(true);showTyping();
    try{const data=await api('/api/command',{cmd:text});hideTyping();
      handleCommandOutput(data);}
    catch(e){hideTyping();appendError('命令失败：'+(e.message||e));}
    finally{setBusy(false);}
    return;
  }
  appendMsg('player',text,st.arc.name);
  setBusy(true);showTyping();
  try{
    const data=await api('/api/chat',{text});
    hideTyping();
    if(data.error){appendError('模型返回错误：'+data.error);}
    else{
      appendMsg('narrator',data.reply);
      if(data.threads)st.threads=data.threads;
      if(data.surges)st.surges=data.surges;
      if(data.stage)st.stage=data.stage;
      if(data.clock_year)st.clock_year=data.clock_year;
      if(data.pending_clues){
        const before=st.pending_clues.length;
        st.pending_clues=data.pending_clues;
        if(st.pending_clues.length>before)appendClueToast(st.pending_clues);
      }
      if(data.rollback)appendRollback(data.rollback);
      applyStorm(data.storm);
      updateClockDisplay();
      setTimeout(async()=>{
        try{const s=await api('/api/state');
          if(s.threads)st.threads=s.threads;
          if(s.surges)st.surges=s.surges;
          if(s.clues)st.clues=s.clues;
          if(s.pending_clues)st.pending_clues=s.pending_clues;
          applyStorm(s.storm);
        }catch(e){}
      },6000);
    }
  }catch(e){hideTyping();appendError('请求失败：'+(e.message||e));}
  finally{setBusy(false);}
}

function handleCommandOutput(data){
  if(data.readables){
    st.readables=data.readables;
    if(data.readable && data.readable.read){
      appendSystem('【已读】'+(data.readable.name||''));
    }
  }
  if(data.output==='__CARD__'){if(data.card){st.arc=data.card;renderMenu();openPanel($('#panel-menu'));}return;}
  if(data.output==='__THREADS__'){st.threads=data.threads||[];st.surges=data.surges||[];renderThreads();openPanel($('#panel-thread'));return;}
  if(data.output==='__CLUES__'){
    if(data.clues)st.clues=data.clues;
    if(data.pending)st.pending_clues=data.pending;
    if(data.links)st.clue_links=data.links;
    if(data.quest_log)st.quest_log=data.quest_log;
    renderClues();openPanel($('#panel-clue'));return;
  }
  if(data.output==='__STORM__'){
    if(data.storm)applyStorm(data.storm);
    renderStorm();openPanel($('#panel-storm'));return;
  }
  if(data.clock_year){st.clock_year=data.clock_year;updateClockDisplay();}
  if(data.age!=null)st.age=data.age;
  if(data.birth_year!=null)st.birth_year=data.birth_year;
  if(data.stage)st.stage=data.stage;
  if(data.clue){st.clues.push(data.clue);}
  if(data.clues&&Array.isArray(data.clues))st.clues=data.clues;
  if(data.pending&&Array.isArray(data.pending))st.pending_clues=data.pending;
  if(data.link)st.clue_links.push(data.link);
  if(data.storm)applyStorm(data.storm);
  if(data.rollback)appendRollback(data.rollback);
  if(data.output)appendSystem(data.output);
  updateClockDisplay();
}

function openPanel(p){if(p)p.classList.add('active');}
function closePanel(p){if(p)p.classList.remove('active');}

function updateClockDisplay(){
  const btn=document.getElementById('btn-clock');
  if(btn&&st.clock_year!=null){
    const mm=String(st.clock_month||1).padStart(2,'0');
    btn.textContent=String(st.clock_year).padStart(4,'0')+'-'+mm;
  }
}

function renderMenu(){
  const body=$('#menu-body');
  const a=st.arc;
  if(!a){body.innerHTML='<div class="card-value dim">尚无档案。</div>';return;}
  const rows=[
    ['姓名',a.name],['性别',a.gender],['出生年',st.birth_year],
    ['出生锚定',a.birth_context],
    ['当前年份',st.clock_year],['当前年龄',st.age!=null?(st.age+' 岁'):''],
    ['编号',a.code_name],['隶属',a.affiliation],
    ['出生地',a.birthplace],['','（'+(a.birthplace_note||'')+'）'],
    ['血统',a.bloodline],['家庭',a.family],['性格',a.personality],
    ['外貌',a.appearance],['神秘术',a.ability_name],
    ['咒语',a.ability_incantation],['',a.ability_desc],
    ['时间线',a.vertin_offset],['与维尔汀',a.relation],
  ];
  let html='';
  for(const [label,val] of rows){
    if(!val&&label)continue;
    html+='<div class="card-row">';
    if(label)html+='<div class="card-label">'+esc(label)+'</div>';
    html+='<div class="card-value'+(label?'':' dim')+'">'+esc(val)+'</div></div>';
  }
  if(st.stage){
    html+='<div class="setting-row"><div class="setting-label">时 间 锚 点</div>'
      +'<div class="card-value dim" style="font-size:12px;">'+esc(st.stage)+'</div></div>';
  }
  html+='<div class="setting-row"><div class="setting-label">档案引用强度 DRIFT</div>'
    +'<div style="display:flex;align-items:center;gap:10px;">'
    +'<input type="range" min="0" max="3" step="1" id="drift-range" value="'+st.drift+'" style="flex:1;accent-color:#c9a961;">'
    +'<span style="color:#c9a961;font-size:16px;width:22px;text-align:center;">'+st.drift+'</span>'
    +'</div></div>'
    +'<div class="setting-row"><div class="setting-label">存 档 槽 位</div>'
    +'<div id="saves-block"><div class="card-value dim">读取中……</div></div></div>'
    +'<div class="btn-row">'
    +'<button class="btn" id="m-save">存档</button>'
    +'<button class="btn" id="m-load">读档</button>'
    +'<button class="btn" id="m-new">新建</button>'
    +'</div>';
  body.innerHTML=html;
  renderSaves();
  const range=document.getElementById('drift-range');
  if(range){
    range.addEventListener('change',async()=>{
      const v=parseInt(range.value,10);
      try{await api('/api/drift',{value:v});st.drift=v;}catch(e){}
    });
  }
}

async function renderSaves(){
  const block=document.getElementById('saves-block');
  if(!block)return;
  try{
    const d=await api('/api/saves');
    const slots=d.slots||[];
    if(!slots.length){
      block.innerHTML='<div class="card-value dim">暂无存档。每次新建角色会自动开一个槽位。</div>';
      return;
    }
    let html='';
    for(const s of slots){
      const cur=s.id===d.current;
      html+='<div class="card-row" style="align-items:center;">'
        +'<div class="card-value" style="flex:1;">'
        +(cur?'<span style="color:#c9a961;">▶ </span>':'')
        +esc(s.name||s.char_name||s.id)
        +'<div style="font-size:11px;color:#5a6078;">'
        +esc(s.id)+' · 时钟 '+esc(String(s.clock_year??'?'))
        +' · '+esc(s.saved_at||'')+'</div>'
        +'</div>'
        +'<div style="display:flex;gap:6px;flex-shrink:0;">'
        +(cur?'':'<button class="btn sv-load" data-id="'+esc(s.id)+'">载入</button>')
        +'<button class="btn sv-rename" data-id="'+esc(s.id)+'" data-name="'+esc(s.name||'')+'">改名</button>'
        +'<button class="btn sv-del" data-id="'+esc(s.id)+'">删</button>'
        +'</div></div>';
    }
    block.innerHTML=html;
  }catch(e){
    block.innerHTML='<div class="card-value dim">存档列表读取失败。</div>';
  }
}

function renderThreads(){
  const body=$('#thread-body');
  let html='';
  if(st.threads&&st.threads.length){
    html+='<div class="setting-label" style="margin-bottom:8px;">暗 流</div>';
    for(const t of st.threads){
      html+='<div class="thread-item">';
      html+='<div class="thread-name">· '+esc(t.name||'?')+'</div>';
      if(t.state)html+='<div class="thread-field"><b>状态</b>'+esc(t.state)+'</div>';
      if(t.seeds)html+='<div class="thread-field"><b>走向</b>'+esc(t.seeds)+'</div>';
      if(t.stake)html+='<div class="thread-field"><b>关联</b>'+esc(t.stake)+'</div>';
      html+='</div>';
    }
  }
  if(st.surges&&st.surges.length){
    html+='<div class="setting-label" style="margin:16px 0 8px;">涌 动 · 谜 题</div>';
    for(const s of st.surges){
      html+='<div class="thread-item surge">';
      html+='<div class="thread-name surge">◈ '+esc(s.name||'?')+'</div>';
      if(s.mystery)html+='<div class="thread-field"><b>谜题</b>'+esc(s.mystery)+'</div>';
      if(s.state)html+='<div class="thread-field"><b>状态</b>'+esc(s.state)+'</div>';
      if(s.seeds)html+='<div class="thread-field"><b>走向</b>'+esc(s.seeds)+'</div>';
      if(s.stake)html+='<div class="thread-field"><b>关联</b>'+esc(s.stake)+'</div>';
      html+='</div>';
    }
  }
  if(!html)html='<div class="card-value dim">（暂无）</div>';
  body.innerHTML=html;
}

function renderClues(){
  const body=$('#clue-body');
  let html='';
  html+='<div class="setting-label" style="margin-bottom:8px;">已 采 纳 · 点 击 选 中 两 条 后 验 证</div>';
  if(st.clues.length){
    const groups = new Map();
    for(const c of st.clues){
      const k = ((c.tag||'').trim()) || '__unfiled__';
      if(!groups.has(k)) groups.set(k, []);
      groups.get(k).push(c);
    }
    const namedKeys = Array.from(groups.keys()).filter(k => k !== '__unfiled__');
    const orderedKeys = namedKeys.concat(
      groups.has('__unfiled__') ? ['__unfiled__'] : []);
    for(const key of orderedKeys){
      const arr = groups.get(key);
      const isUnfiled = (key === '__unfiled__');
      const label = isUnfiled ? '未 归 类' : key;
      const color = isUnfiled ? '#5a6078' : '#c9a961';
      html+='<div class="setting-label" style="margin:12px 0 6px;color:'+color+';">'
        + esc(label) + ' <span style="color:#5a6078;">(' + arr.length + ')</span></div>';
      for(const c of arr){
        const sel=st.selectedClues.has(c.id)?' selected':'';
        html+='<div class="clue-item'+sel+'" data-id="'+esc(c.id)+'">';
        html+='<div class="clue-text"><span class="clue-id">'+esc(c.id)+'</span>'+esc(c.text)+'</div>';
        html+='<div class="clue-meta">'+esc(c.year||'')+' · '+esc(c.source||'')+'</div>';
        html+='</div>';
      }
    }
  } else {
    html+='<div class="card-value dim">（还没有采纳任何线索）</div>';
  }
  html+='<div class="btn-row" style="margin-top:12px;">'
    +'<button class="btn primary" id="c-verify">验 证 所 选</button>'
    +'<button class="btn" id="c-clear">清 空</button>'
    +'</div>';
  if(st.pending_clues.length){
    html+='<div class="setting-label" style="margin:20px 0 8px;">待 选 线 索</div>';
    st.pending_clues.forEach((c,i)=>{
      html+='<div class="clue-item">';
      html+='<div class="clue-text"><span class="clue-id">候选 '+(i+1)+'</span>'+esc(c.text)+'</div>';
      html+='<div class="btn-row" style="margin-top:6px;">'
        +'<button class="btn" data-take="'+(i+1)+'">采纳</button>'
        +'<button class="btn" data-drop="'+(i+1)+'">丢弃</button>'
        +'</div></div>';
    });
  }
  if(st.clue_links.length){
    html+='<div class="setting-label" style="margin:20px 0 8px;">验 证 记 录</div>';
    for(const l of st.clue_links){
      html+='<div class="link-item"><span class="link-verdict">'+esc(l.from)+' × '+esc(l.to)+' → '+esc(l.verdict)+'</span><br>'+esc(l.reason||'')+'</div>';
    }
  }
  if(st.quest_log&&st.quest_log.length){
    html+='<div class="setting-label" style="margin:20px 0 8px;">影 响 记 录</div>';
    for(const q of st.quest_log){
      html+='<div class="link-item" style="border-left-color:#8890a8;">'+esc(q.year||'')+' · '+esc(q.text||'')+'</div>';
    }
  }
  body.innerHTML=html;
  body.querySelectorAll('.clue-item[data-id]').forEach(el=>{
    el.addEventListener('click',()=>{
      const id=el.dataset.id;
      if(st.selectedClues.has(id))st.selectedClues.delete(id);
      else{
        if(st.selectedClues.size>=2)st.selectedClues.clear();
        st.selectedClues.add(id);
      }
      renderClues();
    });
  });
  const verify=document.getElementById('c-verify');
  if(verify)verify.addEventListener('click',async()=>{
    const ids=Array.from(st.selectedClues);
    if(ids.length!==2){appendSystem('请选中恰好两条线索。');return;}
    try{
      const data=await api('/api/command',{cmd:'/link '+ids[0]+' '+ids[1]});
      st.selectedClues.clear();
      handleCommandOutput(data);
    }catch(e){appendError('验证失败：'+(e.message||e));}
  });
  const clr=document.getElementById('c-clear');
  if(clr)clr.addEventListener('click',()=>{st.selectedClues.clear();renderClues();});
  body.querySelectorAll('[data-take]').forEach(b=>{
    b.addEventListener('click',async()=>{
      try{const d=await api('/api/command',{cmd:'/take '+b.dataset.take});
        handleCommandOutput(d);renderClues();}catch(e){}
    });
  });
  body.querySelectorAll('[data-drop]').forEach(b=>{
    b.addEventListener('click',async()=>{
      try{const d=await api('/api/command',{cmd:'/drop '+b.dataset.drop});
        handleCommandOutput(d);renderClues();}catch(e){}
    });
  });
}

function renderStorm(){
  const body=$('#storm-body');
  const S = st.storm||{};
  const era = (S.eras||{})[S.current_era] || {level:0, name:S.current_era||'—'};
  const lvl = era.level||0;
  const thresh = S.threshold||100;
  const pct = Math.round(lvl/thresh*100);
  let html='';
  html+='<div class="storm-current">';
  html+='<div class="era-title">当前时代 · '+(esc(era.name||'—'))+'</div>';
  html+='<div class="era-bar">'+('█'.repeat(Math.round(lvl/thresh*14)) + '░'.repeat(14-Math.round(lvl/thresh*14)))+'</div>';
  html+='<div style="display:flex;justify-content:space-between;align-items:baseline;">'
    +'<span style="color:#8890a8;font-size:12px;">病害程度 · '+(S.label||'平静')+'</span>'
    +'<span class="era-num">'+lvl+' / '+thresh+' ('+pct+'%)</span>'
    +'</div>';
  html+='<div style="color:#5a6078;font-size:11px;margin-top:6px;">'
    +'· 20 微澜 &nbsp; · 60 暴雨将至 &nbsp; · 85 崩坏边缘 &nbsp; · 100 触发回退'
    +'</div>';
  html+='</div>';
  html+='<div class="btn-row" style="margin-top:8px;">'
    +'<button class="btn danger" id="s-rollback">手 动 回 退</button></div>';

  // 回退分级说明
  html+='<div class="setting-label" style="margin:20px 0 8px;">回 退 分 级（超额越大 · 回退越深）</div>';
  const tiers = [
    ["微溃","+0-4","2-8 年"],["失序","+5-14","5-18 年"],
    ["崩解","+15-29","12-35 年"],["溃灭","+30-49","25-70 年"],
    ["归墟","+50+","50-150 年"],
  ];
  for(let i=0;i<tiers.length;i++){
    const t=tiers[i];
    html+=`<div class="storm-era" style="padding:8px 12px;">`
      +`<div class="storm-era-name"><span class="tier-badge tier-${i+1}">${t[0]}</span>`
      +`<span class="tag">${t[1]}</span></div>`
      +`<div class="storm-era-label">基础回退：${t[2]}</div>`
      +`</div>`;
  }

  const eras = S.eras||{};
  const keys = Object.keys(eras);
  if(keys.length){
    html+='<div class="setting-label" style="margin:20px 0 8px;">时 代 目 录</div>';
    keys.sort();
    for(const k of keys){
      const e = eras[k];
      const l = e.level||0;
      const p = l/100;
      const cls = p>=0.85?'critical':(p>=0.6?'warning':(p>=0.2?'elevated':'calm'));
      const isCur = (k===S.current_era)?' current':'';
      const isSeal = e.sealed?' sealed':'';
      html+='<div class="storm-era'+isCur+isSeal+'">';
      html+='<div class="storm-era-name">'
        +'<span>'+esc(e.name||k)+'</span>'
        +'<span class="tag">'+(e.sealed?'已封存':'')+'</span></div>';
      html+='<div class="storm-era-bar '+cls+'">'
        +('█'.repeat(Math.round(p*14)) + '░'.repeat(14-Math.round(p*14)))
        +'</div>';
      html+='<div class="storm-era-label">'+l+' / 100</div>';
      html+='</div>';
    }
  }

  if(S.rollbacks && S.rollbacks.length){
    html+='<div class="setting-label" style="margin:20px 0 8px;">回 退 记 录</div>';
    for(let i=S.rollbacks.length-1;i>=0;i--){
      const r=S.rollbacks[i];
      const idx={"微溃":1,"失序":2,"崩解":3,"溃灭":4,"归墟":5}[r.tier_name]||1;
      html+='<div class="link-item" style="border-left-color:#d05050;">'
        +'<span class="tier-badge tier-'+idx+'">'+(r.tier_name||'回退')+'</span> '
        +esc(r.from_year)+' → '+esc(r.to_year)
        +'（倒退 '+esc(r.years_back)+' 年';
      if(r.overshoot!=null)html+=' · 超额+'+esc(r.overshoot);
      html+='）<br><span style="color:#8890a8;font-size:11px;">'
        +esc(r.from_era)+' → '+esc(r.to_era);
      if(r.official && r.official.title){
        html+=' · 吸附「'+esc(r.official.title)+'」';
      }
      html+='</span><div style="color:#5a6078;font-size:10px;margin-top:2px;">'+esc(r.ts||'')+'</div></div>';
    }
  }

  if(S.log && S.log.length){
    html+='<div class="setting-label" style="margin:20px 0 8px;">暴 雨 事 件 日 志</div>';
    const recent = S.log.slice(-20).reverse();
    for(const l of recent){
      const d = l.delta>0?('+'+l.delta):String(l.delta);
      html+='<div class="link-item" style="border-left-color:#7a6838;">'
        +'<span style="color:#c9a961;font-family:ui-monospace,monospace;">'+esc(d)+'</span> '
        +esc(l.reason||'')+' <span style="color:#5a6078;">('+esc(l.era_name||l.era||'')+' '+esc(l.from)+'→'+esc(l.to)+')</span>'
        +'<div style="color:#5a6078;font-size:10px;margin-top:2px;">'+esc(l.ts||'')+'</div></div>';
    }
  }
  body.innerHTML=html;
  const rb=document.getElementById('s-rollback');
  if(rb)rb.addEventListener('click',async()=>{
    if(!confirm('确认手动触发时代回退？（时钟将回拨）'))return;
    try{
      const d=await api('/api/command',{cmd:'/rollback'});
      handleCommandOutput(d);
      if(d.rollback)appendRollback(d.rollback);
      renderStorm();
    }catch(e){appendError('回退失败：'+(e.message||e));}
  });
}

function renderWelcome(){
  const w=document.createElement('div');w.className='welcome';
  w.innerHTML='<div class="welcome-title">雨 幕 档 案</div>'
    +'<div class="welcome-sub">R E V E R S E : 1 9 9 9</div>'
    +'<div class="welcome-line"></div>'
    +'<div class="welcome-note">'
    +'⚠ 暴雨预警 · 非稳定时间线<br>'
    +'⚠ 对话 / 事件 / 人物 可能被抹除<br>'
    +'▶ 推演模式：从 0 岁开始 · 时钟可拨<br><br>'
    +'输入任意文字作为你的行动或台词。<br>'
    +'点击顶栏时钟可拨动年份（年龄随之变化）。<br>'
    +'每次拨动 / 对话都会累积「暴雨·时代病」。<br>'
    +'病害满值即触发回退——回退幅度取决于超额的严重程度，<br>'
    +'并会吸附到官方剧情时段。'
    +'</div>'
    +'<div class="format-legend">'
    +'<div class="legend-title">叙 事 格 式</div>'
    +'<div class="legend-item"><code>//</code>旁白 · 环境 · 镜头</div>'
    +'<div class="legend-item"><code>(&nbsp;)</code>动作 · 神态 · 细节</div>'
    +'<div class="legend-item"><code>说话人：内容</code>对话</div>'
    +'</div>';
  chat.appendChild(w);
}

function showLogin(){
  document.getElementById('modal-login').classList.add('active');
}

function setUserTitle(name){
  const tu=document.getElementById('title-user');
  if(tu&&name)tu.textContent='— '+name+' —';
}

async function refreshState(){
  if(!st.username){showLogin();return;}
  setUserTitle(st.username);
  try{
    const data=await api('/api/state');
    st.arc=data.arc;st.drift=data.drift;
    st.threads=data.threads||[];st.surges=data.surges||[];
    st.stage=data.stage||'';
    st.clock_year=data.clock_year;
    st.clock_month=data.clock_month||1;
    st.clock_day=data.clock_day||1;
    st.birth_year=data.birth_year;
    st.age=data.age;
    st.clues=data.clues||[];
    st.pending_clues=data.pending_clues||[];
    st.clue_links=data.clue_links||[];
    st.quest_log=data.quest_log||[];
    st.readables=data.readables||[];
    applyStorm(data.storm);
    updateClockDisplay();
    chat.innerHTML='';
    if(!data.has_game){renderWelcome();
      document.getElementById('modal-new').classList.add('active');return;}
    for(const msg of data.history){
      if(msg.role==='player')appendMsg('player',msg.text,data.arc?data.arc.name:'你');
      else appendMsg('narrator',msg.text);
    }
    if(data.need_open&&data.history.length===0){
      const ph=appendMsg('narrator','（正在拉开帷幕……）');
      ph.id='opening-placeholder';
      try{
        const o=await api('/api/open',{},600000);
        const p=document.getElementById('opening-placeholder');
        if(p)p.remove();
        if(o.error)appendError('开场失败：'+o.error);
        else{
          if(o.opening)appendMsg('narrator',o.opening);
          if(o.threads)st.threads=o.threads;
          if(o.surges)st.surges=o.surges;
          if(o.stage)st.stage=o.stage;
          if(o.clock_year)st.clock_year=o.clock_year;
          if(o.pending_clues)st.pending_clues=o.pending_clues;
          applyStorm(o.storm);
          updateClockDisplay();
          appendClueToast(st.pending_clues);
        }
      }catch(e){const p=document.getElementById('opening-placeholder');
        if(p)p.remove();appendError('开场失败：'+(e.message||e));}
    }
  }catch(e){appendError('无法连接服务器：'+(e.message||e));}
}

function openClockModal(){
  const modal=document.getElementById('modal-clock');
  document.getElementById('clock-range').value=st.clock_year||1999;
  document.getElementById('clock-month').value=st.clock_month||1;
  document.getElementById('clock-day').value=st.clock_day||1;
  updateClockModal();
  modal.classList.add('active');
}
function updateClockModal(){
  const y=parseInt(document.getElementById('clock-range').value,10);
  const m=parseInt(document.getElementById('clock-month').value,10);
  const d=parseInt(document.getElementById('clock-day').value,10);
  document.getElementById('clock-display').textContent=
    String(y).padStart(4,'0')+'-'+String(m).padStart(2,'0')+'-'+String(d).padStart(2,'0');
  const age=st.birth_year!=null?Math.max(0,y-st.birth_year):null;
  document.getElementById('clock-age').textContent='年龄：'+(age!=null?age+' 岁':'—');
  document.getElementById('chip-birth').textContent='出生年：'+(st.birth_year??'—');
  document.getElementById('chip-now').textContent='当前：'+(st.clock_year??'—');
  const chipStorm=document.getElementById('chip-storm');
  if(chipStorm)chipStorm.textContent='暴雨：'+(st.storm.label||'—');
}

sendBtn.addEventListener('click',send);
input.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
});
input.addEventListener('input',()=>{
  input.style.height='auto';
  input.style.height=Math.min(input.scrollHeight,120)+'px';
});
document.querySelectorAll('.commands button').forEach(b=>{
  b.addEventListener('click',()=>{
    const c=b.dataset.cmd;
    const a=b.dataset.action;
    if(a==='nextday'){input.value='进入下一天';send();return;}
    if(a==='nextmonth'){input.value='进入下一月';send();return;}
    if(a==='nextyear'){input.value='进入下一年';send();return;}
    if(c==='/card'){if(st.arc){renderMenu();openPanel($('#panel-menu'));}else{input.value='/card';}return;}
    if(c==='/thread'){renderThreads();openPanel($('#panel-thread'));return;}
    if(c==='/clues'){renderClues();openPanel($('#panel-clue'));return;}
    if(c==='/storm'){renderStorm();openPanel($('#panel-storm'));return;}
    if(c==='/clock'){openClockModal();return;}
    input.value=c;send();
  });
});
document.getElementById('btn-menu').addEventListener('click',()=>{
  renderMenu();openPanel($('#panel-menu'));
});
document.getElementById('btn-thread').addEventListener('click',()=>{
  renderThreads();openPanel($('#panel-thread'));
});
document.getElementById('btn-clue').addEventListener('click',()=>{
  renderClues();openPanel($('#panel-clue'));
});
document.getElementById('btn-storm').addEventListener('click',()=>{
  renderStorm();openPanel($('#panel-storm'));
});
document.getElementById('btn-clock').addEventListener('click',openClockModal);

const clockRange=document.getElementById('clock-range');
clockRange.addEventListener('input',updateClockModal);
const clockMonth=document.getElementById('clock-month');
if(clockMonth)clockMonth.addEventListener('input',updateClockModal);
const clockDay=document.getElementById('clock-day');
if(clockDay)clockDay.addEventListener('input',updateClockModal);
document.getElementById('btn-clock-go').addEventListener('click',async()=>{
  const y=parseInt(clockRange.value,10);
  const m=parseInt(document.getElementById('clock-month').value,10);
  const dd=parseInt(document.getElementById('clock-day').value,10);
  try{
    const d=await api('/api/clock',{year:y,month:m,day:dd});
    if(d.ok){
      st.clock_year=d.clock_year;st.age=d.age;st.stage=d.stage;
      applyStorm(d.storm);
      updateClockDisplay();
      document.getElementById('modal-clock').classList.remove('active');
      appendSystem('时钟拨至 '+y+' 年。玩家年龄 '+d.age+'。');
      if(d.rollback)appendRollback(d.rollback);
      await api('/api/chat',{text:'（时间已改变。请让世界与人物随新年份自然演进，若锚点已失效请重建。）'});
    }
  }catch(e){appendError('时钟失败：'+(e.message||e));}
});

document.querySelectorAll('.panel-overlay').forEach(ov=>{
  ov.addEventListener('click',e=>{if(e.target===ov)closePanel(ov);});
});
document.querySelectorAll('[data-close]').forEach(b=>{
  b.addEventListener('click',()=>{
    const p=b.closest('.panel-overlay');if(p)closePanel(p);
  });
});
document.querySelectorAll('.modal').forEach(m=>{
  m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('active');});
});

document.addEventListener('click',async e=>{
  const t=e.target;if(!t)return;
  if(t.classList&&t.classList.contains('sv-load')){
    e.preventDefault();
    const id=t.getAttribute('data-id');
    try{const d=await api('/api/saves/switch',{id});
      if(d.error){appendError(d.error);return;}
      await refreshState();renderMenu();
      appendSystem(d.output||('已载入存档 '+id));
    }catch(err){appendError('载入失败：'+(err.message||err));}
    return;}
  if(t.classList&&t.classList.contains('sv-rename')){
    e.preventDefault();
    const id=t.getAttribute('data-id');
    const name=prompt('新的存档名：',t.getAttribute('data-name')||'');
    if(!name)return;
    try{const d=await api('/api/saves/rename',{id,name});
      if(d.error){appendError(d.error);return;}
      renderMenu();
    }catch(err){appendError('改名失败：'+(err.message||err));}
    return;}
  if(t.classList&&t.classList.contains('sv-del')){
    e.preventDefault();
    const id=t.getAttribute('data-id');
    if(!confirm('删除存档 '+id+'？此操作不可恢复。'))return;
    try{const d=await api('/api/saves/delete',{id});
      if(d.error){appendError(d.error);return;}
      renderMenu();if(d.output)appendSystem(d.output);
    }catch(err){appendError('删除失败：'+(err.message||err));}
    return;}
  if(!t.id)return;
  switch(t.id){
    case 'btn-login':e.preventDefault();doLogin();break;
    case 'btn-create':e.preventDefault();createCharacter();break;
    case 'btn-custom-toggle':{
      e.preventDefault();
      const cf=document.getElementById('custom-fields');
      if(cf)cf.style.display=(cf.style.display==='none')?'block':'none';
      break;}
    case 'm-save':{
      e.preventDefault();
      const path=prompt('存档文件名：','sim_save.json');if(!path)return;
      try{await api('/api/save',{path});appendSystem('已存档 → '+path);closePanel($('#panel-menu'));}
      catch(err){appendError('存档失败：'+(err.message||err));}
      break;}
    case 'm-load':{
      e.preventDefault();
      const path=prompt('读档文件名：','sim_save.json');if(!path)return;
      try{const d=await api('/api/load',{path});
        if(!d.ok){appendError('读档失败');return;}
        await refreshState();appendSystem('已读档 ← '+path);closePanel($('#panel-menu'));}
      catch(err){appendError('读档失败：'+(err.message||err));}
      break;}
    case 'm-new':
      e.preventDefault();closePanel($('#panel-menu'));
      document.getElementById('modal-new').classList.add('active');
      break;
  }
});

console.log('[rain1999] v3.1 starting (dynamic rollback + official snap)');
refreshState();
})();
</script>
</body>
</html>
"""


# ===========================================================================
# HTTP 服务
# ===========================================================================

STATE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log("http", fmt % args, level="debug")

    def _send_json(self, obj, status=200):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception as e:
            data = json.dumps({"error": str(e)}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _send_html(self, html, status=200):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _read_user_header(self):
        try:
            u = self.headers.get("X-User", "") or ""
        except Exception:
            u = ""
        return u.strip() or "default"

    def do_GET(self):
        log("http", f"GET  {self.path}", level="debug")
        try:
            STATE.ensure_user(self._read_user_header())
        except Exception:
            pass
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
            return
        if p == "/api/me":
            self._send_json({
                "user": STATE.current_user,
                "has_game": STATE.arc is not None,
            })
            return

        if p == "/api/state":
            self._send_json(STATE.snapshot())
            return
        if p == "/api/saves":
            self._send_json(STATE.saves_list())
            return
        if p == "/api/storm":
            self._send_json({"storm": STATE.storm.to_dict(),
                             "current_era": STATE.storm.current_era})
            return
        if p == "/api/storyline":
            if STATE.storyline and STATE.storyline.has_graph():
                self._send_json({
                    "has_graph": True,
                    "stats": STATE.storyline.stats(),
                    "nodes": STATE.storyline.storyline_nodes(limit=60),
                })
            else:
                self._send_json({"has_graph": False, "stats": {}, "nodes": []})
            return
        if p == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self):
        log("http", f"POST {self.path}", level="debug")
        try:
            STATE.ensure_user(self._read_user_header())
        except Exception:
            pass
        p = urlparse(self.path).path
        body = self._read_json()
        try:
            if p == "/api/login":
                name = (body.get("user") or "").strip()
                if not name:
                    self._send_json({"error": "用户名不能为空"}, 400)
                    return
                log("http", f"登录 → {name}", level="info")
                STATE.set_user(name)
                snap = STATE.snapshot()
                self._send_json({
                    "ok": True,
                    "user": name,
                    "has_game": snap.get("has_game", False),
                    "snapshot": snap,
                })
                return

            if p == "/api/new":
                name = (body.get("name") or "").strip() or "无名"
                gender = (body.get("gender") or "").strip() or "不明"
                ab = (body.get("ability_name") or "").strip() or "未命名术式"
                desc = (body.get("ability_desc") or "").strip() or "效果不明。"
                try:
                    by = int(body.get("birth_year") or 1960)
                except (TypeError, ValueError):
                    by = 1960
                try:
                    sy = int(body.get("start_year") or by)
                except (TypeError, ValueError):
                    sy = by
                if sy < by:
                    sy = by
                log("http", "建档请求", level="info",
                    name=name, birth=by)
                birth_side = (body.get("birth_side") or "").strip()
                birth_context = (body.get("birth_context") or "").strip()
                custom = body.get("custom")
                if not isinstance(custom, dict):
                    custom = None
                result = STATE.new_game(name, gender, ab, desc, by, sy,
                                        birth_side=birth_side,
                                        birth_context=birth_context,
                                        custom=custom)
                self._send_json(result)
                return

            if p == "/api/birthyear/check":
                # AI 档案员核对出生年：1996 等暴雨落点返回 ambiguous+options
                log("http", f"出生年核对 → {body.get('year')}", level="info")
                self._send_json(STATE.birth_year_review(body.get("year")))
                return

            if p == "/api/birthyear/confirm":
                # 选定暴雨前/后（或普通年份）后生成剧情锚定
                log("http", f"出生年定档 → {body.get('year')} "
                            f"{body.get('side') or '-'}", level="info")
                self._send_json(STATE.birth_year_confirm(
                    body.get("year"), body.get("side")))
                return

            if p == "/api/open":
                log("http", "开场请求", level="info")
                self._send_json(STATE.open_story())
                return

            if p == "/api/saves/switch":
                log("http", f"切换存档 → {body.get('id')}", level="info")
                self._send_json(STATE.saves_switch(body.get("id")))
                return

            if p == "/api/saves/delete":
                log("http", f"删除存档 → {body.get('id')}", level="warn")
                self._send_json(STATE.saves_delete(body.get("id")))
                return

            if p == "/api/saves/rename":
                self._send_json(STATE.saves_rename(
                    body.get("id"), body.get("name")))
                return

            if p == "/api/chat":
                text = (body.get("text") or "").strip()
                if not text:
                    self._send_json({"error": "空输入"}, 400)
                    return
                log("http", f"对话请求 {len(text)} 字", level="info")
                self._send_json(STATE.chat(text))
                return

            if p == "/api/command":
                cmd = (body.get("cmd") or "").strip()
                log("http", f"命令 {cmd[:40]}", level="debug")
                self._send_json(STATE.command(cmd))
                return

            if p == "/api/clock":
                try:
                    y = int(body.get("year"))
                except (TypeError, ValueError):
                    self._send_json({"error": "年份无效"}, 400)
                    return
                m = body.get("month")
                d = body.get("day")
                log("http", f"拨动时钟 → {y}-{m or '-'}-{d or '-'}",
                    level="info")
                self._send_json(STATE._set_clock(y, month=m, day=d))
                return

            if p == "/api/drift":
                try:
                    v = int(body.get("value", 2))
                except (TypeError, ValueError):
                    v = 2
                v = max(0, min(3, v))
                STATE.drift = v
                STATE._autosave()
                log("http", f"档案引用强度 → {v}", level="debug")
                self._send_json({"drift": v})
                return

            if p == "/api/save":
                path = (body.get("path") or "sim_save.json").strip()
                if not STATE.arc:
                    self._send_json({"error": "尚未建档"}, 400)
                    return
                log("http", f"存档 → {path}", level="info")
                STATE.save(path)
                self._send_json({"ok": True, "path": path})
                return

            if p == "/api/load":
                path = (body.get("path") or "sim_save.json").strip()
                log("http", f"读档 ← {path}", level="info")
                ok = STATE.load(path)
                self._send_json({"ok": ok})
                return

            if p == "/api/nextday":
                log("http", "进入下一天", level="info")
                self._send_json(STATE.next_day())
                return

            if p == "/api/nextmonth":
                log("http", "进入下一月", level="info")
                self._send_json(STATE.next_month())
                return

            if p == "/api/nextyear":
                log("http", "进入下一年", level="info")
                self._send_json(STATE.next_year())
                return

            if p == "/api/rollback":
                log("http", "手动触发回退", level="warn")
                self._send_json(STATE.command("/rollback"))
                return

            if p == "/api/storm/add":
                try:
                    delta = int(body.get("delta", 0))
                except (TypeError, ValueError):
                    delta = 0
                reason = (body.get("reason") or "外部事件").strip()
                log("http", f"病害 +{delta}（{reason}）", level="warn")
                STATE._storm_add(delta, reason)
                self._send_json({"ok": True, "storm": STATE.storm.to_dict()})
                return

        except Exception as e:
            import traceback
            traceback.print_exc()
            log("error", f"HTTP 错误：{e}", level="error")
            self._send_json({"error": str(e)}, 500)
            return

        self.send_error(404)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ===========================================================================
# 入口
# ===========================================================================

DEFAULT_DB_PATHS = [
    os.path.join(_HERE, "rain1999", "data", "rain1999_story.db"),
    os.path.join(_HERE, "data", "rain1999_story.db"),
    os.path.join(_HERE, "rain1999_story.db"),
    os.path.join(".", "rain1999", "data", "rain1999_story.db"),
    os.path.join(".", "data", "rain1999_story.db"),
]


def locate_db():
    for p in DEFAULT_DB_PATHS:
        if os.path.exists(p):
            return p
    print("未自动找到 rain1999_story.db。")
    while True:
        p = input("请输入数据库路径（Ctrl-C 退出）：").strip()
        if not p:
            continue
        if os.path.exists(p):
            return p
        print(f"路径不存在：{p}")


def main():
    global STATE
    global TIMELINE
    global BIRTH_TIMELINE

    log_banner()

    if BIRTH_TIMELINE is None:
        BIRTH_TIMELINE = BirthTimeline.locate()
    if BIRTH_TIMELINE:
        log("boot", "暴雨时间轴已接入（出生年 AI 核对）", level="info",
            path=BIRTH_TIMELINE.path,
            storms=len(BIRTH_TIMELINE.storms),
            periods=len(BIRTH_TIMELINE.periods))
    else:
        log("boot", "未找到 index.json 暴雨时间轴，出生年核对将退化为通用措辞",
            level="warn")

    db_path = locate_db()
    log("boot", "打开数据库", level="info", path=db_path)

    try:
        lore = LoreDB(db_path)
    except Exception as e:
        log("error", f"数据库打开失败：{e}", level="error")
        sys.exit(1)

    stats = lore.stats()
    log("boot", "档案库", level="info",
        episodes=stats["episodes"], lines=stats["lines"],
        chapters=stats["chapters"], categories=stats["categories"])

    storyline = None
    try:
        storyline = StorylineDB(db_path)
        if storyline.has_graph():
            sstats = storyline.stats()
            log("story", "故事线图谱已加载", level="info",
                tables=len(sstats), total=sum(sstats.values()))
            for tname, n in sstats.items():
                log("story", f"  · {tname}: {n} 行", level="debug")
        else:
            log("story", "未检测到 uttu_* 表（先跑 rain1999.py uttu）",
                level="warn")
            storyline = None
    except Exception as e:
        log("story", f"加载失败：{e}", level="warn")
        storyline = None

    log("boot", f"模型 {MODEL}", level="info", base=API_BASE)

    timeline_dir = os.path.join(_HERE, "rain1999", "data", "timeline")
    if _HAS_TIMELINE and os.path.exists(os.path.join(timeline_dir, "index.json")):
        try:
            TIMELINE = TimelineDB(timeline_dir)
            log("boot", "时间线已加载", level="info",
                phases=len(TIMELINE.phases()), storms=len(TIMELINE.storms()))
            log("storm", "官方时段吸附已就绪", level="info",
                tolerance=SNAP_TOLERANCE)
        except Exception as e:
            log("boot", f"时间线加载失败：{e}", level="warn")
            TIMELINE = None
    else:
        log("boot", "时间线未生成（先跑 timeline_organizer.py）", level="warn")
        log("storm", "无时间线 · 回退将不会吸附官方时段", level="warn")

    STATE = GameState(lore, storyline=storyline)

    if STATE.arc:
        log("boot", "检测到存档", level="info",
            name=STATE.arc.name, year=STATE.clock_year,
            age=STATE.player_age, history=len(STATE.history))
        eras = STATE.storm.eras
        if eras:
            for code, e in eras.items():
                bar, lvl = STATE.storm.progress_bar(code)
                log("storm", f"  · {e['name']:<24} {bar} {lvl}",
                    level="debug")

    url = f"http://{HOST}:{PORT}/"
    log_rule("服务已就绪")
    log("boot", f"地址 {url}", level="info")
    log("boot", "手机访问：把 127.0.0.1 换成电脑/手机的局域网 IP", level="info")
    log("boot", "Ctrl-C 退出", level="info")
    log_rule()

    if HOST in ("127.0.0.1", "localhost", "0.0.0.0"):
        try:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("boot", "收到 Ctrl-C，正在关闭 ……", level="warn")
    finally:
        server.server_close()
        log("boot", "服务已停止", level="info")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("error", f"致命错误：{e}", level="error")
        traceback.print_exc()
        sys.exit(1)