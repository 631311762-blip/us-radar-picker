#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lixin.py —— 条件②「立信审计排除集」生成器（全局强制，供所有模型共用）
========================================================================
用户口径（二者满足其一即剔除，逻辑「或」）：
  A. 2025 年报审计机构 = 立信会计师事务所（含其特殊普通合伙、分所表述）；
  B. 2026 年发布「续聘 / 改聘 / 聘任会计师事务所」公告，且公告指向立信。

明确排除：「立信中联会计师事务所」是另一家独立事务所，不在剔除范围。

数据通道：
  A 通道 —— 东财 F10 RPT_F10_BASIC_ORGINFO.ACCOUNTFIRM_NAME（当前/最近年报审计机构），
            批量 40 只/次，覆盖绝大多数；幂等。
  B 通道 —— 东财公告接口 np-anotice-stock，扫标题含「续聘/改聘/聘任/审计机构/
            年报审计」的公告正文，命中「立信会计师事务所」即入集，记录命中依据。
            为控制耗时，B 通道只针对「A 通道未命中」的股票做增量扫描（并发）。

产物：common/state/lixin_exclude.json
    { "codes": [...], "reasons": {code: "f10"|"ann"|"f10+ann"},
      "updated_at": ..., "stats": {...} }

用法：
  python3 common/lixin.py                 # A+B 全量（推荐，首次/每周）
  python3 common/lixin.py --f10-only     # 仅 A 通道（快，日常）
  python3 common/lixin.py --no-ann       # 同 --f10-only
"""
import os
import sys
import re
import json
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common_lib as cm

LIXIN_JSON = cm.LIXIN_JSON

# 立信体系（严格命中：排除「立信中联」等近似名）
RE_FIRM_LIXIN = re.compile(r"(?<![中北京])立信(?!中联)")
# 旧版 RE_ANN_TITLE 过宽（"聘任高级管理人员"也命中），正文请求量大且易触发限流。
# 收紧为「审计机构类词 + 聘任动作词」同时出现，精准定位改聘/续聘公告。
RE_ANN_TITLE = re.compile(r"会计师事务所|续聘|改聘|聘任|审计机构|年报审计")
RE_ANN_AUDIT = re.compile(r"会计师事务所|审计机构|年报审计|审计师|财务审计")
RE_ANN_ACTION = re.compile(r"续聘|改聘|聘任|变更|选聘|聘用|聘请|更换|改选")
RE_ANN_BODY_LIXIN = re.compile(r"(?<![中北京])立信(?!中联)\s*会计师事务所")
# 噪声词：避免把事务所沿革介绍 / 前任机构 / 无关词误判
NOISE = ("本次", "公司", "选聘", "拟", "关于", "前任", "任同一", "为由", "经对")

EM_F10 = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EM_ANN_LIST = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EM_ANN_BODY = "https://np-cnotice-stock.eastmoney.com/api/content/ann"

ANN_WORKERS = 16
ANN_MAX_PER_STOCK = 6          # 每只最多看 6 篇候选公告正文，控耗时
ANN_PAGE_SIZE = 100            # 公告批量接口每页条数
ANN_PAGE_MAX = 400             # 单段最多翻页保护（防接口异常死循环）
ANN_SEGMENT_DAYS = 30          # 全年基线按 30 天分段，规避 total_hits 5 万上限截断
ANN_INCREMENT_DAYS = 10        # 日常增量窗口（覆盖周末+节假日+公告补录）
_lock = threading.Lock()


def secucode(code):
    c = str(code).zfill(6)
    if c.startswith("6"):
        return c + ".SH"
    if c.startswith(("8", "4", "92")):
        return c + ".BJ"
    return c + ".SZ"


# ------------------------------------------------------------------ A 通道：F10 年报审计机构
def _f10_chunk(secucodes):
    inlist = ",".join('"%s"' % c for c in secucodes)
    j = cm.http_json(EM_F10, params={
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": "SECUCODE,SECURITY_NAME_ABBR,EM2016,ACCOUNTFIRM_NAME",
        "filter": "(SECUCODE in (%s))" % inlist,
        "pageNumber": 1, "pageSize": 50, "source": "HSF10", "client": "PC",
    }, timeout=15, retry=1)
    if isinstance(j, list) and j:
        j = j[0]
    try:
        return j["result"]["data"] or []
    except Exception:
        return []


def f10_firms(codes, verbose=True):
    """返回 {code: firm_name}。"""
    out = {}
    secus = [secucode(c) for c in codes]
    chunks = [secus[i:i + 40] for i in range(0, len(secus), 40)]
    for idx, chunk in enumerate(chunks):
        rows = _f10_chunk(chunk)
        if not rows and len(chunk) > 10:      # 大块失败则拆小块重试
            for j in range(0, len(chunk), 10):
                rows += _f10_chunk(chunk[j:j + 10])
                time.sleep(0.15)
        for row in rows:
            code = str(row.get("SECUCODE", "")).split(".")[0]
            out[code] = (row.get("ACCOUNTFIRM_NAME") or "").strip()
        if verbose and (idx + 1) % 30 == 0:
            print("[lixin] F10 进度 %d/%d 批" % (idx + 1, len(chunks)))
        time.sleep(0.15)
    return out


def _firm_is_lixin(firm):
    if not firm:
        return False
    if "立信中联" in firm:          # 独立事务所，不剔除
        return False
    return bool(RE_FIRM_LIXIN.search(firm))


# ------------------------------------------------------------------ B 通道：2026 续聘/改聘公告
def _ann_list(code):
    out = []
    for pg in (1, 2):
        url = (EM_ANN_LIST + "?sr=-1&page_size=100&page_index=%d&ann_type=A"
               "&client_source=web&stock_list=%s" % (pg, code))
        try:
            d = cm.http_json(url, timeout=15, retry=1)
            out += ((d or {}).get("data") or {}).get("list") or []
        except Exception:
            pass
    return out


def _ann_body(art_code):
    url = (EM_ANN_BODY + "?art_code=%s&client_source=web&page_index=1" % art_code)
    d = cm.http_json(url, timeout=15, retry=1)
    return json.dumps(d, ensure_ascii=False) if d else ""


def ann_hit(code):
    """该股票 2026 年是否有「续聘/改聘为立信」公告。返回 (bool, 依据)。"""
    try:
        lst = _ann_list(code)
        cand = []
        for a in lst:
            title = str(a.get("title", ""))
            # 只看 2026 年公告（26 年续聘/改聘口径），标题命中审计相关词
            notice_date = str(a.get("notice_date", ""))[:4]
            if notice_date and notice_date not in ("2026",):
                continue
            if RE_ANN_TITLE.search(title):
                cand.append(a)
        if not cand:
            return False, ""
        hits = 0
        for a in cand[:ANN_MAX_PER_STOCK]:
            body = _ann_body(str(a.get("art_code", "")))
            if RE_ANN_BODY_LIXIN.search(body):
                hits += 1
        if hits:
            return True, "2026 续聘/改聘公告命中立信（%d 篇）" % hits
        return False, ""
    except Exception:
        return False, ""


# ------------------------------------------------- B 通道（批量·按日期扫描）
def _ann_pages(begin, end):
    """按日期区间分页拉取「全市场公告」，逐条 yield 公告 dict。

    相对旧版逐股扫描（4605 次请求 / 约 8 分钟），这里按日期批量拉全市场
    公告（近 7 天实测 76 页约 10 秒），用 codes[].stock_code 反查股票代码。
    """
    pg = 1
    cap = ANN_PAGE_MAX * ANN_PAGE_SIZE
    while pg <= ANN_PAGE_MAX:
        url = (EM_ANN_LIST + "?sr=-1&page_size=%d&page_index=%d&ann_type=A"
               "&client_source=web&begin_time=%s&end_time=%s"
               % (ANN_PAGE_SIZE, pg, begin, end))
        try:
            d = cm.http_json(url, timeout=15, retry=1)
        except Exception:
            break
        data = (d or {}).get("data") or {}
        lst = data.get("list") or []
        if not lst:
            break
        for a in lst:
            yield a
        total = data.get("total_hits") or 0
        if len(lst) < ANN_PAGE_SIZE or pg * ANN_PAGE_SIZE >= min(total, cap):
            break
        pg += 1


def ann_scan_window(begin, end, verbose=True):
    """扫描一个日期区间的全市场公告：标题过滤 → 反查股票 → 正文判立信。

    返回 (命中代码集合, 依据 dict, 统计 dict)。
    判定口径与旧 ann_hit 完全一致：仅 2026 年公告、标题命中 RE_ANN_TITLE、
    正文命中 RE_ANN_BODY_LIXIN（严格排除「立信中联」）。
    """
    cand = {}          # code -> [art_code]
    title_hits = set()  # 标题即可确认「聘任/改聘立信」的代码（正文限流时兜底）
    n_ann = 0
    for a in _ann_pages(begin, end):
        n_ann += 1
        if str(a.get("notice_date", ""))[:4] != "2026":
            continue
        title = str(a.get("title", ""))
        # 收紧：必须同时含「审计机构类词」与「聘任动作词」，过滤"聘任高管"噪声
        if not (RE_ANN_AUDIT.search(title) and RE_ANN_ACTION.search(title)):
            continue
        art = str(a.get("art_code", ""))
        # 标题降级判定：明确写出「聘任/变更…立信会计师事务所」→ 直接命中。
        # 用户口径为「或」关系（25年立信 OR 26年公告改聘立信），此类标题无论
        # 改聘进/出，25 年多为立信，命中符合口径；且"立信出具的问询回复"标题
        # 无聘任动作词，已被上方正则排除，不会误伤。
        title_lixin = bool(RE_ANN_BODY_LIXIN.search(title))
        for c in (a.get("codes") or []):
            code = str(c.get("stock_code") or "").zfill(6)
            if len(code) != 6 or not code.isdigit() or code == "000000":
                continue
            # 公告可能关联可转债/基金代码（如 11xxxx），只保留 A 股股票前缀
            if not code.startswith(("60", "68", "00", "30", "8", "4")):
                continue
            if title_lixin:
                title_hits.add(code)
            if art:
                slot = cand.setdefault(code, [])
                if len(slot) < ANN_MAX_PER_STOCK:
                    slot.append(art)

    jobs = [(c, art) for c, arts in cand.items() for art in arts]

    def work(item):
        c, art = item
        try:
            body = _ann_body(art)
            # 正文接口被限流时返回空串：视为「未确认」，不误剔除
            return c, bool(body and RE_ANN_BODY_LIXIN.search(body)), bool(body)
        except Exception:
            return c, False, False

    hits = set(title_hits)
    why = {}
    empty_body = 0
    if jobs:
        # 正文并发降到 8，降低触发东财反爬(HTTP 567)的概率
        with ThreadPoolExecutor(max_workers=min(ANN_WORKERS, 8)) as ex:
            for c, ok, got in ex.map(work, jobs):
                if not got:
                    empty_body += 1
                if ok:
                    hits.add(c)
    for c in hits:
        why[c] = "2026 续聘/改聘公告命中立信"
    stats = {"notices": n_ann, "candidates": len(cand),
             "body_checked": len(jobs), "hits": len(hits),
             "title_hits": len(title_hits), "body_empty": empty_body,
             "window": "%s~%s" % (begin, end)}
    if verbose:
        print("[lixin] 公告窗口 %s：扫描 %d 条 / 候选 %d 只 / 正文 %d 篇 / 命中 %d 只"
              "（标题直判 %d，正文空响应 %d）"
              % (stats["window"], n_ann, len(cand), len(jobs), len(hits),
                 len(title_hits), empty_body))
    return hits, why, stats


def ann_scan_incremental(days=ANN_INCREMENT_DAYS, verbose=True):
    """日常增量：只扫最近 N 天新发布的公告。历史命中已由并集合并永久保留。"""
    end = datetime.now(cm.BJT).date()
    begin = end - timedelta(days=days)
    return ann_scan_window(begin.strftime("%Y-%m-%d"),
                           end.strftime("%Y-%m-%d"), verbose)


def ann_scan_full(verbose=True):
    """全年基线：按 30 天分段扫描 2026-01-01 至今（规避 total_hits 5 万截断）。

    分段之间并发执行（各段翻页互不依赖），把全年串行 ~10 分钟压到约 1-2 分钟。
    仅用于首次建库 / 手动全量校准；日常由 ann_scan_incremental 增量维护。
    """
    end = datetime.now(cm.BJT).date()
    cur = datetime(2026, 1, 1).date()
    segs = []
    while cur <= end:
        seg_end = min(cur + timedelta(days=ANN_SEGMENT_DAYS - 1), end)
        segs.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = seg_end + timedelta(days=1)

    def run_seg(seg):
        return ann_scan_window(seg[0], seg[1], verbose=False)

    all_hits, all_why = set(), {}
    agg = {"notices": 0, "candidates": 0, "body_checked": 0, "hits": 0,
           "segments": len(segs),
           "window": "2026-01-01~%s" % end.strftime("%Y-%m-%d")}
    # 并发分段数适中，避免对公告接口过度并发触发限流
    with ThreadPoolExecutor(max_workers=4) as ex:
        for h, w, s in ex.map(run_seg, segs):
            all_hits |= h
            all_why.update(w)
            for k in ("notices", "candidates", "body_checked"):
                agg[k] += s.get(k, 0)
    agg["hits"] = len(all_hits)
    if verbose:
        print("[lixin] 全年基线 %d 段并发完成：扫描 %d 条 / 候选 %d / 命中 %d 只"
              % (len(segs), agg["notices"], agg["candidates"], agg["hits"]))
    return all_hits, all_why, agg


# ------------------------------------------------------------------ 汇总
def load_existing():
    """读取已有排除集（保留历史命中，做并集合并，避免某天接口抖动导致误放出）。"""
    if not os.path.exists(LIXIN_JSON):
        return set(), {}
    try:
        d = json.load(open(LIXIN_JSON, encoding="utf-8"))
        return set(d.get("codes", [])), (d.get("reasons") or {})
    except Exception:
        return set(), {}


def build(codes, do_ann=True, ann_mode="incremental", verbose=True,
          skip_f10=False):
    """生成排除集。返回 (codes_set, stats)。

    ann_mode:
      "incremental" —— 默认，按日期批量扫最近 N 天全市场公告（约 30 秒）；
      "full"        —— 2026 全年分段并发基线（首次建库 / 手动校准用）。
    do_ann=False 等价于旧 --f10-only（完全跳过 B 通道，快速验证用）。
    skip_f10=True 用于只补 B 通道基线（F10 结果已在库，省 4 分钟）。

    无论哪种模式，结果都与历史排除集做「并集合并」，命中永不自动释放，
    因此增量窗口不会漏掉历史上已命中的股票，也不用每天全量重扫。
    """
    old_codes, old_reasons = load_existing()
    reasons = dict(old_reasons)

    # A 通道
    f10_hit = set()
    if skip_f10:
        # 复用已有排除集中 f10: 前缀的历史命中（基线场景：F10 已跑并入库）
        for c in old_codes:
            if str(old_reasons.get(c, "")).startswith("f10:"):
                f10_hit.add(c)
        if verbose:
            print("[lixin] skip_f10：复用已入库 F10 命中 %d 只" % len(f10_hit))
    else:
        firms = f10_firms(codes, verbose=verbose)
        for code, firm in firms.items():
            if _firm_is_lixin(firm):
                f10_hit.add(code)
                reasons[code] = "f10:%s" % firm
        if verbose:
            print("[lixin] A 通道(F10 年报审计机构=立信) 命中 %d 只" % len(f10_hit))

    # B 通道：按日期批量扫全市场公告（不再是逐股 4605 次请求）
    ann_hit_set = set()
    ann_stats = {}
    if do_ann:
        if ann_mode == "full":
            ann_hit_set, why_map, ann_stats = ann_scan_full(verbose)
        else:
            ann_hit_set, why_map, ann_stats = ann_scan_incremental(verbose=verbose)
        for c, w in why_map.items():
            reasons[c] = "ann:%s" % w
        # 只保留样本池内的代码（排除集服务于下游选股，池外代码无意义）
        pool = set(codes)
        ann_hit_set &= pool
    else:
        if verbose:
            print("[lixin] --f10-only：跳过 B 通道公告扫描。")

    # 并集合并（保留历史，防止接口抖动误放）
    merged = set(old_codes) | f10_hit | ann_hit_set
    for c in merged:
        reasons.setdefault(c, "history")

    payload = {
        "codes": sorted(merged),
        "reasons": {c: reasons.get(c, "") for c in sorted(merged)},
        "updated_at": datetime.now(cm.BJT).isoformat(timespec="seconds"),
        "stats": {
            "universe": len(codes),
            "f10_hit": len(f10_hit),
            "ann_mode": ann_mode if do_ann else "off",
            "ann_scanned": ann_stats.get("notices", 0),
            "ann_candidates": ann_stats.get("candidates", 0),
            "ann_hit": len(ann_hit_set),
            "ann_window": ann_stats.get("window", ""),
            "ann_detail": ann_stats,
            "total_excluded": len(merged),
            "do_ann": bool(do_ann),
        },
    }
    with open(LIXIN_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    if verbose:
        print("[lixin] 排除集合计 %d 只 → %s" % (len(merged), LIXIN_JSON))
    return merged, payload["stats"]


def main():
    args = sys.argv[1:]
    do_ann = not ("--f10-only" in args or "--no-ann" in args)
    ann_mode = "full" if "--ann-full" in args else "incremental"
    print("=" * 64)
    print("共同条件② 立信审计排除集  do_ann=%s  ann_mode=%s" % (do_ann, ann_mode))
    print("=" * 64)
    # 立信判定覆盖「全市场」：取仅 ST/次新剔除后的全量代码（不应用立信剔除，
    # 否则生成排除集时又引用排除集本身，形成循环依赖）。
    u = cm.get_universe(verbose=False, apply_lixin=False)
    codes = [x["code"] for x in u]
    baseline = "--ann-baseline" in args
    build(codes, do_ann=do_ann,
          ann_mode="full" if (ann_mode == "full" or baseline) else "incremental",
          skip_f10=baseline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
