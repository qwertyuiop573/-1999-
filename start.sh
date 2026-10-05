#!/usr/bin/env bash
# ============================================================
#  雨幕档案 · 一键启动
#  用法：
#     ./start.sh            有库直接进模拟器；没库先抓再进
#     ./start.sh --force    强制重新抓取（剧情库 + 图谱），再进模拟器
# ============================================================
set -e

# 切到脚本所在目录，保证相对路径稳定
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 找 python 解释器
PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"
command -v "$PY" >/dev/null 2>&1 || {
    echo "[start.sh] 未找到 python3 或 python，请先安装。" >&2
    exit 1
}

# 检查两个必需的 py 文件
for f in rain1999.py rain1999_sim.py; do
    if [ ! -f "$f" ]; then
        echo "[start.sh] 缺少文件：$f（应与本脚本放在同一目录）" >&2
        exit 1
    fi
done

# --- 下载产物路径（与 rain1999.py 的 crawl 默认值一致） ---------------
DATA_DIR="./rain1999/data"
DB="$DATA_DIR/rain1999_story.db"
CACHE_DIR="$DATA_DIR/_cache"
TXT_DIR="$DATA_DIR/story_txt"

# 是否强制重爬
FORCE=0
for a in "$@"; do
    case "$a" in
        --force|-f) FORCE=1 ;;
    esac
done

# --- 检测数据库是否“有效可用” ----------------------------------------
# 文件存在 + SQLite 能打开 + episodes 表非空
db_ok() {
    [ -f "$DB" ] || return 1
    "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    finally:
        conn.close()
    sys.exit(0 if n > 0 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

# --- 检测时间线数据（timeline 表非空） --------------------------------
timeline_ok() {
    [ -f "$DB" ] || return 1
    "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]
    finally:
        conn.close()
    sys.exit(0 if n > 0 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

# --- 检测故事线图谱（任意 uttu_* 表非空） ------------------------------
# 表名由 rain1999_uttu_bridge.py 决定，所以这里动态扫描 sqlite_master
storyline_ok() {
    [ -f "$DB" ] || return 1
    "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'uttu\\_%' ESCAPE '\\'"
        ).fetchall()
        for (name,) in rows:
            n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if n > 0:
                sys.exit(0)
    finally:
        conn.close()
    sys.exit(1)
except Exception:
    sys.exit(1)
PYEOF
}

# --- 检测缓存/TXT 是否齐全 -------------------------------------------
aux_ok() {
    [ -d "$CACHE_DIR" ] || return 1
    [ -d "$TXT_DIR" ]   || return 1
    find "$TXT_DIR" -type f -name '*.txt' -print -quit 2>/dev/null | grep -q .
}

# --- 综合判定 --------------------------------------------------------
NEED_CRAWL=0
NEED_UTTU=0
REASON=""

if [ "$FORCE" -eq 1 ]; then
    NEED_CRAWL=1
    NEED_UTTU=1
    REASON="--force 指定强制重抓"
elif ! db_ok; then
    NEED_CRAWL=1
    NEED_UTTU=1
    if [ -f "$DB" ]; then
        REASON="数据库无效或为空：$DB"
    else
        REASON="未找到数据库：$DB"
    fi
elif ! aux_ok; then
    NEED_CRAWL=1
    NEED_UTTU=1
    REASON="缓存/TXT 目录缺失或不完整（$CACHE_DIR / $TXT_DIR）"
else
    # 库没问题，再看两类图谱
    if ! timeline_ok; then
        NEED_UTTU=1
        REASON="timeline 表为空，将重建"
    fi
    if ! storyline_ok; then
        NEED_UTTU=1
        if [ -n "$REASON" ]; then
            REASON="$REASON；未检测到故事线/时间线图谱（uttu_* 表）"
        else
            REASON="未检测到故事线/时间线图谱（uttu_* 表）"
        fi
    fi
fi

# --- 抓取剧情库 ------------------------------------------------------
if [ "$NEED_CRAWL" -eq 1 ]; then
    echo "[start.sh] 需要抓取：$REASON"
    echo "[start.sh] 抓取剧情数据库 ……"
    "$PY" rain1999.py crawl \
        --out  "$DATA_DIR" \
        --db   "rain1999_story.db" \
        --slim-cache

    if ! db_ok; then
        echo "[start.sh] 抓取完成后数据库仍然无效：$DB" >&2
        exit 1
    fi
    # 抓完顺带补时间线（万一 build_timeline 没跑）
    if ! timeline_ok; then
        echo "[start.sh] 重建时间线 ……"
        "$PY" rain1999.py retimeline --db "$DB"
    fi
else
    echo "[start.sh] 检测到已有可用剧情库：$DB"
fi

# --- 并入故事线 / 时间线图谱 ----------------------------------------
# 桥接模块存在才尝试；缺失时只警告，不阻断启动
if ! storyline_ok; then
    if [ -f "rain1999_uttu_bridge.py" ]; then
        echo "[start.sh] 未检测到故事线图谱，尝试并入 uttu ……"
        if "$PY" rain1999.py uttu --db "$DB"; then
            if storyline_ok; then
                echo "[start.sh] 故事线图谱并入完成。"
            else
                echo "[start.sh] [warn] uttu 命令返回成功但仍未检测到图谱。" >&2
            fi
        else
            echo "[start.sh] [warn] uttu 并入失败，跳过（不影响模拟器启动）。" >&2
        fi
    else
        echo "[start.sh] [warn] 未找到 rain1999_uttu_bridge.py，跳过故事线图谱。" >&2
    fi
else
    echo "[start.sh] 已检测到故事线图谱（uttu_* 表）。"
fi

echo "[start.sh] 启动模拟器 ……"
exec "$PY" rain1999_sim.py