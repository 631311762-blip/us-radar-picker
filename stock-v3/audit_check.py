# -*- coding: utf-8 -*-
"""审计机构排查：识别年报审计机构 / 续聘改聘公告中是否出现"立信会计师事务所"。
输出 audit_result.json，供每日买点扫描.py 做排除。
"""
import json, re, subprocess, sys, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

# 2026-11-02：westock-data 本地脚本被 WorkBuddy 更新移除，公告改走东财公开接口。
# 判定逻辑（标题关键词 + 正文强语境模式）完全不变。
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
OUT = "audit_result.json"


def _get(url, timeout=25, retry=2):
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            if i == retry:
                return None
            time.sleep(0.8)
    return None

# 命中标题的关键词（覆盖续聘/改聘/变更/审计机构相关公告）
TITLE_KW = ["会计师事务所", "审计机构", "续聘", "改聘", "变更会计", "聘任会计"]

FIRM = r"([\u4e00-\u9fa5]{2,10}会计师事务所(?:\s*[（(][^）)]*[）)])*)"
# 强语境模式：只有在"聘任/续聘/改聘/变更/审计机构为"语境下取到的事务所名称才作数，
# 避免把事务所历史沿革介绍、或"立信中联"等近似名误判进来。
STRONG_PAT = [
    re.compile(r"拟聘任的?会计师事务所名称[：: ]*" + FIRM),
    re.compile(r"(?:拟)?续聘\s*" + FIRM),
    re.compile(r"(?:拟)?改聘\s*" + FIRM),
    re.compile(r"(?:拟)?(?:聘|聘任|聘请)\s*" + FIRM),
    re.compile(r"变更[^。；\n]{0,25}会计师事务所为?\s*" + FIRM),
    re.compile(r"审计机构[^\n。；]{0,12}?" + FIRM),
    re.compile(FIRM + r"[^。；\n]{0,25}?(?:为|担任)[^。；\n]{0,12}?(?:20\d\d\s*年(?:度)?)(?:财务报告|年报|审计)"),
    re.compile(r"年审会计师事务所[^。；\n]{0,10}" + FIRM),
    re.compile(r"对\s*" + FIRM + r"[^。；\n]{0,30}?(?:20\d\d)\s*年度"),
]
# 直接命中"立信会计师事务所"且非沿革介绍，作为兜底
LIXIN_STRICT = re.compile(r"(?<![中北京])立信(?!中联)\s*会计师事务所")
NOISE = re.compile(r"创建于|泰斗|合并|改制|前身|历史沿革|1927年|成立之初|由我国会计")


def run(args, timeout=120):
    """兼容保留：旧 westock 调用入口已废弃，一律返回 None。"""
    return None


def flatten(obj):
    """把 sections / data 嵌套结构拉平成列表"""
    if obj is None:
        return []
    if isinstance(obj, list):
        out = []
        for x in obj:
            out.extend(flatten(x))
        return out
    if isinstance(obj, dict):
        for k in ("data", "sections", "items"):
            if k in obj:
                return flatten(obj[k])
        return [obj]
    return []


def _one_page(code, offset, limit=100):
    page = offset // limit + 1
    # 兼容带前缀代码（sz300447）：东财接口只认裸 6 位代码
    bare = code[-6:] if len(code) > 6 else code
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1"
           f"&page_size={limit}&page_index={page}&ann_type=A&client_source=web"
           f"&stock_list={bare}&f_node=0&s_node=0")
    d = _get(url)
    items = ((d or {}).get("data") or {}).get("list") or []
    out = []
    for x in items:
        if not x.get("art_code"):
            continue
        out.append({"id": x["art_code"], "title": x.get("title") or "",
                    "time": (x.get("display_time") or x.get("eiTime") or "")[:19]})
    return out


NCACHE = "audit_notices.json"


def list_notices(codes, limit=100, max_pages=5, cover_before="2024-06-01"):
    """逐只翻页拉取公告，直到覆盖到 cover_before 之前或达到页数上限。
    注意：活跃个股 100 条可能只覆盖 4 个月，必须翻页才不会漏掉续聘公告。
    同日结果写缓存，避免重复拉取（续聘公告一年一次，日内不会变）。"""
    import datetime
    today = datetime.date.today().isoformat()
    try:
        nc = json.load(open(NCACHE, encoding="utf-8"))
    except Exception:
        nc = {}
    todo = [c for c in codes
            if not (nc.get(c, {}).get("date") == today and nc.get(c, {}).get("list"))]
    if todo:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(list_notices_one, c, limit, max_pages, cover_before): c
                    for c in todo}
            for f in futs:
                nc[futs[f]] = {"date": today, "list": f.result()}
        json.dump(nc, open(NCACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return {c: nc.get(c, {}).get("list", []) for c in codes}


def list_notices_one(code, limit=100, max_pages=5, cover_before="2024-06-01"):
    got, seen = [], set()
    for p in range(max_pages):
        page = _one_page(code, p * limit, limit)
        if not page:
            break
        new = [x for x in page if x["id"] not in seen]
        for x in new:
            seen.add(x["id"])
        got += new
        earliest = min((x.get("time", "")[:10] for x in got), default="")
        if earliest and earliest <= cover_before:
            break
        if len(new) < limit:
            break
    got.sort(key=lambda x: x.get("time", ""), reverse=True)
    return got


DCACHE = "audit_detail.json"
_dc = {}


def load_dc():
    global _dc
    try:
        _dc = json.load(open(DCACHE, encoding="utf-8"))
    except Exception:
        _dc = {}


def fetch_detail(nid):
    if nid in _dc:
        return _dc[nid][0], _dc[nid][1]
    d = _get("https://np-cnotice-stock.eastmoney.com/api/content/ann"
             f"?art_code={nid}&client_source=web&page_index=1")
    title, detail = "", ""
    data = (d or {}).get("data") or {}
    title = data.get("notice_title") or ""
    detail = data.get("notice_content") or ""
    _dc[nid] = [title, detail]
    return title, detail


# 事务所名里若含这些词，说明抓到的是"续聘会计师事务所"这类通用串，而非具体所名
BAD_WORD = re.compile(r"续聘|改聘|聘任|聘请|聘用|选聘|变更|年度|关于|公司|所有|执行|业务|"
                      r"文件|要求|相关|其他|前任|后任|原|新|该|下一|同一|的|任|请|用|度|"
                      r"能够|充分|了解|具备|具有|独立|客观|依法|证券|资质|资格|从事|审计|"
                      r"评估|咨询|服务|中介|机构|本所|我所|贵所|情况|规定|意见|报告|工作|"
                      r"人员|合伙|管理|监督|职责|履职|评价|制度|办法|条件|标准|程序|"
                      r"约定|明确|合同|同意|议案|表决|审议|通过|提交|股东|董事会|委员会|了|年审|年年")

ANY_FIRM = re.compile(r"([\u4e00-\u9fa5]{2,6})会计师事务所")

# 国内具备证券资格的主要会计师事务所（校验抓到的是真实所名，而非"续聘会计师事务所"这类串）
KNOWN = ["天健", "立信", "容诚", "致同", "大华", "中汇", "信永中和", "天职国际",
         "中兴华", "中兴财光华", "众华", "希格玛", "天圆全", "尤尼泰振青", "中审亚太",
         "中审众环", "中天恒", "中天运", "中喜", "久安", "苏亚金诚", "永拓", "鹏盛",
         "和信", "利安达", "华兴", "公证天业", "天衡", "上会", "兴华", "中准",
         "正中珠江", "安永华明", "普华永道", "德勤", "毕马威", "国富浩华", "瑞华",
         "中勤万信", "亚太", "华普天健", "万隆", "湘能", "开元", "中瑞", "四川华信",
         "北京中天恒", "深圳久安"]


def clean_firm(s):
    """把公告里抓到的串洗成干净的所名：优先按已知事务所名精确匹配"""
    s = re.sub(r"（[^）]*）|\([^)]*\)", "", (s or "")).strip()
    for k in sorted(KNOWN, key=len, reverse=True):
        if re.search(re.escape(k) + r"\s*会计师事务所", s):
            return k + "会计师事务所"
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,6})会计师事务所", s):
        nm = m.group(1)
        if BAD_WORD.search(nm):
            continue
        full = re.sub(r"就会计师事务所$", "会计师事务所", nm + "会计师事务所")
        if any(k in full for k in KNOWN):
            return full
    return ""


def best_firm(ev):
    """取事务所名称：优先用最新的续聘/聘任公告（公司可能中途换所），
    最新公告取不到时再退回出现频次最高的。"""
    ordered = sorted(ev, key=lambda e: e.get("time", ""), reverse=True)
    for key in ("firms", "firms_all"):      # firms 取不到时退回全文兜底
        for e in ordered:
            for f in e.get(key, []):
                nm = clean_firm(f)
                if nm:
                    return nm
    cnt = {}
    for e in ev:
        for f in e.get("firms", []):
            nm = clean_firm(f)
            if nm:
                cnt[nm] = cnt.get(nm, 0) + 1
    if not cnt:
        return ""
    return max(cnt.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def main(codes, verbose=True):
    load_dc()
    cache = {}
    if os.path.exists(OUT):
        try:
            cache = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            cache = {}
    if verbose:
        print(f"拉取 {len(codes)} 只个股公告...")
    notices = list_notices(codes)
    targets = []          # (code, notice)
    meta = {}
    for c in codes:
        lst = notices.get(c, [])
        hit = [x for x in lst if any(k in (x.get("title") or "") for k in TITLE_KW)]
        meta[c] = {"total": len(lst), "hit": len(hit),
                   "span": (lst[-1]["time"][:10] + " ~ " + lst[0]["time"][:10]) if lst else ""}
        for x in hit:
            targets.append((c, x))
    if verbose:
        print(f"命中审计相关公告 {len(targets)} 篇，读取正文...")

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {(c, x["id"]): ex.submit(fetch_detail, x["id"]) for c, x in targets}
        results = {k: f.result() for k, f in futs.items()}
    targets_all = [x for _, x in targets]

    out = {}
    for c in codes:
        ev = []
        for (cc, nid), (title, detail) in results.items():
            if cc != c:
                continue
            detail = detail or ""
            names = []
            for p in STRONG_PAT:
                for mm in p.finditer(detail):
                    names.append(mm.group(1).strip())
            strict = []
            for mm in LIXIN_STRICT.finditer(detail):
                ctx = detail[max(0, mm.start() - 50): mm.end() + 30].replace("\n", " ")
                if NOISE.search(ctx):      # 事务所沿革介绍，不算
                    continue
                strict.append(ctx)
            is_lx = any(("立信" in n and "中联" not in n) for n in names) or bool(strict)
            tm = next((x["time"][:10] for x in targets_all
                       if x["id"] == nid), "")
            # 兜底：全文里所有"XX会计师事务所"（用于 firms 取不到具体所名时）
            alln = [m.group(0) for m in ANY_FIRM.finditer(detail)
                    if not BAD_WORD.search(m.group(1))]
            cnt = {}
            for x in alln:
                cnt[x] = cnt.get(x, 0) + 1
            ev.append({"id": nid, "time": tm, "title": title or nid, "lixin": is_lx,
                       "firms": list(dict.fromkeys(names))[:6],
                       "firms_all": [k for k, _ in sorted(cnt.items(),
                                                          key=lambda kv: -kv[1])[:3]],
                       "snippet": (strict[0] if strict else (names[0] if names else ""))})
        if any(e["lixin"] for e in ev):
            status = "LIXIN"
        elif ev:
            status = "OK"
        else:
            status = "UNKNOWN"
        firm = best_firm(ev)
        out[c] = {"status": status, "firm": firm, "notices": ev, "meta": meta.get(c, {})}
        cache[c] = out[c]

    json.dump(cache, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(_dc, open(DCACHE, "w", encoding="utf-8"), ensure_ascii=False)

    if verbose:
        print(f"\n{'代码':<10}{'判定':<10}{'公告数':<7}{'审计相关':<9}事务所")
        for c in codes:
            o = out[c]
            tag = {"LIXIN": "★立信 排除", "OK": "非立信", "UNKNOWN": "未查到"}[o["status"]]
            print(f"{c:<10}{tag:<10}{o['meta'].get('total',0):<7}{len(o['notices']):<9}{o.get('firm','')}")
        n = sum(1 for v in out.values() if v["status"] == "LIXIN")
        u = sum(1 for v in out.values() if v["status"] == "UNKNOWN")
        print(f"\n立信命中 {n} 只，未查到 {u} 只 → 结果写入 {OUT}")
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cs = sys.argv[1].split(",")
    else:
        cs = [x["code"] for x in json.load(open("watchlist.json", encoding="utf-8"))]
    main(cs)
