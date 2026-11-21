#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影子一号（SHADOW-1）—— 三模型协同共振 · 阶段1 影子运行观察器
================================================================
定位：纯观察任务，不推送、不进任何模型正式台账、不参与 run_all 的模型结算。
每天收盘后：记录新共振信号 → 随行情更新历史信号后续收益 → 分组评估 → 出报告。

【不碰】三套模型的任何代码/参数/台账/推送；只读它们的台账 + 行情库。
【只写】自己的 scripts/shadow_one_state/ 目录。

cohort 区分（避免用已知结果自我验证）：
  hist = 阶段0导入的历史共振（anchor 早于启动日，后续结果已知，仅作基线）
  live = 影子一号启动后新增（anchor >= 启动日，真正的前瞻验证样本）

达标闸门（promote_ready）：live 组 A/S 级累计 >=30 条 且 r5 胜率 >=45% 且 r5 均值 >=0。
达标后才提请用户决定是否引入正式优先级；未达标继续观察。

命令：
  python3 scripts/shadow_one.py                 # record+settle+evaluate+report 一条龙
  python3 scripts/shadow_one.py init            # 首次：导入阶段0历史共振（cohort=hist）
  python3 scripts/shadow_one.py record          # 只记当日新增/升级共振
  python3 scripts/shadow_one.py settle          # 只更新后续收益
  python3 scripts/shadow_one.py evaluate        # 只评估闸门
  python3 scripts/shadow_one.py report          # 只输出报告
"""
import os, sys, json, datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATE = os.path.join(HERE, "shadow_one_state")
os.makedirs(STATE, exist_ok=True)
LEDGER = os.path.join(STATE, "ledger.jsonl")
SNAP = os.path.join(STATE, "snapshot.json")
REPORT = os.path.join(STATE, "report.md")
for _p in (HERE, REPO, os.path.join(REPO, "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from synergy_observer import (load_all, trade_calendar, date_idx, classify,
                              fwd_returns, WIN, FWD)

MAX_ANCHOR_AGE = 120        # 只跟踪最近120个交易日内的锚点
GRADES = ("S", "A", "B", "R")


def jload_lines(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def jwrite_lines(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def jsave(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def today():
    return datetime.date.today().isoformat()


def scan_resonance():
    """复用阶段0算法：按 code 分组，20日窗口扫描共振，返回 {(code,anchor):{...}}。"""
    sig = load_all()
    cal = trade_calendar()
    idx = date_idx(cal)
    by_code = defaultdict(list)
    for s in sig:
        if s["date"] in idx and s["code"]:
            by_code[s["code"]].append(s)
    print(f"[shadow1] 一次性加载 {len(by_code)} 只股票行情...", flush=True)
    all_bars = cm_load_many(list(by_code.keys()))

    found = {}
    for code, items in by_code.items():
        items.sort(key=lambda x: idx[x["date"]])
        n = len(items)
        # 以每个信号为起点，收集窗口内后续信号（与阶段0完全同口径）
        for a in range(n):
            seq = [(items[a]["date"], items[a]["model"])]
            d0 = idx[items[a]["date"]]
            for b in range(a + 1, n):
                if idx[items[b]["date"]] - d0 <= WIN:
                    seq.append((items[b]["date"], items[b]["model"]))
                else:
                    break
            grade, desc = classify(seq)
            if grade not in GRADES:
                continue                      # C级单模型不记账
            anchor = seq[-1][0]               # 共振完成日
            if idx[anchor] < len(cal) - MAX_ANCHOR_AGE:
                continue                      # 太旧的锚点不跟踪
            key = (code, anchor)
            rec = dict(code=code, name=items[a].get("name", ""), anchor=anchor,
                       grade=grade, desc=desc,
                       seq=" → ".join(f"{m}@{d}" for d, m in seq),
                       n_models=len(set(m for _, m in seq)))
            # 同一 (code,anchor) 若有更完整的序列（grade 升级 S>A>B>R 或模型数更多），保留之
            rank = {"S": 4, "A": 3, "B": 2, "R": 1}
            prev = found.get(key)
            if prev is None or (rank[grade], rec["n_models"]) > (rank[prev["grade"]], prev["n_models"]):
                found[key] = rec
    return found, cal, idx, all_bars


def cm_load_many(codes):
    import common_lib as cm
    return cm.MARKET_STORE.load_many(codes)


def do_init():
    """首次：导入阶段0历史共振，全部标 cohort=hist。"""
    if os.path.exists(LEDGER) and jload_lines(LEDGER):
        print("[shadow1] ledger 已存在，init 跳过（避免覆盖 live 样本）")
        return
    found, cal, idx, bars = scan_resonance()
    rows = []
    for (code, anchor), rec in found.items():
        ret = fwd_returns(code, anchor, cal, idx, {}, bars)
        r = dict(rec)
        r.update({k: ret.get(k) for k in [f"r{n}" for n in FWD] + ["max_gain", "max_dd"]})
        r["cohort"] = "hist"
        r["first_seen"] = today()
        r["updated"] = today()
        rows.append(r)
    jwrite_lines(LEDGER, rows)
    snap = jload(SNAP, {})
    snap["start_date"] = today()
    snap["init_count"] = len(rows)
    jsave(SNAP, snap)
    print(f"[shadow1] init 导入历史共振 {len(rows)} 条，启动日 {today()}")


def do_record():
    """记录新共振 / 升级已有共振；anchor>=启动日的标 live。"""
    snap = jload(SNAP, {})
    start = snap.get("start_date", today())
    ledger = jload_lines(LEDGER)
    idxmap = {(r["code"], r["anchor"]): r for r in ledger}
    found, cal, idx, bars = scan_resonance()
    added = upgraded = 0
    for (code, anchor), rec in found.items():
        cohort = "live" if anchor >= start else "hist"
        key = (code, anchor)
        if key not in idxmap:
            r = dict(rec)
            r.update({k: None for k in [f"r{n}" for n in FWD] + ["max_gain", "max_dd"]})
            r["cohort"] = cohort
            r["first_seen"] = today()
            r["updated"] = today()
            ledger.append(r)
            idxmap[key] = r
            added += 1
        else:
            prev = idxmap[key]
            rank = {"S": 4, "A": 3, "B": 2, "R": 1}
            if (rank[rec["grade"]], rec["n_models"]) > (rank[prev["grade"]], prev["n_models"]):
                prev["grade"] = rec["grade"]
                prev["desc"] = rec["desc"]
                prev["seq"] = rec["seq"]
                prev["n_models"] = rec["n_models"]
                prev["updated"] = today()
                upgraded += 1
    jwrite_lines(LEDGER, ledger)
    snap["last_record"] = today()
    jsave(SNAP, snap)
    print(f"[shadow1] record 新增 {added} 条，升级 {upgraded} 条，台账共 {len(ledger)} 条")


def do_settle():
    """用最新行情更新所有 signal 的后续收益（未到期的留 None）。"""
    ledger = jload_lines(LEDGER)
    if not ledger:
        print("[shadow1] ledger 为空，settle 跳过"); return
    codes = list(set(r["code"] for r in ledger))
    bars = cm_load_many(codes)
    cal = trade_calendar(); idx = date_idx(cal)
    cache = {}
    filled = 0
    for r in ledger:
        ret = fwd_returns(r["code"], r["anchor"], cal, idx, cache, bars)
        if not ret:
            continue
        for k in [f"r{n}" for n in FWD] + ["max_gain", "max_dd"]:
            if k in ret and ret[k] is not None:
                if r.get(k) != ret[k]:
                    filled += 1
                r[k] = ret[k]
        r["settled_at"] = today()
    jwrite_lines(LEDGER, ledger)
    print(f"[shadow1] settle 更新 {filled} 个收益字段")


def stat_group(rows):
    g = {"n": len(rows)}
    for n in FWD:
        vals = [r[f"r{n}"] for r in rows if r.get(f"r{n}") is not None]
        if vals:
            g[f"r{n}_win"] = round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)
            g[f"r{n}_avg"] = round(sum(vals) / len(vals), 2)
            g[f"r{n}_n"] = len(vals)
    return g


def do_evaluate():
    ledger = jload_lines(LEDGER)
    snap = jload(SNAP, {})
    live = [r for r in ledger if r.get("cohort") == "live"]
    hist = [r for r in ledger if r.get("cohort") == "hist"]
    stats = {}
    for cohort, rows in (("live", live), ("hist", hist)):
        stats[cohort] = {g: stat_group([r for r in rows if r["grade"] == g]) for g in GRADES}
    # 闸门：live A/S 累计>=30 且 r5胜率>=45% 且 r5均值>=0
    core = [r for r in live if r["grade"] in ("S", "A")]
    core5 = [r["r5"] for r in core if r.get("r5") is not None]
    ready = False
    reason = ""
    if len(core) < 30:
        reason = f"live 核心样本 {len(core)}/30，未达标"
    elif not core5:
        reason = "live 核心样本 r5 尚未到期结算"
    else:
        win = 100 * sum(1 for v in core5 if v > 0) / len(core5)
        avg = sum(core5) / len(core5)
        ready = (win >= 45 and avg >= 0)
        reason = f"live 核心 {len(core)} 条, r5胜率{win:.1f}% 均值{avg:+.2f}%, " + ("达标" if ready else "未达标")
    snap["stats"] = stats
    snap["promote_ready"] = ready
    snap["gate_reason"] = reason
    snap["live_count"] = len(live)
    snap["hist_count"] = len(hist)
    snap["last_evaluate"] = today()
    jsave(SNAP, snap)
    print(f"[shadow1] evaluate: {reason}")
    return stats, ready, reason


def do_report():
    snap = jload(SNAP, {})
    stats = snap.get("stats", {})
    lines = ["# 影子一号（SHADOW-1）· 协同共振观察报告", "",
             f"报告日期：{today()}　启动日：{snap.get('start_date','-')}",
             f"历史基线(hist)：{snap.get('hist_count',0)} 条　前瞻样本(live)：{snap.get('live_count',0)} 条",
             f"闸门状态：**{'✅ 达标·提请决策' if snap.get('promote_ready') else '⏳ 观察中'}**　{snap.get('gate_reason','')}", "",
             "## 分组统计（胜率=正收益占比 / 均值收益%，N=已结算样本数）", ""]
    for cohort, label in (("live", "前瞻组 live（真正的验证样本）"), ("hist", "历史基线 hist（结果已知，仅参考）")):
        lines.append(f"### {label}")
        lines.append("| 等级 | 样本 | " + " | ".join(f"r{n} 胜率/均值(n)" for n in FWD) + " |")
        lines.append("|---|---|" + "|".join(["---"] * len(FWD)) + "|")
        for g in GRADES:
            s = stats.get(cohort, {}).get(g, {})
            if not s or not s.get("n"):
                lines.append(f"| {g} | 0 | " + " | ".join(["-"] * len(FWD)) + " |")
                continue
            cells = []
            for n in FWD:
                wv = s.get(f"r{n}_win"); av = s.get(f"r{n}_avg"); nn = s.get(f"r{n}_n", 0)
                cells.append(f"{wv}%/{av:+.2f}%({nn})" if wv is not None else "-")
            lines.append(f"| {g} | {s['n']} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["## 说明", "",
              "- 等级：S=凯众→破位→奥雅三段 / A=潜伏→放量确认 / B=双潜伏 / R=反向(放量→潜伏,疑似出货)",
              "- 只有 live 组用于判断协同是否提准；hist 组后续结果已知，不能自我验证。",
              "- 达标闸门：live 的 S/A 累计≥30 条且 r5 胜率≥45%、均值≥0，才提请引入正式优先级。",
              "- 本任务纯观察：不推送、不改任何模型代码/参数/台账。", ""]
    txt = "\n".join(lines)
    open(REPORT, "w", encoding="utf-8").write(txt)
    print(txt)


def main():
    args = sys.argv[1:]
    if not args:
        args = ["record", "settle", "evaluate", "report"]
    if "init" in args:
        do_init()
    if "record" in args:
        do_record()
    if "settle" in args:
        do_settle()
    if "evaluate" in args:
        do_evaluate()
    if "report" in args:
        do_report()


if __name__ == "__main__":
    main()
