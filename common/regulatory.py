#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regulatory.py —— 条件⑤「近一年监管立案 / 涉嫌违法排除集」（全局强制）
========================================================================
用户口径：
  最近一年内被监管机构立案调查，或公告明示涉嫌违法的股票，即使未被 ST，
  也必须在共同前置层物理剔除，任何下游模型都不可选到。

判定依据（标题命中，宁缺勿滥）：
  · 立案调查 / 立案告知书 / 立案通知书 / 被立案 / 立案侦查
  · 证监会（含派出机构）立案
  · 涉嫌…违法 / 涉嫌…违规
  澄清、否认类公告（澄清/不实/否认/不存在/未被立案）不计入。

滚动窗口：
  记录按公告日期入库，只保留最近 365 天；每次读取和构建都会按当日重新
  计算窗口，超期自动失效（与立信「命中永不释放」不同）。

数据通道：
  东财公告批量接口（与 lixin.py 的 B 通道同源），按日期区间分页拉取全市场
  公告，标题过滤后直接入集，不再逐篇请求正文，单日增量通常数秒完成。
  接口异常/被限流时保留历史记录不释放（fail-safe），并记录 scan_empty 标记
  供前置层告警，避免"扫不到=全部放行"。

产物：common/state/regulatory_exclude.json
    { "codes": [...],
      "records": {code: {"date": "YYYY-MM-DD", "title": "...", "art_code": "..."}},
      "window_days": 365, "updated_at": ..., "stats": {...} }

用法：
  python3 common/regulatory.py                # 日常增量（最近 10 天）
  python3 common/regulatory.py --full         # 首次建库：滚动 365 天基线
  python3 common/regulatory.py --days 30      # 自定义增量窗口
"""
import os
import re
import sys
import json
import datetime
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common_lib as cm
import lixin as lx

REG_JSON = cm.REGULATORY_JSON
WINDOW_DAYS = 365
INCREMENT_DAYS = 10
SEGMENT_DAYS = 7          # 单段约 9 千条公告，远低于接口 5 万 total_hits 上限

# 立案 / 公司涉嫌违法：标题命中即剔除
# 「涉嫌违法」限定在「公司/本公司」主体，避免股东个人违规减持等无关公告误伤
RE_REG_HIT = re.compile(
    r"立案(调查|告知|通知|决定|侦查)|被立案"
    r"|证监会.{0,20}立案"
    r"|(公司|本公司).{0,12}涉嫌.{0,10}违法")
# 澄清/否认/非本公司事项类公告不计入（避免误伤上市公司）
RE_REG_NOISE = re.compile(
    r"澄清|不实|否认|不存在|未被立案|未涉嫌|撤销立案"
    r"|非本公司事项|非公司事项|与公司无关")


def _today():
    return datetime.datetime.now(cm.BJT).date()


def _cutoff(today=None):
    return ((today or _today()) - datetime.timedelta(days=WINDOW_DAYS)).isoformat()


def _prune(records, today=None):
    """丢弃超期记录，并按当前规则重新校验历史命中（规则收紧后自动清理误判）。"""
    cut = _cutoff(today)
    out = {}
    for c, r in records.items():
        dt = r.get("date") or ""
        if dt and dt < cut:
            continue
        title = r.get("title") or ""
        if title and not is_regulatory_title(title):
            continue
        out[c] = r
    return out


def load_records(today=None):
    if not os.path.exists(REG_JSON):
        return {}
    try:
        with open(REG_JSON, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return dict(d.get("records") or {})
    except Exception:
        return {}


def load_regulatory_exclude(today=None):
    """返回当前仍在一年窗口内的监管排除代码集合。"""
    if not os.path.exists(REG_JSON):
        return set()
    try:
        with open(REG_JSON, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return set()
    records = d.get("records") or {}
    if not records:                     # 兼容仅存 codes 的旧文件
        return set(d.get("codes") or [])
    return set(_prune(records, today))


def is_regulatory_title(title):
    t = str(title or "")
    if not t or RE_REG_NOISE.search(t):
        return False
    return bool(RE_REG_HIT.search(t))


def scan_window(begin, end, verbose=True):
    """扫描日期区间全市场公告，返回 ({code: record}, stats)。"""
    hits = {}
    notices = 0
    for a in lx._ann_pages(begin, end):
        notices += 1
        title = str(a.get("title", ""))
        if not is_regulatory_title(title):
            continue
        date = str(a.get("notice_date", ""))[:10]
        art = str(a.get("art_code", ""))
        for c in (a.get("codes") or []):
            code = str(c.get("stock_code") or "").zfill(6)
            if len(code) != 6 or not code.isdigit() or code == "000000":
                continue
            if not code.startswith(("60", "68", "00", "30", "8", "4")):
                continue
            old = hits.get(code)
            if not old or date > old.get("date", ""):
                hits[code] = {"date": date, "title": title, "art_code": art}
    stats = {"notices": notices, "hits": len(hits),
             "window": "%s~%s" % (begin, end)}
    if verbose:
        print("[regulatory] 公告窗口 %s：扫描 %d 条 / 命中 %d 只"
              % (stats["window"], notices, len(hits)))
    return hits, stats


def scan_incremental(days=INCREMENT_DAYS, verbose=True):
    end = _today()
    begin = end - datetime.timedelta(days=days)
    return scan_window(begin.isoformat(), end.isoformat(), verbose)


def scan_full(verbose=True):
    """滚动一年基线：按 7 天分段并发扫描。"""
    end = _today()
    begin = end - datetime.timedelta(days=WINDOW_DAYS)
    segs = []
    cur = begin
    while cur < end:
        nxt = min(cur + datetime.timedelta(days=SEGMENT_DAYS), end)
        segs.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt

    hits, agg = {}, {"notices": 0, "hits": 0, "segments": len(segs)}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for part, st in ex.map(
                lambda s: scan_window(s[0], s[1], verbose=False), segs):
            agg["notices"] += st.get("notices", 0)
            for code, rec in part.items():
                old = hits.get(code)
                if not old or rec.get("date", "") > old.get("date", ""):
                    hits[code] = rec
    agg["hits"] = len(hits)
    agg["window"] = "%s~%s" % (begin.isoformat(), end.isoformat())
    if verbose:
        print("[regulatory] 一年基线 %d 段完成：扫描 %d 条 / 命中 %d 只"
              % (len(segs), agg["notices"], len(hits)))
    return hits, agg


def build(codes=None, mode="incremental", days=None, verbose=True):
    """生成排除集并落盘。返回 (codes_set, stats)。"""
    old = _prune(load_records())
    if mode == "full":
        hits, stats = scan_full(verbose)
    else:
        hits, stats = scan_incremental(days or INCREMENT_DAYS, verbose)

    # 新命中覆盖旧记录（取较新日期），并做滚动窗口裁剪
    merged = dict(old)
    for code, rec in hits.items():
        if not old.get(code) or rec.get("date", "") > old[code].get("date", ""):
            merged[code] = rec
    merged = _prune(merged)

    payload = {
        "codes": sorted(merged),
        "records": {c: merged[c] for c in sorted(merged)},
        "window_days": WINDOW_DAYS,
        "updated_at": datetime.datetime.now(cm.BJT).isoformat(timespec="seconds"),
        "stats": {
            "universe": len(codes or []),
            "mode": mode,
            "scanned": stats.get("notices", 0),
            "new_hits": len(hits),
            "scan_empty": stats.get("notices", 0) == 0,
            "window": stats.get("window", ""),
            "total_excluded": len(merged),
        },
    }
    tmp = REG_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, REG_JSON)

    if verbose:
        print("[regulatory] 排除集合计 %d 只（近 %d 天滚动）→ %s"
              % (len(merged), WINDOW_DAYS, REG_JSON))
    return set(merged), payload["stats"]


def main():
    args = sys.argv[1:]
    mode = "full" if "--full" in args else "incremental"
    days = INCREMENT_DAYS
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except (IndexError, ValueError):
            pass
    print("=" * 64)
    print("共同条件⑤ 近一年监管立案/涉嫌违法排除集  mode=%s" % mode)
    print("=" * 64)
    u = cm.get_universe(verbose=False, apply_lixin=False, apply_regulatory=False)
    _, stats = build([x["code"] for x in u], mode=mode, days=days)
    if stats.get("scan_empty"):
        print("[regulatory][WARN] 本次公告扫描 0 条，接口可能被限流；"
              "已保留历史记录，未释放任何标的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
