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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common_lib as cm

LIXIN_JSON = cm.LIXIN_JSON

# 立信体系（严格命中：排除「立信中联」等近似名）
RE_FIRM_LIXIN = re.compile(r"(?<![中北京])立信(?!中联)")
RE_ANN_TITLE = re.compile(r"会计师事务所|续聘|改聘|聘任|审计机构|年报审计")
RE_ANN_BODY_LIXIN = re.compile(r"(?<![中北京])立信(?!中联)\s*会计师事务所")
# 噪声词：避免把事务所沿革介绍 / 前任机构 / 无关词误判
NOISE = ("本次", "公司", "选聘", "拟", "关于", "前任", "任同一", "为由", "经对")

EM_F10 = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EM_ANN_LIST = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EM_ANN_BODY = "https://np-cnotice-stock.eastmoney.com/api/content/ann"

ANN_WORKERS = 16
ANN_MAX_PER_STOCK = 6          # 每只最多看 6 篇候选公告正文，控耗时
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


def build(codes, do_ann=True, verbose=True):
    """生成排除集。返回 (codes_set, stats)。"""
    old_codes, old_reasons = load_existing()
    reasons = dict(old_reasons)

    # A 通道
    firms = f10_firms(codes, verbose=verbose)
    f10_hit = set()
    for code, firm in firms.items():
        if _firm_is_lixin(firm):
            f10_hit.add(code)
            reasons[code] = "f10:%s" % firm
    if verbose:
        print("[lixin] A 通道(F10 年报审计机构=立信) 命中 %d 只" % len(f10_hit))

    # B 通道：仅对 A 未命中的股票扫 2026 公告（控制耗时）
    ann_hit_set = set()
    scanned = 0
    if do_ann:
        targets = [c for c in codes if c not in f10_hit]
        if verbose:
            print("[lixin] B 通道(2026 续聘/改聘公告) 待扫 %d 只 ..." % len(targets))

        def work(c):
            hit, why = ann_hit(c)
            return c, hit, why

        with ThreadPoolExecutor(max_workers=ANN_WORKERS) as ex:
            futs = [ex.submit(work, c) for c in targets]
            for fut in as_completed(futs):
                try:
                    c, hit, why = fut.result()
                    if hit:
                        ann_hit_set.add(c)
                        reasons[c] = "ann:%s" % why
                except Exception:
                    pass
                with _lock:
                    scanned += 1
                if verbose and scanned % 500 == 0:
                    print("[lixin] B 通道进度 %d/%d，命中 %d"
                          % (scanned, len(targets), len(ann_hit_set)))
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
            "ann_scanned": scanned,
            "ann_hit": len(ann_hit_set),
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
    print("=" * 64)
    print("共同条件② 立信审计排除集  do_ann=%s" % do_ann)
    print("=" * 64)
    # 立信判定覆盖「全市场」：取仅 ST/次新剔除后的全量代码（不应用立信剔除，
    # 否则生成排除集时又引用排除集本身，形成循环依赖）。
    u = cm.get_universe(verbose=False, apply_lixin=False)
    codes = [x["code"] for x in u]
    build(codes, do_ann=do_ann)
    return 0


if __name__ == "__main__":
    sys.exit(main())
