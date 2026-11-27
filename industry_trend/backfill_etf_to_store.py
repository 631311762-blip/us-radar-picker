# -*- coding: utf-8 -*-
"""
把行业 ETF 的 qfq 日线回填到共享行情表 common/state/market_daily。

用途（可选，2026-11-27 备好）：
  当前行业趋势模型的 ETF 周线用 akshare 现抓；若要让 ETF 与个股同表、
  模型统一走 load_kline 读本地，可运行本脚本一次性回填 + 每日随流水线增量更新。

运行方式（在仓库 common/ 目录下，或设置 REPO_ROOT）：
  python industry_trend/backfill_etf_to_store.py

机制：
  - 用 akshare fund_etf_hist_em(adjust='qfq') 抓每只 ETF 日线；
  - 转成共享表标准 bar {code,date,open,high,low,close,volume,turnover}；
  - 用 market_store.MarketStore(root=<repo>/common/state/market_daily).save_bars 写入；
  - save_bars 只追加缺失记录、不覆盖已有，可安全重复运行（幂等）。

注意：save_bars 仅接受 >= START_DATE(2024-01-01) 的 bar；ETF 历史足够。
"""

import os
import sys
import json
import time
import warnings
from datetime import datetime

import pandas as pd
import akshare as ak

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.environ.get("REPO_ROOT", os.path.abspath(os.path.join(HERE, "..")))
COMMON_DIR = os.path.join(REPO_ROOT, "common")
STORE_ROOT = os.path.join(COMMON_DIR, "state", "market_daily")
ETF_FILE = os.path.join(HERE, "etf_universe.json")


def load_etf_codes():
    uni = json.load(open(ETF_FILE, encoding="utf-8"))
    return [(x["etf_code"], x.get("etf_name", "")) for x in uni]


def fetch_etf_bars(code, start="20240101", end=None):
    end = (end or datetime.now().strftime("%Y%m%d"))
    for attempt in range(3):
        try:
            df = ak.fund_etf_hist_em(symbol=str(code), period="daily",
                                     start_date=start, end_date=end, adjust="qfq")
            if df is None or len(df) == 0:
                return []
            bars = []
            for _, r in df.iterrows():
                try:
                    bars.append({
                        "code": str(code).zfill(6),
                        "date": str(r["日期"])[:10],
                        "open": float(r["开盘"]), "high": float(r["最高"]),
                        "low": float(r["最低"]), "close": float(r["收盘"]),
                        "volume": float(r["成交量"]),
                        "turnover": float(r.get("成交额", 0) or 0),
                    })
                except (ValueError, TypeError):
                    continue
            return bars
        except Exception:
            time.sleep(1.5)
    return []


def main():
    if not os.path.isdir(STORE_ROOT):
        print("共享行情表目录不存在:", STORE_ROOT)
        return
    sys.path.insert(0, COMMON_DIR)
    import market_store as ms
    store = ms.MarketStore(root=STORE_ROOT)

    codes = load_etf_codes()
    print("待回填行业ETF %d 只 -> %s" % (len(codes), STORE_ROOT))
    all_bars = []
    ok = 0
    for i, (code, name) in enumerate(codes, 1):
        bars = fetch_etf_bars(code)
        if bars:
            all_bars.extend(bars)
            ok += 1
            print("  [%d/%d] %s %s -> %d 根" % (i, len(codes), code, name, len(bars)))
        else:
            print("  [%d/%d] %s %s -> 失败" % (i, len(codes), code, name))
        time.sleep(0.2)

    if not all_bars:
        print("无数据，退出")
        return
    res = store.save_bars(all_bars, complete_codes=[c for c, _ in codes if any(b["code"] == c for b in all_bars)])
    print("\n回填完成: 成功 %d/%d 只, 新增 %d 行, 写入 %d 个分片" %
          (ok, len(codes), res.get("inserted", 0), res.get("shards_written", 0)))
    cov = store.coverage()
    print("行情表最新日:", cov.get("last_date"))


if __name__ == "__main__":
    main()
