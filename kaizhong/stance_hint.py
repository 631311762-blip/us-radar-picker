# -*- coding: utf-8 -*-
"""
【突破提示】独立扫描器 v4（不考虑试盘因素；提示点 = 放量突破当天）

依据用户 2026-11-06 要求简化后的结构：
  【硬前置】提示日前约两个月（40 个交易日）横盘，且前后两月各自横盘
    → 站稳 MA5（连续 2 日收盘 > MA5、MA5 上行）
    → 【提示日 = 放量突破当天】收盘 ≥ 触发价 且 成交量 ≥ 所需量
    → 当天给出「平台上沿 / 触发价 / 所需量 / 止损」，提示即买点

与 v3 的区别：
  - 不再识别「试盘日」（温和放量冲高 + 次日大跌洗盘），也不再要求缩量整理 / 整理期企稳；
  - 横盘窗口、平台上沿、所需量全部改以「提示日」为锚，不再依赖试盘日；
  - 提示点从「突破前一日站稳 MA5」改为「突破当天」（凯众原型：10-27，而非 10-26）。

验证（凯众 603037）：10-26 收 10.79 < 触发价 11.25 → 不提示；
10-27 收 11.87、量 21.78 万手 ≥ 所需量 → 唯一提示；10-28 起横盘窗口被涨停污染 → 不再提示。

重要边界：
  - 提示即买点，但成交仍须「收盘 ≥ 触发价 且 量 ≥ 所需量」同时满足。
  - 不写主台账 history.json，不改推送策略；不 import 主模型 analyze / 不改 SCHEME / BUY_P。
  - 合规剔除复用 lib.blocked_by_rule；只读本地 kline 缓存 + F10。

用法：python3 stance_hint.py [YYYY-MM-DD] [TOP]
"""
import os
import sys
import glob
import json
from datetime import datetime

import numpy as np
import pandas as pd

import lib

# -------- 突破形态参数（凯众 2026-10-27 突破日为原型校准）--------
STANCE = {
    # 硬前置：提示日前 40 个交易日横盘（只用收盘价，避免单根影线放大）
    "sideways_days": 40,             # 约两个月交易日
    "sideways_range_max": 1.20,      # 整段「收盘 max/min」≤（凯众 10-27 前 1.133）
    "sideways_std_max": 0.045,       # 整段「收盘 std/mean」≤
    "sideways_seg_range_max": 1.20,  # 前后各 20 日各自收盘 max/min ≤（对应「8月和9月均横盘」）

    # 形态健康度
    "stance_days": 2,                # 连续 N 日收盘 > MA5

    # 突破与买点
    "breakout_buf": 1.005,           # 触发价 = 平台上沿 × 该系数
    "vol_ratio": 1.8,                # 所需量 ≥ 近 5 日均量 × 该系数
    "sideways_vol_x": 2.0,           # 所需量 ≥ 横盘期均量 × 该系数
    "stop_loss": 0.05,               # 单笔风险上限 5%
}


def sideways_ok_at(c, cfg, k):
    """位置 k 之前的 sideways_days 个交易日是否满足「长期横盘」。

    只用收盘价：最高/最低会被单根长影线放大，把正常平台误判为不横盘。
    返回 dict(ok, rng, std)。
    """
    sw = cfg["sideways_days"]
    if k < sw:
        return dict(ok=False, rng=None, std=None)
    cc = c[k - sw:k]
    lo = float(cc.min())
    cm = float(cc.mean())
    if lo <= 0 or cm <= 0:
        return dict(ok=False, rng=None, std=None)
    rng = float(cc.max()) / lo
    std = float(cc.std()) / cm
    res = dict(ok=True, rng=rng, std=std)
    if rng > cfg["sideways_range_max"] or std > cfg["sideways_std_max"]:
        res["ok"] = False
        return res
    half = sw // 2
    c1, c2 = cc[:half], cc[half:]
    if (c1.max() / max(c1.min(), 1e-9) > cfg["sideways_seg_range_max"]
            or c2.max() / max(c2.min(), 1e-9) > cfg["sideways_seg_range_max"]):
        res["ok"] = False
    return res


def scan_one(code, df, as_of=None):
    """站在 as_of 收盘，只用截止当日数据，判定当日是否为「横盘后放量突破」提示日。"""
    df = df.reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    if as_of:
        df = df[df["date"] <= str(as_of)]
    if len(df) < 70:
        return None
    df = df.tail(160).reset_index(drop=True)
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    h = df["high"].values.astype(float)
    o = df["open"].values.astype(float)
    d = df["date"].values
    n = len(df)
    i = n - 1

    if c[i] < lib.MIN_PRICE:
        return None
    if float((c[-20:] * v[-20:]).mean()) < lib.MIN_AMOUNT:
        return None

    # ① 硬前置：提示日前约两个月横盘（不依赖试盘日）
    sw = sideways_ok_at(c, STANCE, i)
    if not sw["ok"]:
        return None
    w = slice(i - STANCE["sideways_days"], i)

    # ② 站稳 MA5（连续 N 日收盘 > MA5、MA5 上行）
    ma5 = pd.Series(c).rolling(5).mean().values
    sd = STANCE["stance_days"]
    if not all(c[j] > ma5[j] for j in range(i - sd + 1, i + 1)):
        return None
    if not (ma5[i] >= ma5[i - 1]):
        return None

    # ③ 平台上沿 = 横盘窗口（不含当日）的最高价；触发价 / 所需量
    p_top = float(h[w].max())
    if p_top <= 0:
        return None
    trigger = round(p_top * STANCE["breakout_buf"], 2)
    req_vol = int(max(float(v[i - 5:i].mean()) * STANCE["vol_ratio"],
                      float(v[w].mean()) * STANCE["sideways_vol_x"]))

    # ④ 提示条件 = 放量突破当天：收盘 ≥ 触发价 且 量 ≥ 所需量（两个条件同时满足）
    if c[i] < trigger or v[i] < req_vol:
        return None

    vol_x = v[i] / max(float(v[i - 5:i].mean()), 1.0)
    prox = c[i] / p_top
    chg = (c[i] / c[i - 1] - 1) * 100 if c[i - 1] > 0 else 0.0
    score = (0.35 * (1 - min(sw["rng"] / STANCE["sideways_range_max"], 1.0))
             + 0.35 * min(vol_x / 3.0, 1.0)
             + 0.30 * min(prox / 1.15, 1.0))
    sw_start = str(d[i - STANCE["sideways_days"]])
    sw_end = str(d[i - 1])
    return dict(
        code=code, date=str(d[i]), score=round(score, 4),
        ma5=round(float(ma5[i]), 2),
        platform_top=round(p_top, 2), trigger=trigger,
        req_vol=req_vol, req_vol_wan=round(req_vol / 1e6, 2),
        stop=round(trigger * (1 - STANCE["stop_loss"]), 2),
        last_close=round(float(c[i]), 2),
        chg=round(float(chg), 2), vol_x=round(float(vol_x), 2), prox=round(prox, 3),
        sw_days=int(STANCE["sideways_days"]),
        sw_range=round(float(sw["rng"]), 3),
        sw_std=round(float(sw["std"]), 3),
        sw_win=f"{sw_start}~{sw_end}",
    )


def main():
    as_of = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    f10 = lib.load_f10()
    names = {}
    nf = os.path.join(lib.DATA, "names.json")
    if os.path.exists(nf):
        try:
            names = json.load(open(nf, encoding="utf-8"))
        except Exception:
            pass

    hits = []
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        row = f10.get(code, {})
        name = names.get(code) or row.get("name") or code
        if lib.blocked_by_rule(name, code, row):   # 合规剔除静默执行
            continue
        try:
            df = pd.read_csv(f)
            r = scan_one(code, df, as_of=as_of)
            if r:
                r["name"] = name
                r["industry"] = row.get("industry", "")
                r["concepts"] = row.get("concepts", "")
                hits.append(r)
        except Exception:
            continue
    hits.sort(key=lambda x: -x["score"])

    L = []
    L.append(f"# 横盘突破提示 · {as_of}")
    L.append("")
    L.append("> **结构：横盘约两个月 → 站稳 MA5 → 放量突破当天提示**；不考虑试盘因素。")
    L.append("> 硬前置：提示日前 40 个交易日收盘价横盘（max/min ≤1.20、std/mean ≤0.045），"
             "前后两月分别横盘；仅用收盘价，避免影线干扰。")
    L.append("> **提示即买点**：收盘 ≥ 触发价 **且** 成交量 ≥ 所需量，两个条件同时满足才成立。")
    L.append("")
    L.append(f"全市场扫描命中 **{len(hits)}** 只，按评分取前 {min(top, len(hits))} 只：")
    L.append("")
    L.append("| 名称 | 代码 | 横盘区间 | 收盘振幅/std | 涨幅 | 量比 | 平台上沿 | 触发价 | 所需量(万手) | 止损 | 现价 | 评分 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in hits[:top]:
        L.append(f"| {r['name']} | {r['code']} | {r['sw_win']} | {r['sw_range']}/{r['sw_std']} | "
                 f"{r['chg']}% | {r['vol_x']} | {r['platform_top']} | {r['trigger']} | "
                 f"{r['req_vol_wan']} | {r['stop']} | {r['last_close']} | {r['score']} |")
    L.append("")
    L.append("> 止损统一 -5%；回踩不是买点。（合规剔除已在筛选阶段静默完成，不单独列出）")
    rp = os.path.join(lib.REPORT_DIR, f"stance_{as_of}.md")
    open(rp, "w", encoding="utf-8").write("\n".join(L))
    json.dump(hits[:top], open(os.path.join(lib.REPORT_DIR, f"stance_{as_of}.json"), "w",
                              encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"命中 {len(hits)} 只 → 报告 {rp}")
    for r in hits[:top]:
        print(f"  {r['name']}({r['code']}) 涨{r['chg']}% 量比{r['vol_x']} "
              f"横盘{r['sw_win']}({r['sw_range']}/{r['sw_std']}) "
              f"触发{r['trigger']} 需量{r['req_vol_wan']}万手 评分{r['score']}")
    return hits


if __name__ == "__main__":
    main()
