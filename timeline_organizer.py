#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timeline_organizer.py — 《重返未来：1999》暴雨时间线加载模块
================================================================

配套数据文件（放在同一时间线目录下）：

    index.json            结构化暴雨时间轴（12 个平稳期 + 11 场暴雨）
    vertin_timeline.json  维尔汀个人时间表（时间 -> 章节 -> 人物，可选）

rain1999_sim.py 的对接方式（模拟器本体无需任何改动）：

    from timeline_organizer import TimelineDB
    TIMELINE = TimelineDB(timeline_dir)              # 目录里要有 index.json
    TIMELINE.phases()                                # 平稳期列表
    TIMELINE.storms()                                # 暴雨列表
    TIMELINE.get_phase_for_ext_year(1929)            # -> {"code","title","start_year","end_year",...} | None
    TIMELINE.phase_context(code, max_events=5, max_chronology=6)  # -> 注入旁白的文本块

维尔汀时间表的引用：phase_context() 生成的文本块里会自动附带
「维尔汀动态」一节（引自 vertin_timeline.json，按时期代码关联），
随模拟器每次对话注入旁白；也可用 vertin_at(year) 单独查询。

命令行用法：

    python timeline_organizer.py                  # 自动定位时间线目录并做完整检查
    python timeline_organizer.py check [--dir D]  # 校验数据文件结构
    python timeline_organizer.py show [--dir D] [--phase CODE]
    python timeline_organizer.py vertin [--dir D] [--year Y]
    python timeline_organizer.py context CODE [--dir D]   # 预览模拟器实际注入的文本块

只使用 Python 标准库，Termux 下可直接运行。
"""

import argparse
import json
import os
import sys

SCHEMA_VERSION = "2.0"
VERTIN_FILE = "vertin_timeline.json"
INDEX_FILE = "index.json"


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

class TimelineDB:
    """加载并查询暴雨时间轴（index.json）与维尔汀时间表（vertin_timeline.json）。"""

    def __init__(self, timeline_dir):
        self.dir = os.path.abspath(timeline_dir)
        index_path = os.path.join(self.dir, INDEX_FILE)
        if not os.path.exists(index_path):
            raise FileNotFoundError("未找到 " + index_path)
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                self._index = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                "index.json 不是合法 JSON（第 {} 行）：{}。"
                "如果它是旧的 Wiki 纯文本版，请用新版结构化 index.json 替换。".format(
                    getattr(e, "lineno", "?"), e)
            )
        self._phases = list(self._index.get("时期") or [])
        self._storms = list(self._index.get("暴雨") or [])
        if not self._phases or not self._storms:
            raise ValueError("index.json 缺少「时期」或「暴雨」列表，结构不符合规范 2.0")
        self._phase_by_code = {p["代码"]: p for p in self._phases}
        self._storm_by_code = {s["代码"]: s for s in self._storms}

        # 维尔汀时间表（可选；缺失时不影响 index.json 的使用）
        self._vertin_rows = []
        self._vertin_by_phase = {}
        self._vertin_meta = None
        vpath = os.path.join(self.dir, VERTIN_FILE)
        if os.path.exists(vpath):
            try:
                with open(vpath, "r", encoding="utf-8") as f:
                    vdata = json.load(f)
                self._vertin_meta = vdata.get("元信息") or {}
                self._vertin_rows = list(vdata.get("年表") or [])
                for row in self._vertin_rows:
                    self._vertin_by_phase.setdefault(row.get("时期代码"), []).append(row)
            except (json.JSONDecodeError, OSError) as e:
                print("[timeline] 警告：{} 读取失败（{}），已跳过维尔汀时间表".format(VERTIN_FILE, e),
                      file=sys.stderr)

    # ---- 列表 -------------------------------------------------------------

    def phases(self):
        """全部平稳期（按叙事先后）。"""
        return list(self._phases)

    def storms(self):
        """全部暴雨（按发生先后）。"""
        return list(self._storms)

    def phase(self, code):
        return self._phase_by_code.get(code)

    def storm(self, code):
        return self._storm_by_code.get(code)

    # ---- 年份 -> 时期 -------------------------------------------------------

    def get_phase_for_ext_year(self, year):
        """
        按外界年代年份查找所处平稳期。
        返回 {"code","title","start_year","end_year",...}；未命中返回 None。
        重叠年份（如 1913、1935）取叙事上更晚的时期。
        """
        try:
            y = int(year)
        except (TypeError, ValueError):
            return None
        for p in reversed(self._phases):  # 叙事上更晚的优先
            span = p.get("外界年代") or {}
            lo, hi = span.get("起始"), span.get("结束")
            if lo is None or hi is None:
                continue
            if int(lo) <= y <= int(hi):
                return {
                    "code": p.get("代码"),
                    "title": p.get("标题"),
                    "start_year": int(lo),
                    "end_year": int(hi),
                    "kind": p.get("类型"),
                    "narrative_order": p.get("序号"),
                    "storm_era": p.get("暴雨纪年"),
                    "apeiron_era": p.get("阿派朗纪年"),
                    "vertin_age": p.get("维尔汀年龄"),
                    "span_display": "{} - {}".format(span.get("起始显示", lo),
                                                     span.get("结束显示", hi)),
                }
        return None

    # ---- 旁白上下文 ---------------------------------------------------------

    def phase_context(self, code, max_events=5, max_chronology=6):
        """
        生成注入模拟器旁白的「官方时间线参考」文本块。
        内含：阶段概况、对应剧情、关联角色、大事记节选、
        维尔汀动态（引自 vertin_timeline.json）、前后暴雨。
        """
        if code in self._storm_by_code:
            return self._storm_context(self._storm_by_code[code])
        p = self._phase_by_code.get(code)
        if p is None:
            return "[系统 · 官方时间线] 未找到时期代码：{}".format(code)

        span = p.get("外界年代") or {}
        lines = ["[系统 · 官方时间线 · 参考]"]
        lines.append("阶段：{}（外界 {} - {} · 暴雨纪年 {} · 阿派朗纪年 {} · 维尔汀 {}）".format(
            p.get("标题"),
            span.get("起始显示", span.get("起始")),
            span.get("结束显示", span.get("结束")),
            p.get("暴雨纪年"), p.get("阿派朗纪年"), p.get("维尔汀年龄")))

        chapters = p.get("对应章节") or []
        if chapters:
            lines.append("对应剧情：" + "；".join(chapters))
        chars = p.get("关联角色") or []
        if chars:
            lines.append("本时段关联角色：" + "、".join(chars))

        events = p.get("事件") or []
        if events:
            picked = _even_sample(events, max_events)
            lines.append("大事记（节选 {}/{} 条）：".format(len(picked), len(events)))
            for ev in picked:
                lines.append("- {} {}".format(ev.get("时间", ""), ev.get("内容", "")))

        # 维尔汀动态（vertin_timeline.json 在此被引用）
        vrows = self._vertin_by_phase.get(code) or []
        if vrows:
            lines.append("维尔汀动态（引自 {}）：".format(VERTIN_FILE))
            for row in vrows[:max_chronology]:
                t = (row.get("时间") or {}).get("显示", "")
                who = "、".join(row.get("相关人物") or [])
                tail = "（相关：{}）".format(who) if who else ""
                lines.append("- {}（{}）：{}{}".format(t, row.get("年龄", ""),
                                                       row.get("事件", ""), tail))

        chron = self._chronology_lines(p, max_chronology)
        if chron:
            lines.append("前后暴雨：")
            lines.extend(chron)

        notes = p.get("备注") or []
        if notes:
            lines.append("考据备注：" + "；".join(notes))
        lines.append("注：以上是官方时间线的「前因」，玩家所在时间线可以偏离；"
                     "未提到的人物与细节按（不可观测）处理。")
        return "\n".join(lines)

    def _storm_context(self, s):
        rb = s.get("回溯") or {}
        lines = ["[系统 · 官方时间线 · 暴雨记录]"]
        lines.append("{}：外界 {} -> {}（{} · 暴雨纪年 {} · 阿派朗纪年 {} · 维尔汀 {}）".format(
            s.get("名称"), rb.get("起点显示"), rb.get("落点显示"), rb.get("方向"),
            s.get("暴雨纪年"), s.get("阿派朗纪年"), s.get("维尔汀年龄")))
        if s.get("症候群"):
            lines.append("暴雨症候群：" + s["症候群"])
        victims = s.get("被回溯人物") or []
        if victims:
            lines.append("被回溯的人物：" + "、".join(victims))
        for note in (s.get("备注") or []):
            lines.append("- " + note)
        vrows = self._vertin_by_phase.get(s.get("代码")) or []
        for row in vrows:
            t = (row.get("时间") or {}).get("显示", "")
            lines.append("- 维尔汀（{}，{}）：{}".format(t, row.get("年龄", ""),
                                                         row.get("事件", "")))
        return "\n".join(lines)

    def _chronology_lines(self, p, max_lines):
        out = []
        for label, key in (("上一场", "前一场暴雨"), ("下一场", "后一场暴雨")):
            scode = p.get(key)
            if not scode:
                continue
            s = self._storm_by_code.get(scode)
            if not s:
                continue
            rb = s.get("回溯") or {}
            out.append("- {}：{}（{} -> {} · {} · 症候：{}）".format(
                label, s.get("名称"), rb.get("起点显示"), rb.get("落点显示"),
                rb.get("方向"), s.get("症候群") or "未知"))
            if len(out) >= max_lines:
                break
        return out

    # ---- 维尔汀时间表 -------------------------------------------------------

    def vertin_entries(self):
        return list(self._vertin_rows)

    def vertin_at(self, year):
        """按外界年份找维尔汀条目：年份直接命中，或其时期代码与该年所处平稳期相同。"""
        try:
            y = int(year)
        except (TypeError, ValueError):
            return []
        hits = [r for r in self._vertin_rows if (r.get("时间") or {}).get("标准") == y]
        phase = self.get_phase_for_ext_year(y)
        if phase:
            for r in self._vertin_by_phase.get(phase["code"], []):
                if r not in hits:
                    hits.append(r)
        hits.sort(key=lambda r: r.get("序号", 0))
        return hits

    def vertin_context(self, year, max_entries=3):
        rows = self.vertin_at(year)[:max_entries]
        if not rows:
            return ""
        lines = ["[系统 · 维尔汀时间表 · {} 年前后]".format(year)]
        for r in rows:
            t = (r.get("时间") or {}).get("显示", "")
            who = "、".join(r.get("相关人物") or [])
            tail = "（相关：{}）".format(who) if who else ""
            lines.append("- {}（{}）：{}{}".format(t, r.get("年龄", ""),
                                                   r.get("事件", ""), tail))
        return "\n".join(lines)


def _even_sample(seq, n):
    """超长列表均匀抽样，保持时间顺序覆盖整个时段。"""
    if n is None or n <= 0 or len(seq) <= n:
        return list(seq)
    if n == 1:
        return [seq[0]]
    step = (len(seq) - 1) / float(n - 1)
    return [seq[round(i * step)] for i in range(n)]


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _candidate_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    cands = []
    for base in (here, cwd):
        cands += [
            os.path.join(base, "rain1999", "data", "timeline"),
            os.path.join(base, "data", "timeline"),
            os.path.join(base, "timeline"),
            base,
        ]
    seen, out = set(), []
    for c in cands:
        c = os.path.abspath(c)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def locate_timeline_dir(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    for d in _candidate_dirs():
        if os.path.exists(os.path.join(d, INDEX_FILE)):
            return d
    return None


def cmd_check(db):
    errors = []
    codes = [p.get("代码") for p in db.phases()]
    scodes = [s.get("代码") for s in db.storms()]
    if len(codes) != len(set(codes)):
        errors.append("时期代码有重复")
    if len(scodes) != len(set(scodes)):
        errors.append("暴雨代码有重复")
    for s in db.storms():
        for k in ("前接时期", "后接时期"):
            if s.get(k) not in codes:
                errors.append("{} 的 {} 无效：{}".format(s.get("代码"), k, s.get(k)))
    for p in db.phases():
        for k in ("前一场暴雨", "后一场暴雨"):
            v = p.get(k)
            if v is not None and v not in scodes:
                errors.append("{} 的 {} 无效：{}".format(p.get("代码"), k, v))
        for ev in (p.get("事件") or []):
            if set(ev.keys()) != {"时间", "内容"}:
                errors.append("{} 的事件字段异常：{}".format(p.get("代码"), ev))
    known = set(codes) | set(scodes)
    for r in db.vertin_entries():
        if r.get("时期代码") not in known:
            errors.append("维尔汀年表第 {} 条时期代码无效：{}".format(r.get("序号"), r.get("时期代码")))
    return errors


def cmd_show(db, phase_code=None):
    out = []
    if phase_code:
        p = db.phase(phase_code)
        if not p:
            print("未找到时期：" + phase_code)
            return 1
        print(db.phase_context(phase_code, max_events=10**9, max_chronology=10**9))
        return 0
    storm_by_code = {s["代码"]: s for s in db.storms()}
    for p in db.phases():
        span = p.get("外界年代") or {}
        out.append("【{}】{}".format(p.get("代码"), p.get("标题")))
        out.append("  外界 {} - {} · 暴雨纪年 {} · 维尔汀 {}".format(
            span.get("起始显示", span.get("起始")), span.get("结束显示", span.get("结束")),
            p.get("暴雨纪年"), p.get("维尔汀年龄")))
        nxt = p.get("后一场暴雨")
        if nxt and nxt in storm_by_code:
            s = storm_by_code[nxt]
            rb = s.get("回溯") or {}
            out.append("  └─ {}：{} -> {}（{}）症候：{}".format(
                s.get("名称"), rb.get("起点显示"), rb.get("落点显示"),
                rb.get("方向"), s.get("症候群") or "未知"))
    print("\n".join(out))
    return 0


def cmd_vertin(db, year=None):
    if year is not None:
        block = db.vertin_context(year, max_entries=10**9)
        print(block or "（{} 年没有命中维尔汀条目）".format(year))
        return 0
    for r in db.vertin_entries():
        t = (r.get("时间") or {}).get("显示", "")
        chapters = "；".join(r.get("对应章节") or [])
        who = "、".join(r.get("相关人物") or [])
        print("{:>3}. [{}] {}（{}）".format(r.get("序号"), r.get("时期代码"), t, r.get("年龄")))
        print("     " + r.get("事件", ""))
        if chapters:
            print("     章节：" + chapters)
        if who:
            print("     人物：" + who)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="timeline_organizer.py",
        description="《重返未来：1999》暴雨时间线加载/校验工具（供 rain1999_sim.py 引用）")
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "show", "vertin", "context"],
                    help="check=校验结构（默认） show=总览 vertin=维尔汀年表 context=预览模拟器注入文本")
    ap.add_argument("code", nargs="?", default=None,
                    help="context 命令的时期/暴雨代码，或 show --phase 的简写")
    ap.add_argument("--dir", default=None, help="时间线目录（含 index.json）")
    ap.add_argument("--phase", default=None, help="show 命令只看指定时期")
    ap.add_argument("--year", type=int, default=None, help="vertin 命令按外界年份查询")
    args = ap.parse_args(argv)

    tdir = locate_timeline_dir(args.dir)
    if tdir is None:
        print("未找到时间线目录（含 index.json）。请用 --dir 指定，")
        print("或把 index.json 放到 rain1999/data/timeline/ 下。")
        return 2
    try:
        db = TimelineDB(tdir)
    except (OSError, ValueError) as e:
        print("加载失败：" + str(e))
        return 2

    print("时间线目录：" + tdir)
    if args.cmd == "check":
        errors = cmd_check(db)
        n_events = sum(len(p.get("事件") or []) for p in db.phases())
        print("时期 {} 个 · 暴雨 {} 场 · 事件 {} 条 · 维尔汀年表 {} 条".format(
            len(db.phases()), len(db.storms()), n_events, len(db.vertin_entries())))
        if errors:
            print("发现问题 {} 处：".format(len(errors)))
            for e in errors:
                print("  - " + e)
            return 1
        print("结构校验通过。")
        # 演示一次与模拟器完全相同的调用
        probe = db.get_phase_for_ext_year(1929)
        if probe:
            print("探测 1929 年 -> {} · {}".format(probe["code"], probe["title"]))
        return 0
    if args.cmd == "show":
        return cmd_show(db, args.phase or args.code)
    if args.cmd == "vertin":
        return cmd_vertin(db, args.year)
    if args.cmd == "context":
        code = args.code or args.phase
        if not code:
            print("context 需要一个时期/暴雨代码，例如：python timeline_organizer.py context calm_after_storm_08")
            return 2
        print("-" * 60)
        print(db.phase_context(code, max_events=5, max_chronology=6))
        print("-" * 60)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
