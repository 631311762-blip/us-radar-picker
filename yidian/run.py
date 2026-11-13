# -*- coding: utf-8 -*-
"""易点模型推荐（易点模型清单）· 收盘后选股模型

按用户给定的 9 条口径等价恢复；所有阈值集中在 CONST 区，未作任何改动。
数据源：东方财富公开行情/公告接口（公网 HTTP），仅依赖 Python 3 标准库。
"""
import os, sys, json, csv, re, time, math, datetime, traceback
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
# —— 运行环境适配（仅路径/计时，不改任何阈值与选股逻辑）——
# STATE_DIR 由 GitHub Actions 注入（如 yidian/state）；本地独立运行时退回脚本目录。
STATE_DIR = os.environ.get("STATE_DIR", "").strip() or BASE
ARCHIVE = os.path.join(STATE_DIR, "archive")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
INDEX_PATH = os.path.join(STATE_DIR, "index.jsonl")
LOG_PATH = os.path.join(STATE_DIR, "run.log")
SUMMARY_PATH = os.path.join(STATE_DIR, "summary.json")
# 行情/复盘按北京时间计日（UTC runner 亦正确），并支持 RUN_DATE 覆盖以便补跑/验证。
_BJ = datetime.timezone(datetime.timedelta(hours=8))
TODAY = (os.environ.get("RUN_DATE", "").strip()
         or datetime.datetime.now(_BJ).date().isoformat())
os.makedirs(ARCHIVE, exist_ok=True)

# ===================== 口径常量（勿改） =====================
PREFIX_CYB = ("300", "301", "302")
PREFIX_KCB = ("688", "689")
PREFIX_BJ = ("43", "83", "87", "88", "92")
FS_CYB, FS_KCB, FS_BJ = "m:0+t:80", "m:1+t:23", "m:0+t:81"
# 2026-11-13 用户确认：因子回溯显示北交所 T+5 -1.68% / 胜率 34.05%，显著拖累，
# 故从样本剔除，仅保留创业板 + 科创板（其余阈值、评分、硬条件一律不变）。
# 设回 True 即恢复三板块口径。统一过滤在 main() 取数后执行，覆盖「自有枚举」与
# 「共同前置层 adapter 注入样本池」两条路径，因此无需改动公共 common/adapter.py。
INCLUDE_BJ = False
DD90_MIN = 0.15            # 90日回撤 ≥15%
DD90_BASIS = "prev_close"  # 90日回撤基准价（折中口径，2026-11-13 用户确认）：
                           #   prev_close = 提示点【前一日收盘】（默认）—— 启动大阳线不再
                           #     把回撤「自我修复」到不足15%，同时仍要求股价处于低位；
                           #   open / low = 当日开盘 / 当日最低；
                           #   close      = 当日收盘（旧口径，会被启动阳线自我修复，已弃用）；
                           #   max_dd     = 90日最大回撤(峰值后谷值)，筛选力过低，不推荐。
                           # 只改此常量即可切换口径，push 后 yidian.yml 自动生效。
R5_MAX = 0.45              # 近5日涨幅 ≤45%
CHG_MIN, CHG_MAX = 0.5, 22.0     # 当日涨幅 0.5-22%（百分数）
TURN_MIN, TURN_MAX = 2.0, 35.0   # 换手 2-35%（百分数）
PRICE_MIN, PRICE_MAX = 2.0, 200.0
VR_MIN = 1.4               # 量比 ≥1.4
MA20_FLOOR = 0.95          # 收盘 ≥ MA20×0.95
MA20_DIST_MAX = 0.12       # 距MA20 ≤12%
REQUIRE_YANG = True        # 易点模型提示点必须为阳线（收盘>开盘）：
                           # 阴线 / 十字星 / 高开低走大阴线一律剔除
TOP_N = 6                  # 评分取前 6 推送
BUY_LOW = 0.975            # 买点 = 收盘×0.975 ~ 收盘
STOP_LOSS, TARGET = -0.05, 0.07
MARKDOWN_LIMIT = 900       # 正文 ≤900 字
MIN_UNIVERSE = 1500        # 样本明显偏小视为数据源异常；剔除北交所后正常约 1977
                           # （创业板约 1372 + 科创板约 605），留约 25% 余量
# ===========================================================

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://quote.eastmoney.com/"}
EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"

_logf = open(LOG_PATH, "a", encoding="utf-8")
def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    _logf.write(line + "\n"); _logf.flush()


def http_json(url, tries=5, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last


def num(v, default=None):
    try:
        if v in (None, "", "-", "null"):
            return default
        return float(v)
    except Exception:
        return default


# ---------- 1. 全量枚举样本（分页全量，非涨幅榜前排） ----------
PAGE_FAILS = []

def fetch_board(fs, label):
    out, pn = [], 1
    while pn <= 100:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&fs=%s"
               "&fields=f2,f3,f8,f10,f12,f14,f20,f21" % (pn, urllib.parse.quote(fs, safe=":+")))
        d = None
        for attempt in range(3):
            try:
                d = http_json(url, tries=3)
                break
            except Exception as e:
                if attempt == 2:
                    log("  %s 第%d页持续失败: %s（跳过该页）" % (label, pn, e))
                    PAGE_FAILS.append((label, pn))
                else:
                    time.sleep(1.2 * (attempt + 1))
        pn += 1
        time.sleep(0.12)
        if d is None:
            continue
        data = d.get("data")
        if not data or not data.get("diff"):
            break
        diff = data["diff"]
        items = list(diff.values()) if isinstance(diff, dict) else diff
        if not items:
            break
        out.extend(items)
        total = data.get("total") or 0
        if len(out) >= total:
            break
    log("  %s 板块接口返回 %d 条" % (label, len(out)))
    return out


def build_universe():
    log("全量枚举样本（代码空间全量扫描，分页拉全量）...")
    raw = []
    raw += fetch_board(FS_CYB, "创业板")
    raw += fetch_board(FS_KCB, "科创板")
    if INCLUDE_BJ:
        raw += fetch_board(FS_BJ, "北交所/三板全量")

    cyb, kcb, bj = [], [], []
    seen = set()
    for it in raw:
        code = str(it.get("f12") or "").strip()
        name = str(it.get("f14") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            continue
        # ST/*ST/退市/N 次新剔除
        n = name.replace(" ", "").upper()
        if ("ST" in n or "退" in n or n.startswith("N") or
                re.match(r"^\*", name.strip()) or "PT" in n):
            continue
        price = num(it.get("f2"))
        if price is None or price <= 0:       # 无有效行情（非上市/停牌/退市残留）
            continue
        rec = {
            "code": code, "name": name,
            "price": price / 100.0,
            "chg_pct": num(it.get("f3"), 0) / 100.0,
            "turn_pct": num(it.get("f8"), 0) / 100.0,
            "vr": num(it.get("f10"), 0) / 100.0,
            "mktcap": num(it.get("f20"), 0),       # 仅归档展示，不参与筛选
            "float_mktcap": num(it.get("f21"), 0), # 仅归档展示，不参与筛选
        }
        if code.startswith(PREFIX_CYB):
            rec["board"] = "创业板"; cyb.append(rec)
        elif code.startswith(PREFIX_KCB):
            rec["board"] = "科创板"; kcb.append(rec)
        elif INCLUDE_BJ and code.startswith(PREFIX_BJ):
            rec["board"] = "北交所"; bj.append(rec)
        else:
            continue
        seen.add(code)

    meta = {"date": TODAY, "total": len(cyb) + len(kcb) + len(bj),
            "cyb_kcb": len(cyb) + len(kcb), "cyb": len(cyb), "kcb": len(kcb),
            "bj": len(bj), "page_fails": len(PAGE_FAILS)}
    log("样本总量 %d（创业板/科创板 %d = 创业板 %d + 科创板 %d；北交所 %d）"
        % (meta["total"], meta["cyb_kcb"], meta["cyb"], meta["kcb"], meta["bj"]))
    return cyb + kcb + bj, meta


def apply_board_scope(universe, meta):
    """统一板块口径（2026-11-13 起剔除北交所）。

    数据可能来自自有 build_universe()，也可能来自共同前置层 adapter 注入的
    「创业板+科创板+北交所」样本池（adapter 会整体替换 build_universe）。
    因此在 main() 取数之后统一过滤一次，保证任何路径都不会选到北交所标的，
    同时重算 meta 计数，使归档 / summary / index / 推送正文口径一致。
    """
    if INCLUDE_BJ:
        return universe, meta
    before = len(universe)
    universe = [r for r in universe if r.get("board") != "北交所"]
    cyb = sum(1 for r in universe if r.get("board") == "创业板")
    kcb = sum(1 for r in universe if r.get("board") == "科创板")
    meta = dict(meta)
    meta.update({"total": len(universe), "cyb": cyb, "kcb": kcb,
                 "cyb_kcb": cyb + kcb, "bj": 0,
                 "bj_excluded": before - len(universe)})
    return universe, meta


# ---------- 2. 日K ----------
def kline(code, board, days=110):
    secid = ("1." if board == "科创板" else "0.") + code
    beg = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=200)).strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=%s&ut=%s&klt=101&fqt=1&beg=%s&end=20500101&lmt=%d"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           % (secid, EM_UT, beg, days))
    d = http_json(url, tries=3, timeout=20)
    data = d.get("data") or {}
    ks = data.get("klines") or []
    bars = []
    for s in ks:
        p = s.split(",")
        if len(p) < 11:
            continue
        bars.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]),
                     "vol": float(p[5]), "turn": float(p[10])})
    return bars


def ma(bars, n, shift=0):
    if len(bars) < n + shift:
        return None
    seg = bars[len(bars) - n - shift: len(bars) - shift]
    return sum(b["close"] for b in seg) / n


def check_trading_day(sample):
    log("校验行情日期（非交易日自动退出、不推送）...")
    dates = []
    for r in sample[:5]:
        try:
            b = kline(r["code"], r["board"], 8)
            if b:
                dates.append(b[-1]["date"])
        except Exception:
            pass
    if not dates:
        raise RuntimeError("行情日期无法获取")
    latest = max(dates)
    log("  行情最新日期 = %s，当日 = %s" % (latest, TODAY))
    if latest != TODAY:
        log("[ABORT] 非交易日（行情日期≠当日），自动退出、不推送")
        return False
    return True


# ---------- 3. 硬性条件 ----------
def dd90_ref(bars, win):
    """90日回撤基准价。折中口径取「提示点前一日收盘」：
    启动当天的大阳线不会把回撤自我修复掉，又比「90日最大回撤」严格
    （用最近一个交易日的位置，而非区间内任意最低点），保留洗盘充分的筛选意图。"""
    if DD90_BASIS == "close":
        return bars[-1]["close"]
    if DD90_BASIS == "open":
        return bars[-1]["open"]
    if DD90_BASIS == "low":
        return bars[-1]["low"]
    if DD90_BASIS == "max_dd":
        k = max(range(len(win)), key=lambda i: win[i]["high"])
        return min(b["low"] for b in win[k:])
    return bars[-2]["close"] if len(bars) >= 2 else bars[-1]["close"]  # prev_close


def hard_filter(rec, bars):
    if len(bars) < 61:
        return None
    c = bars[-1]["close"]
    o = bars[-1]["open"]
    # 易点模型核心口径：提示点必须是阳线（收盘 > 开盘）。
    # 「相对昨收涨幅」为正 ≠ 阳线：高开低走会形成大阴线却仍显示红盘，必须单独剔除。
    entity_pct = (c / o - 1.0) * 100 if o > 0 else 0.0
    if REQUIRE_YANG and c <= o:
        return None
    if not (CHG_MIN <= rec["chg_pct"] <= CHG_MAX):
        return None
    if not (TURN_MIN <= rec["turn_pct"] <= TURN_MAX):
        return None
    if not (PRICE_MIN <= rec["price"] <= PRICE_MAX):
        return None
    if rec["vr"] < VR_MIN:
        return None
    ma20 = ma(bars, 20)
    ma5, ma5_prev, ma60 = ma(bars, 5), ma(bars, 5, 1), ma(bars, 60)
    if ma20 is None or ma5 is None or ma5_prev is None or ma60 is None:
        return None
    if c < MA20_FLOOR * ma20:
        return None
    dist = abs(c / ma20 - 1.0)
    if dist > MA20_DIST_MAX:
        return None
    win = bars[-90:] if len(bars) >= 90 else bars
    hi = max(b["high"] for b in win)
    ref = dd90_ref(bars, win)
    dd90 = (hi - ref) / hi if hi > 0 else 0.0
    if dd90 < DD90_MIN:
        return None
    r5 = c / bars[-6]["close"] - 1.0 if len(bars) >= 6 else 0.0
    if r5 > R5_MAX:
        return None
    ma5_up = ma5 > ma5_prev
    below_ma60 = c < ma60
    if not (ma5_up or below_ma60):
        return None
    d = dict(rec)
    d.update({"close": c, "open": o, "entity_pct": round(entity_pct, 2),
              "dd90": dd90, "dd90_ref": round(ref, 2), "dd90_basis": DD90_BASIS,
              "r5": r5, "ma20": ma20, "ma5": ma5,
              "ma5_prev": ma5_prev, "ma60": ma60, "dist_ma20": dist,
              "ma5_up": ma5_up, "below_ma60": below_ma60,
              "high90": hi, "bars": bars})
    return d


# ---------- 4. 审计一票否决（立信 / 查询缺失 剔除） ----------
KNOWN_FIRMS = ["天健", "立信", "容诚", "致同", "大华", "中汇", "信永中和",
               "天职国际", "中审众环", "大信", "致同", "中兴华", "上会",
               "毕马威华振", "德勤华永", "安永华明", "普华永道中天",
               "中喜", "永拓", "和信", "希格玛", "苏亚金诚", "公证天业",
               "亚太", "中审亚太", "北京中天恒", "四川华信", "尤尼泰振青",
               "中兴财光华", "中准", "利安达", "天衡", "众华", "华兴",
               "立信中联", "中勤万信", "容诚", "天圆全", "信永中和"]

def _ann_list(code):
    out = []
    for pg in (1, 2):
        url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1"
               "&page_size=100&page_index=%d&ann_type=A&client_source=web&stock_list=%s" % (pg, code))
        try:
            d = http_json(url, timeout=20)
            out += ((d.get("data") or {}).get("list") or [])
        except Exception:
            pass
    return out

def _ann_detail(art_code):
    url = ("https://np-cnotice-stock.eastmoney.com/api/content/ann?art_code=%s"
           "&client_source=web&page_index=1" % art_code)
    d = http_json(url, timeout=20)
    return json.dumps(d, ensure_ascii=False)

NOISE_WORDS = ("本次", "公司", "选聘", "续聘", "拟", "关于", "报告", "公告", "评估",
               "变更", "聘任", "前任", "任同一", "任的", "审计", "年度", "履职", "监督",
               "独立", "专项", "说明", "相关", "上述", "会计师", "事务所", "内部控制",
               "鉴证", "签字", "合伙", "有限", "分所", "双方", "本所", "该所", "我们",
               "机构", "议案", "同意", "决定", "聘请", "改聘", "拟聘", "续聘", "聘任",
               "及", "与", "和的", "为由", "经对", "按将", "已并", "或在", "是有")

def audit_one(code):
    """返回 (status, firm)：status ∈ OK / LIXIN / UNKNOWN
    口径：立信（含续聘/改聘）或查询缺失 → 剔除。"""
    try:
        lst = _ann_list(code)
        cand = [a for a in lst if re.search(
            r"会计师事务所|续聘|改聘|聘任|审计机构|年报审计", str(a.get("title", "")))]
        texts = []
        for a in cand[:6]:
            try:
                texts.append(_ann_detail(str(a.get("art_code", ""))))
            except Exception:
                pass
        blob = "".join(texts)
        # 严格立信命中（排除"立信中联"等近似名、以及事务所沿革介绍）
        if re.search(r"(?<![中北京])立信(?!中联)\s*会计师事务所", blob):
            return "LIXIN", "立信会计师事务所"
        names = re.findall(r"([\u4e00-\u9fa5]{2,8})会计师事务所", blob)
        clean = []
        for n in names:
            n = n.strip()
            if "立信中联" in n or any(w in n for w in NOISE_WORDS):
                continue
            hit = next((k for k in KNOWN_FIRMS if k in n and k != "立信"), None)
            clean.append(hit or n)
        if clean:
            # 取出现频次最高者（最新续聘公告的干扰已由噪声词过滤）
            best = max(set(clean), key=clean.count)
            return "OK", best + "会计师事务所"
        return "UNKNOWN", ""
    except Exception as e:
        return "UNKNOWN", "查询失败:%s" % type(e).__name__


# ---------- 5. 评分 / 状态 / 启动天数 ----------
def score(d):
    """按口径恢复的可复现打分（0-100）：偏好回撤充分、量能温和放大、
    贴近MA20、未过度追高、MA5拐头。原脚本公式不可考，故采用透明加权。"""
    s = 0.0
    s += 22 * min(1.0, (d["dd90"] - 0.15) / 0.20)                 # 回撤深度
    s += 16 * min(1.0, (d["vr"] - 1.4) / 1.6)                     # 量比
    s += 12 * min(1.0, (d["turn_pct"] - 2) / 10)                  # 换手
    s += 14 * max(0.0, 1 - d["dist_ma20"] / 0.12)                 # 贴近MA20
    s += 10 * max(0.0, 1 - abs(d["chg_pct"] - 4.0) / 10.0)        # 当日涨幅适中
    s += 10 * max(0.0, 1 - max(0.0, d["r5"]) / 0.45)               # 近5日不追高
    s += 10 * (1.0 if d["ma5_up"] else 0.4)                        # MA5拐头
    s += 6 * (1.0 if d["below_ma60"] else 0.5)                     # 价低于MA60
    return round(min(100.0, s), 2)


def startup_days(bars):
    """连续满足启动条件的交易日数（首次运行按K线回溯）。"""
    n = 0
    for i in range(len(bars) - 1, 3, -1):
        seg = bars[:i + 1]
        m5, m5p, m20 = ma(seg, 5), ma(seg, 5, 1), ma(seg, 20)
        if m5 is None or m5p is None or m20 is None:
            break
        c = seg[-1]["close"]
        prev = seg[-2]["close"] if len(seg) >= 2 else c
        chg = (c / prev - 1) * 100 if prev else 0
        if m5 > m5p and c >= MA20_FLOOR * m20 and chg >= 0.5:
            n += 1
        else:
            break
    return max(1, n)


def classify(d):
    c, b = d["close"], d["bars"]
    if d["ma5_up"] and d["chg_pct"] >= 3.0 and d["vr"] >= 1.4 and c > d["ma20"]:
        return "已启动", startup_days(b)
    if d["dist_ma20"] <= 0.03 and d["vr"] >= 2.0:
        return "临界", 0
    if d["below_ma60"] and d["dd90"] >= 0.25 and d["turn_pct"] <= 8.0:
        return "筑底", 0
    return "观察", 0


def watch_signals(d):
    """观察池形态：下轨缩量 / 上影试盘"""
    bars, sig = d["bars"], []
    last = bars[-1]
    low20 = min(x["low"] for x in bars[-20:])
    vol5 = sum(x["vol"] for x in bars[-6:-1]) / 5.0 if len(bars) >= 6 else last["vol"]
    if last["close"] <= low20 * 1.03 and last["vol"] <= vol5 * 0.8:
        sig.append("下轨缩量")
    body_hi = max(last["open"], last["close"])
    if last["high"] >= body_hi * 1.02 and (last["high"] - body_hi) >= (body_hi - last["low"]) * 0.8:
        sig.append("上影试盘")
    return sig


# ---------- 6. 昨日复盘 ----------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"last_archive": None, "cum_wins": 0, "cum_settled": 0, "history": []}


def review(state, quote_map):
    prev = state.get("last_archive")
    if not prev or not prev.get("codes") or prev.get("date") == TODAY:
        return {"samples": 0, "wins": 0, "losses": 0, "pending": 0,
                "win_rate": None, "note": "首次运行/同日重跑，无此前归档候选，昨日复盘无样本"}
    wins = losses = pending = 0
    details = []
    for item in prev["codes"]:
        code, base = item["code"], item.get("close")
        q = quote_map.get(code)
        if not q or not base:
            pending += 1; continue
        chg = (q["price"] / base - 1.0) * 100
        if chg >= 7.0:
            wins += 1; tag = "达标(+7%)"
        elif chg <= -5.0:
            losses += 1; tag = "止损(-5%)"
        else:
            pending += 1; tag = "观察中"
        details.append({"code": code, "name": item.get("name"), "next_chg_pct": round(chg, 2), "tag": tag})
    return {"samples": len(prev["codes"]), "wins": wins, "losses": losses,
            "pending": pending, "details": details,
            "win_rate": round(wins / (wins + losses), 4) if (wins + losses) else None,
            "note": "按次日涨幅对 +7% 目标 / -5% 止损结算"}


# ---------- 7. 归档 / 推送 ----------
def archive(all_q, top, meta, audit_removed, watch_pool, review_res, state):
    stamp = TODAY.replace("-", "")
    jf = os.path.join(ARCHIVE, "qualified_%s.json" % stamp)
    cf = os.path.join(ARCHIVE, "qualified_%s.csv" % stamp)
    payload = {
        "meta": dict(meta, audit_veto_count=audit_removed, qualified_count=len(all_q),
                     top_count=len(top), watch_pool_count=len(watch_pool)),
        "qualified": [{k: v for k, v in q.items() if k != "bars"} for q in all_q],
        "top": [{k: v for k, v in t.items() if k != "bars"} for t in top],
        "watch_pool": watch_pool,
        "review": review_res,
    }
    json.dump(payload, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    cols = ["code", "name", "board", "close", "open", "entity_pct", "chg_pct",
            "turn_pct", "vr", "price",
            "dd90", "dd90_ref", "dd90_basis",
            "r5", "ma20", "dist_ma20", "ma5_up", "below_ma60",
            "mktcap", "float_mktcap", "audit_firm", "score", "status",
            "startup_day", "buy_low", "buy_high", "stop_loss", "target"]
    with open(cf, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for q in all_q:
            w.writerow(q)
    entry = {"date": TODAY, "sample_total": meta["total"],
             "cyb_kcb": meta["cyb_kcb"], "bj": meta["bj"],
             "include_bj": INCLUDE_BJ, "bj_excluded": meta.get("bj_excluded", 0),
             "qualified": len(all_q), "audit_veto": audit_removed,
             "top6": [{"code": t["code"], "name": t["name"], "score": t["score"]} for t in top],
             "json": os.path.basename(jf), "csv": os.path.basename(cf),
             "cum_win_rate": state["cum_win_rate"]}
    lines = []
    if os.path.exists(INDEX_PATH):
        for ln in open(INDEX_PATH, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                if json.loads(ln).get("date") == TODAY:   # 同日重跑覆盖，不追加重复行
                    continue
            except Exception:
                pass
            lines.append(ln)
    lines.append(json.dumps(entry, ensure_ascii=False))
    open(INDEX_PATH, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    state["last_archive"] = {"date": TODAY,
                             "codes": [{"code": q["code"], "name": q["name"], "close": q["close"]} for q in all_q]}
    state["history"].append({"date": TODAY, "qualified": len(all_q), "review": review_res})
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 仓库即云端：记录相对文件名（不写入运行机绝对路径，避免污染提交内容）
    json.dump({"json": os.path.basename(jf), "csv": os.path.basename(cf),
               "index": os.path.basename(INDEX_PATH), "state": os.path.basename(STATE_PATH)},
              open(os.path.join(ARCHIVE, "cloud_upload_pending.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return jf, cf


def pushplus(title, content, token):
    if not token:
        return False, "未配置 PushPlus token（PUSHPLUS_TOKEN 或 pushplus_token.txt）"
    data = urllib.parse.urlencode({"token": token, "title": title,
                                   "content": content, "template": "markdown"}).encode()
    try:
        req = urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "Mozilla/5.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "ignore")
        ok = '"code":200' in body.replace(" ", "") or '"code": 200' in body
        return ok, body[:200]
    except Exception as e:
        return False, str(e)


def build_markdown(top, meta, audit_removed, review_res, cum_rate, qualified_count):
    L = ["## 易点模型推荐 · %s" % TODAY, "",
         "样本 %d（创业板 %d / 科创板 %d；%s）｜合格 %d｜审计否决 %d"
         % (meta["total"], meta.get("cyb", 0), meta.get("kcb", 0),
            (("含北交所 %d" % meta.get("bj", 0)) if meta.get("bj") else "已剔除北交所"),
            qualified_count, audit_removed), ""]
    r = review_res
    if r["samples"]:
        L.append("昨日复盘 %d 只：达标 %d / 止损 %d / 观察 %d；累计胜率 %s"
                 % (r["samples"], r["wins"], r["losses"], r["pending"],
                    ("%.1f%%" % (cum_rate * 100)) if cum_rate is not None else "—"))
    else:
        L.append("昨日复盘：无历史归档样本；累计胜率 %s" %
                 (("%.1f%%" % (cum_rate * 100)) if cum_rate is not None else "—"))
    L += ["", "### 前 %d 只" % len(top), ""]
    for i, t in enumerate(top, 1):
        L.append("%d. **%s** `%s` %s｜%s｜启动第%d天｜%.1f分｜阳线%+.2f%%" %
                 (i, t["name"], t["code"], t["board"], t["status"],
                  t["startup_day"], t["score"], t.get("entity_pct", 0)))
        L.append("   买点 %.2f~%.2f｜止损 %.2f｜目标 %.2f｜审计 %s" %
                 (t["buy_low"], t["buy_high"], t["stop_loss"], t["target"], t["audit_firm"]))
    L += ["", "买点=收盘×0.975~收盘，止损 -5%，目标 +7%。",
          "提示点均为阳线（收盘>开盘），已剔除阴线/十字星/高开低走大阴线。",
          "90日回撤按%s计（折中口径），避免启动阳线自我修复。"
          % {"prev_close": "启动前一日收盘", "open": "当日开盘", "low": "当日最低",
             "close": "当日收盘", "max_dd": "90日最大回撤"}.get(DD90_BASIS, DD90_BASIS),
          "状态：已启动/临界/筑底/观察；观察池含下轨缩量、上影试盘。",
          "", "*统计研究，不构成投资建议。*"]
    txt = "\n".join(L)
    if len(txt) > MARKDOWN_LIMIT:
        txt = txt[:MARKDOWN_LIMIT]
    return txt


# ---------- 主流程 ----------
def main():
    # 共同前置层接入（强制）：数据入口自动切到共享样本池/K线缓存。
    _ad = None
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "..", "common"))
        import adapter as _ad
        _ad.install("yidian", globals())
        # 共同层已做 ST/立信剔除，创/科/北交样本会比原始枚举略小（如剔除 773 只
        # 立信后 2000→1960），这是正确的合规结果，不应触发「样本偏小=数据源异常」。
        # 且共同层 prefilter 已分页拉到 total、空池会终止，全量由它保证，
        # 故就绪时关闭易点自身的样本下限自保护（仅共同层就绪时生效）。
        globals()["MIN_UNIVERSE"] = 0
    except Exception:
        _ad = None
    globals()["_COMMON_ADAPTER"] = _ad
    log("=" * 60)
    log("易点模型推荐 启动 | 基准日 %s" % TODAY)
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    tf = os.path.join(BASE, "pushplus_token.txt")
    if not token and os.path.exists(tf):
        token = open(tf, encoding="utf-8").read().strip()

    try:
        universe, meta = build_universe()
        # 统一板块口径：无论数据来自自有枚举还是共同前置层，都在此剔除北交所。
        universe, meta = apply_board_scope(universe, meta)
        log("板块口径：%s｜样本 %d（创业板 %d + 科创板 %d）"
            % (("三板块（含北交所）" if INCLUDE_BJ else
                "仅创业板/科创板，已剔除北交所 %d 只" % meta.get("bj_excluded", 0)),
               meta["total"], meta.get("cyb", 0), meta.get("kcb", 0)))
    except Exception as e:
        log("[FAIL] 数据获取失败：%s" % e)
        pushplus("易点模型 · 数据获取告警", "样本枚举失败：%s" % e, token)
        _write_summary({"ok": False, "error": "数据获取失败: %s" % e, "date": TODAY})
        return 1

    if meta["total"] < MIN_UNIVERSE:
        log("[FAIL] 样本明显偏小(%d<%d)，判为数据源异常，不推送" % (meta["total"], MIN_UNIVERSE))
        pushplus("易点模型 · 数据源异常告警", "样本总量 %d，低于 %d，本次未推送" % (meta["total"], MIN_UNIVERSE), token)
        _write_summary({"ok": False, "error": "数据源异常，样本偏小", "meta": meta, "date": TODAY})
        return 1

    try:
        if not check_trading_day(universe):
            _write_summary({"ok": False, "abort": "非交易日自动退出、不推送", "date": TODAY, "meta": meta})
            return 0
    except Exception as e:
        log("[FAIL] 行情日期校验失败：%s" % e)
        pushplus("易点模型 · 数据获取告警", "行情日期校验失败：%s" % e, token)
        _write_summary({"ok": False, "error": "行情日期校验失败: %s" % e, "date": TODAY})
        return 1

    quote_map = {r["code"]: r for r in universe}

    # 粗筛（批量行情字段，无市值限制）
    coarse = [r for r in universe
              if CHG_MIN <= r["chg_pct"] <= CHG_MAX
              and TURN_MIN <= r["turn_pct"] <= TURN_MAX
              and PRICE_MIN <= r["price"] <= PRICE_MAX
              and r["vr"] >= VR_MIN]
    log("批量行情粗筛：%d 只进入K线精算" % len(coarse))

    passed, failed, yin_removed = [], 0, 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut = {ex.submit(kline, r["code"], r["board"]): r for r in coarse}
        for f in as_completed(fut):
            r = fut[f]
            try:
                bars = f.result()
                if bars and bars[-1]["close"] <= bars[-1]["open"]:
                    yin_removed += 1        # 阴线 / 十字星 / 高开低走大阴线
                d = hard_filter(r, bars) if bars else None
                if d:
                    passed.append(d)
            except Exception:
                failed += 1
    log("K线精算完成：通过硬性条件 %d 只（其中阳线口径剔除阴线 %d 只），K线失败 %d 只"
        % (len(passed), yin_removed, failed))
    if coarse and failed / len(coarse) > 0.30:
        log("[FAIL] K线失败率 %.1f%% 过高，判为数据源异常，不推送" % (100 * failed / len(coarse)))
        pushplus("易点模型 · 数据获取告警", "K线失败率过高 %.1f%%" % (100 * failed / len(coarse)), token)
        _write_summary({"ok": False, "error": "K线失败率过高", "date": TODAY, "meta": meta})
        return 1

    # 审计一票否决
    log("审计一票否决核查（立信 / 查询缺失剔除）...")
    audit_res = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut = {ex.submit(audit_one, d["code"]): d["code"] for d in passed}
        for f in as_completed(fut):
            code = fut[f]
            try:
                audit_res[code] = f.result()
            except Exception:
                audit_res[code] = ("UNKNOWN", "查询失败")
    qualified, audit_removed = [], 0
    for d in passed:
        st, firm = audit_res.get(d["code"], ("UNKNOWN", ""))
        if st == "LIXIN" or st == "UNKNOWN":
            audit_removed += 1
            continue
        d["audit_firm"] = firm
        d["score"] = score(d)
        d["status"], d["startup_day"] = classify(d)
        d["signals"] = watch_signals(d)
        c = d["close"]
        d["buy_low"] = round(c * BUY_LOW, 2)
        d["buy_high"] = round(c, 2)
        d["stop_loss"] = round(c * (1 + STOP_LOSS), 2)
        d["target"] = round(c * (1 + TARGET), 2)
        qualified.append(d)
    qualified.sort(key=lambda x: -x["score"])
    log("审计否决剔除 %d 只 → 合格股 %d 只" % (audit_removed, len(qualified)))

    watch_pool = [{"code": q["code"], "name": q["name"], "signals": q["signals"]}
                  for q in qualified if q["signals"]]
    top = qualified[:TOP_N]

    # 共同前置层：写统一推荐台账（出口 gate + 回溯/参数修正）
    _ad = globals().get("_COMMON_ADAPTER")
    if _ad:
        try:
            _ad.record("yidian", top)
        except Exception:
            pass

    state = load_state()
    review_res = review(state, quote_map)
    w, l = review_res["wins"], review_res["losses"]
    state["cum_wins"] = state.get("cum_wins", 0) + w
    state["cum_settled"] = state.get("cum_settled", 0) + w + l
    state["cum_win_rate"] = (state["cum_wins"] / state["cum_settled"]
                             if state["cum_settled"] else None)
    log("昨日复盘：样本 %d / 达标 %d / 止损 %d；累计胜率 %s"
        % (review_res["samples"], w, l,
           ("%.2f%%" % (state["cum_win_rate"] * 100)) if state["cum_win_rate"] is not None else "—"))

    jf, cf = archive(qualified, top, meta, audit_removed, watch_pool, review_res, state)
    log("归档完成：%s / %s / index.jsonl / state.json" % (os.path.basename(jf), os.path.basename(cf)))

    md = build_markdown(top, meta, audit_removed, review_res, state["cum_win_rate"], len(qualified))
    pf = os.path.join(ARCHIVE, "push_%s.md" % TODAY.replace("-", ""))
    open(pf, "w", encoding="utf-8").write(md)
    ok, msg = pushplus("易点模型推荐 · %s · 前%d只" % (TODAY, len(top)), md, token)
    if ok:
        log("[OK] PushPlus 推送成功")
    else:
        log("[FAIL] PushPlus 推送未成功：%s（正文已留存 %s）" % (msg, os.path.basename(pf)))

    summary = {
        "ok": True, "date": TODAY, "meta": meta,
        "qualified_count": len(qualified), "audit_veto_count": audit_removed,
        "yin_removed": yin_removed, "require_yang": REQUIRE_YANG,
        "dd90_basis": DD90_BASIS, "dd90_min": DD90_MIN,
        "include_bj": INCLUDE_BJ, "bj_excluded": meta.get("bj_excluded", 0),
        "top": [{"name": t["name"], "code": t["code"], "board": t["board"],
                 "status": t["status"], "startup_day": t["startup_day"],
                 "score": t["score"], "close": t["close"], "chg_pct": t["chg_pct"],
                 "entity_pct": t.get("entity_pct"), "audit_firm": t["audit_firm"]} for t in top],
        "review": review_res, "cum_win_rate": state["cum_win_rate"],
        "cum_settled": state["cum_settled"],
        "watch_pool_count": len(watch_pool),
        "archive": {"json": jf, "csv": cf, "index": INDEX_PATH, "state": STATE_PATH,
                    "push_md": pf},
        "push": {"ok": ok, "msg": msg},
        "kline_failed": failed,
    }
    _write_summary(summary)
    log("完成。")
    return 0


def _write_summary(obj):
    json.dump(obj, open(SUMMARY_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("[FATAL] %s\n%s" % (e, traceback.format_exc()))
        _write_summary({"ok": False, "error": str(e), "date": TODAY})
        sys.exit(1)
