# -*- coding: utf-8 -*-
"""领湃跟踪修正.py：B_V3.3 低位样本 T+3 / T+5 / T+10 跟踪结算。

三步流程最后一步：读取 领湃_V33跟踪台账.json，对所有未结算样本按
「提示日之后第 3/5/10 个交易日收盘 / 提示日收盘 - 1」回填收益，
汇总各档胜率、平均/中位收益。

2026-11-16（用户要求）：每天只收一份领湃推送。本脚本**默认静默**——
只回填台账、打印汇总，不再单独推送结算简报；结算进度已由
领湃模型_每日.py 合并进当日唯一一条选股清单推送。
若临时需要完整结算简报，设 LINGPEI_SETTLE_PUSH=1 单独开启（仍按天去重）。

统一流水线严格读取共同历史行情库；独立运行才允许显式本地缓存 → 东方财富 → 新浪。
**条件冻结**：本脚本只做结算，不改任何模型阈值；条件更新须在结算结果出来后单独进行。
"""
import os
import sys
import re
import json
import statistics
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
# 台账路径已在工具箱内用 __file__ 固定，这里仅为兼容旧推荐记录设置 STATE_DIR
os.environ.setdefault("STATE_DIR", os.path.join(_HERE, "lingpei", "state"))
sys.path.insert(0, _HERE)
import 领湃工具箱 as tb
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "common"))
try:
    import adapter as _ad
except Exception:
    _ad = None

HORIZONS = (3, 5, 10)
# 本地缓存仅在本轮验证时通过 LINGPEI_KCACHE 显式启用；云端默认走在线行情，避免误读旧缓存。
KCACHE = os.environ.get("LINGPEI_KCACHE", "")


def _sina_hist(code, count=220):
    """新浪日线兜底，返回 [{d,o,c,h,l,v}]（成交量股数转手）。"""
    import requests
    secid = ("sh" + code) if code.startswith("6") else (
        ("bj" + code) if code.startswith(("4", "8", "9")) else ("sz" + code))
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
           "?symbol=" + secid + "&scale=240&ma=no&datalen=" + str(count))
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        m = re.search(r"\((\[.*\])\)", r.text, re.S)
        if m:
            bars = [{"d": str(d["day"]), "o": float(d["open"]), "c": float(d["close"]),
                     "h": float(d["high"]), "l": float(d["low"]),
                     "v": float(d["volume"]) / 100.0} for d in json.loads(m.group(1))]
            return bars or None
    except Exception:
        pass
    return None


def load_bars(code):
    """返回 ([{d,o,c,h,l,v}], 数据源)，按日期升序；正式流水线不联网。"""
    if _ad and _ad.ready():
        bars = _ad.kline_for(code, "lingpei", count=220)
        if bars and len(bars) >= 65:
            return bars, "market_daily"
        return None, "market_daily_miss"
    p = os.path.join(KCACHE, code + ".json") if KCACHE else ""
    if p and os.path.exists(p):
        try:
            bars = json.load(open(p))
            if len(bars) >= 65:
                return bars, "cache"
        except Exception:
            pass
    bars = tb.em_hist(code, 220)
    if bars and len(bars) >= 65:
        return bars, "em"
    bars = _sina_hist(code, 220)
    if bars and len(bars) >= 65:
        return bars, "sina"
    return None, "fail"


def settle_one(s, bars):
    """回填单只样本 T+3/T+5/T+10，返回本次新结算的档位列表。"""
    dates = [b["d"] for b in bars]
    d0 = s.get("date")
    if d0 not in dates:
        return []
    i = dates.index(d0)
    base = bars[i]["c"]
    if base <= 0:
        return []
    fresh = []
    for h in HORIZONS:
        key = "t%d" % h
        t = s.get(key) or {}
        if t.get("done"):
            continue
        j = i + h
        if j >= len(bars):
            continue  # 尚未走到第 h 个交易日，留待后续结算
        b = bars[j]
        t.update({"date": b["d"], "close": round(b["c"], 4),
                  "ret": round(b["c"] / base - 1, 4), "done": True})
        s[key] = t
        fresh.append(h)
    return fresh


def summarize(samples):
    out = {}
    for h in HORIZONS:
        key = "t%d" % h
        rets = [s[key]["ret"] for s in samples
                if (s.get(key) or {}).get("done") and s[key].get("ret") is not None]
        out[h] = {
            "n": len(rets),
            "win_rate": round(sum(1 for x in rets if x > 0) / len(rets), 4) if rets else None,
            "avg": round(statistics.mean(rets), 4) if rets else None,
            "median": round(statistics.median(rets), 4) if rets else None,
        }
    return out


def _pct(v):
    return ("%+.2f%%" % (v * 100)) if v is not None else "n/a"


def main():
    today = tb.today_str()
    print("=" * 64)
    print("领湃 B_V3.3  3/5/10 日跟踪结算  %s" % today)
    print("=" * 64)

    track = tb.load_track()
    samples = track.get("samples", [])
    if not samples:
        print("[台账为空] 无可结算样本")
        return

    pending = [s for s in samples
               if any(not (s.get("t%d" % h) or {}).get("done") for h in HORIZONS)]
    print("[台账] 样本 %d 只，待结算 %d 只，模型 %s"
          % (len(samples), len(pending), track.get("model", "B_V3.3")))

    bars_map, src_cnt = {}, {}
    for s in pending:
        code = s["code"]
        if code in bars_map:
            continue
        bars, src = load_bars(code)
        bars_map[code] = bars
        src_cnt[src] = src_cnt.get(src, 0) + 1
    print("[取数] %s" % src_cnt)

    fresh = 0
    for s in pending:
        bars = bars_map.get(s["code"])
        if not bars:
            continue
        fresh += len(settle_one(s, bars))

    tb.save_track(track)
    summary = summarize(samples)
    print("\n[结算汇总] 本次新结算 %d 条" % fresh)
    for h in HORIZONS:
        v = summary[h]
        wr = ("%.1f%%" % (v["win_rate"] * 100)) if v["win_rate"] is not None else "n/a"
        print("  T+%-2d 已结算 %3d 只 | 胜率 %6s | 平均 %8s | 中位 %8s"
              % (h, v["n"], wr, _pct(v["avg"]), _pct(v["median"])))

    # 最新批次明细
    batches = sorted({s.get("batch") or s["date"] for s in samples})
    latest = batches[-1] if batches else None
    latest_rows = [s for s in samples if (s.get("batch") or s["date"]) == latest]
    print("\n[最新批次 %s] %d 只" % (latest, len(latest_rows)))
    for s in latest_rows:
        cells = " ".join("T+%d:%s" % (h, _pct((s.get("t%d" % h) or {}).get("ret"))
                                     if (s.get("t%d" % h) or {}).get("done") else "待结算")
                         for h in HORIZONS)
        print("  %-10s %s  %s" % (s["name"] or s["code"], s["code"], cells))

    # ---- 推送结算简报 ----
    sum_rows = "".join(
        "<tr><td>T+%d</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (h, summary[h]["n"],
           ("%.1f%%" % (summary[h]["win_rate"] * 100)) if summary[h]["win_rate"] is not None else "n/a",
           _pct(summary[h]["avg"]), _pct(summary[h]["median"]))
        for h in HORIZONS)
    det_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (s["name"] or s["code"], s["code"],
           *[(_pct(s["t%d" % h]["ret"]) if (s.get("t%d" % h) or {}).get("done") else "待结算")
             for h in HORIZONS])
        for s in latest_rows)
    msg = ("<h3>领湃 B_V3.3 3/5/10 日跟踪结算 %s</h3>"
           "<p>台账 %d 只，本次新结算 %d 条；<b>条件已冻结，待结算完成后再评估更新</b>。</p>"
           "<table border='1' cellspacing='0' cellpadding='4'>"
           "<tr><th>档位</th><th>已结算</th><th>胜率</th><th>平均收益</th><th>中位收益</th></tr>"
           "%s</table>"
           "<h4>最新批次 %s（%d 只）</h4>"
           "<table border='1' cellspacing='0' cellpadding='4'>"
           "<tr><th>名称</th><th>代码</th><th>T+3</th><th>T+5</th><th>T+10</th></tr>"
           "%s</table>"
           "<p style='color:#888'>口径：提示日之后第 3/5/10 个交易日收盘 / 提示日收盘 − 1；"
           "统一流水线取数：共同历史行情库（严格离线）。</p>"
           ) % (today, len(samples), fresh, sum_rows, latest, len(latest_rows), det_rows)
    # 2026-11-16：默认静默，避免与当日选股清单构成第二条重复推送。
    # 结算进度已合并进 领湃模型_每日.py 的唯一推送；需要完整简报时显式开启。
    if os.environ.get("LINGPEI_SETTLE_PUSH", "") == "1":
        tb.push_once("领湃 B_V3.3 3/5/10结算 %s" % today, msg,
                     template="html", kind="settle")
        print("[推送] 结算简报已按开关单独发送")
    else:
        print("[推送] 默认静默（每天仅一份领湃推送）；"
              "需完整结算简报可设 LINGPEI_SETTLE_PUSH=1")
    print("\n[完成] %s" % today)


if __name__ == "__main__":
    main()
