#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unlock_risk.py —— 大额限售解禁排除集（每周六识别，全局强制）
========================================================================
用户口径（2026-11-25，已更正）：
  「近一个月内有大解禁的股票不考虑」——**只看解禁，不含低位条件**。

判定：
  · 窗口：过去 WINDOW_DAYS 天 ~ 未来 WINDOW_DAYS 天（自然日，默认前后各 30 天）
    内存在解禁事件；
  · 大解禁：该股票在窗口内任一解禁批次「解禁股数 / 总股本 ≥ BIG_RATIO」
    （东财 RPT_LIFT_STAGE.TOTALSHARES_RATIO，默认 10%）；
  满足即整体剔除（物理剔除，任何下游模型都选不到）。

数据通道：
  东财解禁报表 RPT_LIFT_STAGE（datacenter-web），按日期区间分页批量拉取全市场
  解禁批次，无需逐股请求。窗口内约 2~3 千条、150~200 只股票，数秒完成。

调度：
  仅周六（周刷新）重建；周一~五复用上周六产出的 unlock_exclude.json。
  读取时按当天重新计算窗口，解禁日滑出 [today-Δ, today+Δ] 的批次自动失效，
  文件即使一周未更新也不会继续误剔除。

产物：common/state/unlock_exclude.json
    { "codes": [...],
      "records": {code: {"name": "...", "batches": [{date, ratio, cap_wan}],
                          "max_ratio": 0.35, "unlock_cap_wan": 12345.6}},
      "window_days": 30, "big_ratio": 0.10, "updated_at": ..., "stats": {...} }

用法：
  python3 common/unlock_risk.py               # 重建（前后各 30 天，≥10%）
  python3 common/unlock_risk.py --days 45     # 自定义窗口（前后各 N 天）
  python3 common/unlock_risk.py --ratio 0.05  # 自定义大解禁比例
  python3 common/unlock_risk.py --dry-run     # 只打印不落盘
"""
import os
import sys
import json
import datetime
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import common_lib as cm
    BJT = cm.BJT
    STATE_DIR = cm.STATE_DIR
    UNLOCK_JSON = cm.UNLOCK_JSON
except Exception:                       # 独立运行兜底
    BJT = datetime.timezone(datetime.timedelta(hours=8))
    STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    UNLOCK_JSON = os.path.join(STATE_DIR, "unlock_exclude.json")

API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_LIFT_STAGE"
WINDOW_DAYS = int(os.environ.get("UNLOCK_WINDOW_DAYS", "30"))
BIG_RATIO = float(os.environ.get("UNLOCK_BIG_RATIO", "0.10"))
PAGE_SIZE = 500
MAX_PAGES = 40
TIMEOUT = 25
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
A_SHARE_PREFIX = ("60", "68", "00", "30", "8", "4")


def _today():
    return datetime.datetime.now(BJT).date()


def _parse(d):
    try:
        return datetime.date.fromisoformat(str(d)[:10])
    except Exception:
        return None


def _fnum(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _request(params, retries=2):
    url = API_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:          # 网络/解析异常重试
            last = e
    raise RuntimeError("东财解禁接口失败: %s" % last)


def fetch_unlocks(begin, end, verbose=False):
    """拉取 [begin, end] 区间全市场解禁批次。"""
    rows = []
    for page in range(1, MAX_PAGES + 1):
        j = _request({
            "reportName": REPORT_NAME,
            "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,"
                        "CURRENT_FREE_SHARES,LIFT_MARKET_CAP,TOTALSHARES_RATIO,"
                        "FREE_SHARES_TYPE"),
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "sortColumns": "FREE_DATE",
            "sortTypes": "1",
            "filter": "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (begin, end),
        })
        result = j.get("result") or {}
        data = result.get("data") or []
        rows.extend(data)
        pages = int(result.get("pages") or 0)
        if verbose:
            print("[unlock] 第 %d/%d 页：%d 条" % (page, pages, len(data)))
        if not data or page >= pages:
            break
    return rows


def aggregate(rows):
    """按股票聚合解禁批次，返回 {code: {name, batches, max_ratio, unlock_cap_wan}}。"""
    agg = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "").zfill(6)
        if len(code) != 6 or not code.isdigit() or code == "000000":
            continue
        if not code.startswith(A_SHARE_PREFIX):
            continue
        date = str(r.get("FREE_DATE") or "")[:10]
        if not _parse(date):
            continue
        ratio = _fnum(r.get("TOTALSHARES_RATIO"))
        cap = _fnum(r.get("LIFT_MARKET_CAP"))          # 万元
        a = agg.setdefault(code, {
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "batches": [], "max_ratio": 0.0, "unlock_cap_wan": 0.0,
        })
        a["batches"].append({"date": date, "ratio": round(ratio, 6),
                             "cap_wan": round(cap, 2)})
        a["max_ratio"] = max(a["max_ratio"], ratio)
        a["unlock_cap_wan"] += cap
    for a in agg.values():
        a["batches"].sort(key=lambda b: b["date"])
        a["unlock_cap_wan"] = round(a["unlock_cap_wan"], 2)
    return agg


def build(days=None, ratio=None, verbose=True, path=None, dry_run=False):
    """重建大解禁排除集并落盘。返回 (codes_set, stats)。"""
    today = _today()
    window = int(days if days is not None else WINDOW_DAYS)
    big = float(ratio if ratio is not None else BIG_RATIO)
    begin = (today - datetime.timedelta(days=window)).isoformat()
    end = (today + datetime.timedelta(days=window)).isoformat()

    rows = fetch_unlocks(begin, end, verbose=verbose)
    agg = aggregate(rows)
    excluded = {}
    for code, a in agg.items():
        if a["max_ratio"] < big:
            continue
        excluded[code] = {
            "name": a["name"],
            "batches": a["batches"],
            "max_ratio": round(a["max_ratio"], 6),
            "unlock_cap_wan": a["unlock_cap_wan"],
        }

    stats = {
        "fetched": len(rows),
        "stocks": len(agg),
        "big_unlock": len(excluded),
        "total_excluded": len(excluded),
        "scan_empty": len(rows) == 0,
        "window": "%s~%s" % (begin, end),
        "window_days": window,
        "big_ratio": big,
    }
    payload = {
        "codes": sorted(excluded),
        "records": {c: excluded[c] for c in sorted(excluded)},
        "window_days": window,
        "big_ratio": big,
        "updated_at": datetime.datetime.now(BJT).isoformat(timespec="seconds"),
        "source": "eastmoney:" + REPORT_NAME,
        "stats": stats,
    }
    if verbose:
        print("[unlock] 大解禁排除 %d 只（窗口 %s~%s，占总股本≥%.0f%%）%s"
              % (len(excluded), begin, end, big * 100,
                 "（dry-run 未落盘）" if dry_run else "→ %s" % (path or UNLOCK_JSON)))
    if not dry_run:
        target = path or UNLOCK_JSON
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    return set(excluded), stats


def load_records(path=None):
    target = path or UNLOCK_JSON
    if not os.path.exists(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return dict(d.get("records") or {})
    except Exception:
        return {}


def load_unlock_exclude(today=None, path=None):
    """返回当前窗口内仍属大解禁的代码集合（按当日重算窗口）。"""
    target = path or UNLOCK_JSON
    if not os.path.exists(target):
        return set()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return set()
    records = d.get("records") or {}
    if not records:                      # 兼容仅存 codes 的旧文件
        return set(d.get("codes") or [])
    day = today or _today()
    win = int(d.get("window_days") or WINDOW_DAYS)
    big = float(d.get("big_ratio") or BIG_RATIO)
    lo = (day - datetime.timedelta(days=win)).isoformat()
    hi = (day + datetime.timedelta(days=win)).isoformat()
    out = set()
    for code, rec in records.items():
        for b in (rec.get("batches") or []):
            bd = str(b.get("date") or "")[:10]
            if bd and lo <= bd <= hi and _fnum(b.get("ratio")) >= big:
                out.add(code)
                break
    return out


def main():
    days = WINDOW_DAYS
    ratio = BIG_RATIO
    args = sys.argv[1:]
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except (IndexError, ValueError):
            pass
    if "--ratio" in args:
        try:
            ratio = float(args[args.index("--ratio") + 1])
        except (IndexError, ValueError):
            pass
    print("=" * 64)
    print("大额限售解禁排除集  window=±%d天  ratio≥%.0f%%" % (days, ratio * 100))
    print("=" * 64)
    _, stats = build(days=days, ratio=ratio, verbose=True,
                     dry_run="--dry-run" in args)
    print("[unlock] stats=%s" % json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
