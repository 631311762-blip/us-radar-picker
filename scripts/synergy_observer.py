#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三模型协同 · 阶段0 观察脚本（纯观察，不改任何模型/台账/推送）
================================================================
读取三套模型已有台账，归一化为 {code, date, model, score}，
按 code 分组、20 交易日窗口扫描多模型命中序列，
按「共振类型学」分级，并对每条信号计算信号后 N=3/5/10/20 日收益。

数据源（只读）：
  主升浪/奥雅  stock-v3/predictions.json        放量端
  凯众         kaizhong/state/v4_history.json    缩量端
  破位         powei/state/archive/*.json        价格行为端
  行情         common/market_store (load_bars)

输出（写到 /workspace，不写回仓库）：
  共振明细表.csv / 共振统计报告.md / 反向序列清单.csv

用法：
  python3 scripts/synergy_observer.py
"""
import os, sys, json, csv, glob, datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = "/workspace"
for _p in (REPO, os.path.join(REPO, "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common_lib as cm

WIN = 20          # 共振时间窗口（交易日）
FWD = (3, 5, 10, 20)   # 信号后观察日

# ---------- 代码归一化：去 sh/sz/bj 前缀，统一 6 位纯数字 ----------
def norm(code):
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[len(p):]
    return c.zfill(6) if c.isdigit() else c

# ---------- 读取三套台账 ----------
def load_all():
    sig = []   # {code,name,date,model,score,extra}
    # 主升浪/奥雅（放量端）
    p = os.path.join(REPO, "stock-v3", "predictions.json")
    for x in json.load(open(p, encoding="utf-8")):
        sig.append(dict(code=norm(x["code"]), name=x.get("name", ""),
                        date=x["date"], model="奥雅/主升浪",
                        score=x.get("score"), kind=x.get("kind", "")))
    # 凯众（缩量端）
    p = os.path.join(REPO, "kaizhong", "state", "v4_history.json")
    for x in json.load(open(p, encoding="utf-8")):
        sig.append(dict(code=norm(x["code"]), name=x.get("name", ""),
                        date=x["date"], model="凯众",
                        score=x.get("score"), kind=""))
    # 破位（价格行为端）—— 用 sig_day（收复日）作信号锚点
    for f in glob.glob(os.path.join(REPO, "powei", "state", "archive", "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for x in d.get("picks", []):
            sd = x.get("sig_day")
            if not sd:
                continue
            sig.append(dict(code=norm(x.get("code", "")), name=x.get("name", ""),
                            date=sd, model="破位", score=x.get("score"), kind=""))
    # 去重：同一 code + date + model 只保留一条（主升浪三榜同日推同一票会重复）
    seen = set(); out = []
    for s in sig:
        k = (s["code"], s["date"], s["model"])
        if k in seen:
            continue
        seen.add(k); out.append(s)
    return out

# ---------- 交易日序号（用全市场日历：取行情库交易日历） ----------
def trade_calendar():
    # 从 market_store 覆盖信息拿不到逐日列表，用任一大盘股的 bars 日期做日历
    b = cm.MARKET_STORE.load_bars("000001")  # 平安银行，交易日齐全
    return [x.get("day") or x.get("date") for x in b]

def date_idx(cal):
    return {d: i for i, d in enumerate(cal)}

# ---------- 后续收益 ----------
def fwd_returns(code, date, cal, idx, cache, all_bars=None):
    if code not in cache:
        cache[code] = (all_bars.get(code) if all_bars is not None
                       else cm.MARKET_STORE.load_bars(code))
    bars = cache.get(code) or []
    if not bars:
        return {}
    bmap = {}
    for x in bars:
        d = x.get("day") or x.get("date")
        if d:
            bmap[d] = float(x.get("close"))
    if date not in idx or date not in bmap:
        return {}
    base = bmap[date]
    if base <= 0:
        return {}
    i = idx[date]
    out = {}
    for n in FWD:
        j = i + n
        if j < len(cal) and cal[j] in bmap:
            out[f"r{n}"] = round((bmap[cal[j]] / base - 1) * 100, 2)
    # 窗口内最大浮盈 / 最大回撤
    seg = [bmap[cal[j]] for j in range(i + 1, min(i + 1 + max(FWD), len(cal))) if cal[j] in bmap]
    if seg:
        out["max_gain"] = round((max(seg) / base - 1) * 100, 2)
        out["max_dd"] = round((min(seg) / base - 1) * 100, 2)
    return out

# ---------- 共振分级 ----------
MODELS = {"凯众": "潜伏", "破位": "潜伏", "奥雅/主升浪": "放量"}

def classify(seq):
    """seq: [(date, model), ...] 按日期升序。返回 (等级, 说明)。"""
    models = [m for _, m in seq]
    uniq = set(models)
    has_kz = "凯众" in uniq
    has_pw = "破位" in uniq
    has_oy = "奥雅/主升浪" in uniq
    if len(uniq) == 1:
        return "C", "单模型"
    # 最后一次放量 vs 最后一次潜伏的先后
    last_oy = max((i for i, m in enumerate(models) if m == "奥雅/主升浪"), default=-1)
    last_latent = max((i for i, m in enumerate(models) if m in ("凯众", "破位")), default=-1)
    if has_oy and last_oy > last_latent:
        if has_kz and has_pw and has_oy:
            return "S", "凯众→破位→奥雅 三段标准演进"
        return "A", "潜伏端→放量确认"
    if has_oy and last_oy < last_latent:
        return "R", "放量在前·潜伏在后（疑似拉高出货，风险）"
    if has_kz and has_pw:
        return "B", "凯众+破位 双潜伏"
    return "B", "双模型共振"

# ---------- 主流程 ----------
def main():
    sig = load_all()
    cal = trade_calendar()
    idx = date_idx(cal)
    by_code = defaultdict(list)
    for s in sig:
        if s["date"] in idx and s["code"]:
            by_code[s["code"]].append(s)

    # 一次性加载所有涉及股票的行情（避免逐只全表扫描）
    print(f"[info] 一次性加载 {len(by_code)} 只股票的行情...", flush=True)
    all_bars = cm.MARKET_STORE.load_many(list(by_code.keys()))

    rows = []
    cache = {}
    for code, items in by_code.items():
        items.sort(key=lambda x: idx[x["date"]])
        # 滑动窗口：以每个信号为起点，收集 WIN 交易日内的后续命中
        n = len(items)
        for a in range(n):
            seq = [(items[a]["date"], items[a]["model"])]
            d0 = idx[items[a]["date"]]
            for b in range(a + 1, n):
                if idx[items[b]["date"]] - d0 <= WIN:
                    seq.append((items[b]["date"], items[b]["model"]))
                else:
                    break
            # 只保留窗口内至少含2个不同模型的序列做分级；单模型另记
            grade, desc = classify(seq)
            # 收益以序列中「最后一个信号日」为锚（共振完成日），单模型以自身信号日
            anchor = seq[-1][0]
            ret = fwd_returns(code, anchor, cal, idx, cache, all_bars)
            if not ret:
                continue
            rows.append(dict(
                code=code, name=items[a].get("name", ""), grade=grade, desc=desc,
                anchor=anchor, seq=" → ".join(f"{m}@{d}" for d, m in seq),
                n_models=len(set(m for _, m in seq)), **ret))

    if not rows:
        print("无有效样本"); return

    # 去重：同一 code+anchor+grade 保留一条
    seen = set(); uniq_rows = []
    for r in rows:
        k = (r["code"], r["anchor"], r["grade"])
        if k in seen:
            continue
        seen.add(k); uniq_rows.append(r)
    rows = uniq_rows

    # ---- 输出1：明细表 ----
    detail = os.path.join(OUT, "共振明细表.csv")
    cols = ["code", "name", "grade", "desc", "anchor", "n_models", "seq"] + \
           [f"r{n}" for n in FWD] + ["max_gain", "max_dd"]
    with open(detail, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in sorted(rows, key=lambda x: (x["grade"], -x.get("n_models", 0))):
            w.writerow({k: r.get(k, "") for k in cols})

    # ---- 输出2：反向序列清单 ----
    rev = [r for r in rows if r["grade"] == "R"]
    revf = os.path.join(OUT, "反向序列清单.csv")
    with open(revf, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rev:
            w.writerow({k: r.get(k, "") for k in cols})

    # ---- 输出3：统计报告 ----
    def stat(group):
        g = [r for r in rows if r["grade"] == group]
        if not g:
            return None
        out = {"n": len(g)}
        for n in FWD:
            vals = [r[f"r{n}"] for r in g if f"r{n}" in r]
            if vals:
                out[f"r{n}_win"] = round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)
                out[f"r{n}_avg"] = round(sum(vals) / len(vals), 2)
        return out

    stats = {g: stat(g) for g in ["S", "A", "B", "C", "R"]}
    lines = ["# 三模型协同 · 阶段0 回溯统计报告", "",
             f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"共振窗口：{WIN} 交易日　信号源：主升浪{predictions_count()}条 / 凯众{sum(1 for s in sig if s['model']=='凯众')}条 / 破位{sum(1 for s in sig if s['model']=='破位')}条",
             f"有效共振样本：{len(rows)} 条（去重后）", "",
             "## 分级统计（收益单位 %）", "",
             "| 等级 | 样本数 | " + " | ".join(f"r{n}胜率/r{n}均值" for n in FWD) + " |",
             "|---|---|" + "|".join(["---"] * len(FWD)) + "|"]
    for g in ["S", "A", "B", "C", "R"]:
        s = stats[g]
        if not s:
            lines.append(f"| {g} | 0 | " + " | ".join(["-"] * len(FWD)) + " |")
            continue
        cells = []
        for n in FWD:
            wv = s.get(f"r{n}_win"); av = s.get(f"r{n}_avg")
            cells.append(f"{wv}%/{av}%" if wv is not None else "-")
        lines.append(f"| {g} | {s['n']} | " + " | ".join(cells) + " |")
    lines += ["", "## 结论提示", "",
              "- S/A 级若胜率与均值显著高于 C 级（单模型），协同有提准价值。",
              "- R 级（放量在前、潜伏在后）若胜率明显偏低，验证「反向=出货」过滤逻辑成立。",
              "- 样本数 <30 的等级只作参考，不下定论。", ""]
    rpt = os.path.join(OUT, "共振统计报告.md")
    open(rpt, "w", encoding="utf-8").write("\n".join(lines))

    print(f"[OK] 明细 {len(rows)} 条 → {detail}")
    print(f"[OK] 反向序列 {len(rev)} 条 → {revf}")
    print(f"[OK] 统计报告 → {rpt}")
    print("\n".join(lines))

def predictions_count():
    return sum(1 for s in load_all() if s["model"] == "奥雅/主升浪")

if __name__ == "__main__":
    main()
