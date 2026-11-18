# -*- coding: utf-8 -*-
"""
缩量二次突破模型（SVDB）· 可复现回测

用途：让模型的效果数字有据可查（此前 README 引用的「10日胜率 63.2% / 平均 +4.51%」
在仓库里没有对应代码，无法复现；本脚本用于取代那些无源数字）。

口径与实盘完全一致：
  - 用 shrink_breakout.scan_one 同一套判定（不另写一份逻辑，避免两套口径漂移）；
  - 逐交易日回放，每个提示日只用 date <= D 的 K 线（无未来函数）；
  - 买入价 = 提示日收盘价；收益 = 提示日收盘买入后第 N 个交易日收盘的涨跌幅；
  - 基准 = 同期全市场等权（逐票 N 日收益的横截面平均）；
  - 附加统计「提示日当日涨幅」与负涨幅占比 —— 用于监控方向性（曾经的缺陷）。

用法：
  python3 backtest_shrink.py [YYYY-MM-DD] [YYYY-MM-DD]   # 区间提示日
  KZ_KLINE_DIR=/path/to/kline python3 backtest_shrink.py  # 指定K线目录
输出：控制台表格 + state/reports/回测_SVDB_<起>_<止>.md
"""
import os
import sys
import glob
import json
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib
# KZ_MODEL 可指定对比模型模块名（默认 shrink_breakout），便于新旧口径同数据对比
_MODEL = os.environ.get("KZ_MODEL") or "shrink_breakout"
sh = __import__(_MODEL)

H = (1, 3, 5, 10)


def load_panel(kline_dir):
    """全市场收盘价面板（用于等权基准）。"""
    closes = {}
    for f in sorted(glob.glob(os.path.join(kline_dir, "*.csv"))):
        try:
            df = pd.read_csv(f, usecols=["date", "close"])
            if len(df) < 80:
                continue
            df["date"] = df["date"].astype(str)
            df = df.sort_values("date")
            s = pd.Series(df["close"].values, index=df["date"].values)
            closes[os.path.basename(f)[:6]] = s[~s.index.duplicated()]
        except Exception:
            continue
    if not closes:
        return None
    return pd.DataFrame(closes).sort_index()


def _scan_file(args):
    """单只股票的全区间回放（供多进程调用）。"""
    f, d0, d1 = args
    rows = []
    try:
        df = pd.read_csv(f)
        if len(df) < 80:
            return rows
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        c = df["close"].values.astype(float)
        d = df["date"].values
        n = len(df)
        code = os.path.basename(f)[:6]
        for i in range(70, n):          # scan_one 要求至少 70 行 K 线
            ds = str(d[i])
            if ds < d0 or ds > d1:
                continue
            r = sh.scan_one(code, df.iloc[: i + 1], as_of=ds)
            if not r:
                continue
            rec = dict(code=code, date=ds,
                       chg=(c[i] / c[i - 1] - 1) * 100,
                       strength=r.get("strength"), vol_ratio=r.get("vol_ratio"),
                       gap=r.get("gap"), score=r.get("score"))
            for k in H:
                rec[f"r{k}"] = (c[i + k] / c[i] - 1) * 100 if i + k < n else np.nan
            rows.append(rec)
    except Exception:
        return rows
    return rows


def main():
    d0 = sys.argv[1] if len(sys.argv) > 1 else "2026-08-03"
    d1 = sys.argv[2] if len(sys.argv) > 2 else datetime.date.today().isoformat()
    kdir = os.environ.get("KZ_KLINE_DIR") or lib.KLINE_DIR

    panel = load_panel(kdir)
    idx_of = {}
    if panel is not None:
        idx_of = {x: i for i, x in enumerate(panel.index)}

    def bench_k(date, k):
        i = idx_of.get(date)
        if i is None or i + k >= len(panel):
            return np.nan
        return float((panel.iloc[i + k] / panel.iloc[i] - 1.0).mean() * 100)

    files = sorted(glob.glob(os.path.join(kdir, "*.csv")))
    jobs = int(os.environ.get("KZ_BT_JOBS") or max(1, (os.cpu_count() or 2) - 1))
    print(f"回测 {d0} ~ {d1}｜股票 {len(files)} 只｜并行 {jobs} 进程", flush=True)
    rows = []
    if jobs > 1:
        import multiprocessing as mp
        with mp.Pool(jobs) as pool:
            for k, rs in enumerate(pool.imap_unordered(
                    _scan_file, [(f, d0, d1) for f in files], chunksize=16)):
                rows.extend(rs)
                if (k + 1) % 500 == 0:
                    print(f"  ...{k+1}/{len(files)} 信号 {len(rows)}", flush=True)
    else:
        for k, f in enumerate(files):
            rows.extend(_scan_file((f, d0, d1)))
            if (k + 1) % 500 == 0:
                print(f"  ...{k+1}/{len(files)} 信号 {len(rows)}", flush=True)

    if not rows:
        print("区间内无信号（检查 K 线目录与日期区间）")
        return
    sig = pd.DataFrame(rows)
    L = [f"# 回测 · 缩量二次突破 SVDB [{_MODEL}] · {d0} ~ {d1}", "",
         f"> 判定内核：`{_MODEL}.scan_one`（与实盘同一份代码，无未来函数）",
         f"> 买入价 = 提示日收盘价；基准 = 同期全市场等权；样本 {len(sig)} 个信号", ""]
    L.append("| 持有期 | 有效样本 | 平均涨幅 | 上涨胜率 | 同期基准 | 超额 | 跑赢率 |")
    L.append("|---|---|---|---|---|---|---|")
    for k in H:
        r = sig[f"r{k}"].dropna()
        if not len(r):
            continue
        b = np.array([bench_k(x, k) for x in sig.loc[r.index, "date"]])
        ok = ~np.isnan(b)
        r, b = r[ok], b[ok]
        ex = r.values - b
        L.append(f"| {k} 日 | {len(r)} | {r.mean():+.2f}% | {(r>0).mean()*100:.1f}% | "
                 f"{b.mean():+.2f}% | {ex.mean():+.2f}pp | {(ex>0).mean()*100:.1f}% |")
    neg = (sig["chg"] < 0).mean() * 100
    big = (sig["chg"] < -3).mean() * 100
    L.append("")
    L.append(f"- 提示日当日涨幅：均值 {sig['chg'].mean():+.2f}%、中位 {sig['chg'].median():+.2f}%、"
             f"负涨幅占比 **{neg:.1f}%**、大跌(<-3%)占比 **{big:.1f}%**")
    L.append("- 方向性监控：负涨幅占比应接近 0；若明显 >20% 说明「二次上攻」判据被放宽，"
             "须检查 `require_up_day` / `close_high_min` 是否生效。")
    md = "\n".join(L)
    print(md)
    try:
        os.makedirs(lib.REPORT_DIR, exist_ok=True)
        suffix = "" if _MODEL == "shrink_breakout" else f"_{_MODEL}"
        rp = os.path.join(lib.REPORT_DIR, f"回测_SVDB_{d0}_{d1}{suffix}.md")
        open(rp, "w", encoding="utf-8").write(md)
        print(f"\n报告：{rp}")
    except Exception as e:
        print(f"（报告写入失败：{e}）")


if __name__ == "__main__":
    main()
