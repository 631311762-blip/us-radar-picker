# -*- coding: utf-8 -*-
"""数据取数层 v4 —— 2026-11-02 WorkBuddy 更新移除 westock 本地脚本后的替代方案。
- K线：腾讯行情后端 web.ifzq.gtimg.cn（与腾讯自选股同源，前复权日K）
- 候选池：东方财富 clist 仅做【市值/换手率粗筛 + ST识别】，价格数据仍全部来自腾讯
- 逐bar换手率(exchange)：由成交量与流通股本推算，仅用于"试盘"标签判定，
  不参与模型打分（9 个权重因子全部只依赖 OHLCV）
环境变量 KLINE_END=YYYY-MM-DD 可取历史截止日（补跑历史基准日用）。
"""
import json, os, re, time, datetime, threading, urllib.request, urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor

ST_PAT = re.compile(r"ST|退市|退")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_UA_SINA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://finance.sina.com.cn/"}

# K线入口按顺序尝试：主域名曾因批量请求被网关 501，后两个入口结构相同、可兜底
KLINE_HOSTS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param=",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=",
)

# ── 2026-11-02 事故二：批量抓 1700 只后腾讯三个域名同时返回 501（网关封锁，
#    持续数十分钟不恢复），而原逻辑每只票都要把 3 个域名×3 次重试跑完，
#    2650 只×9 请求 = 2.4 万次无效请求，流程卡死在取数阶段。
#    对策：① 全局限速（默认 ~7 请求/秒，可用 FETCH_MIN_INTERVAL 调整）
#          ② 域名级熔断：收到 501/403/429 立即标记封禁 10 分钟，不再尝试
#          ③ 新浪日线兜底（不复权，仅在前两者全失效时启用）
_MIN_INTERVAL = float(os.environ.get("FETCH_MIN_INTERVAL", "0.15"))
_LOCK = threading.Lock()
_LAST_REQ = [0.0]
_DEAD = {}                     # host -> 解封时间
_DEAD_TTL = float(os.environ.get("FETCH_DEAD_TTL", "600"))
_STATS = {"req": 0, "sina": 0, "tencent": 0, "kline_fail": 0}


def _throttle():
    with _LOCK:
        now = time.time()
        wait = _LAST_REQ[0] + _MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.time()
        _STATS["req"] += 1


def _mark_dead(host):
    with _LOCK:
        _DEAD[host] = time.time() + _DEAD_TTL


def _is_dead(host):
    with _LOCK:
        until = _DEAD.get(host, 0)
        if until and time.time() > until:
            _DEAD.pop(host, None)
            return False
        return bool(until)


def stats():
    return dict(_STATS, dead_hosts=[h for h in _DEAD])


def _get(url, timeout=20, retry=2, host=None):
    for i in range(retry + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 网关封锁/限流：重试无用，直接熔断该域名
            if e.code in (403, 429, 501, 502, 503):
                if host:
                    _mark_dead(host)
                return None
        except Exception:
            pass
        if i == retry:
            return None
        time.sleep(0.8)
    return None


def _prefix(code6):
    if code6.startswith(("60", "68", "90", "51", "56", "58")):
        return "sh" + code6
    if code6.startswith(("00", "30", "12", "15", "16", "18")):
        return "sz" + code6
    return "bj" + code6


_UNI_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "universe_cache.json")


def _universe_sina(mv_lo=2500000000, mv_hi=None, tr_lo=0.3, tr_hi=25.0):
    """降级①：新浪 Market_Center.getHQNodeData 分页枚举全部 A 股（hs_a，
    沪/深/北，含大市值），按 总市值(mktcap,万元)/换手率(turnoverratio,%) 过滤。
    东财被限流时保证大市值票不缺席。GBK→UTF-8 JSON。"""
    out, page = [], 1
    while page <= 120:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1"
               "&node=hs_a&symbol=&_s_r_a=page" % page)
        rows = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=_UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    rows = json.loads(r.read().decode("utf-8", "replace"))
                if rows:
                    break
            except Exception:
                pass
            time.sleep(0.8 + attempt)
        if not rows:
            break
        for x in rows:
            try:
                mv = float(x["mktcap"]) * 1e4          # 万元 → 元
                tr = float(x["turnoverratio"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(x.get("name") or "")
            if mv < mv_lo or (mv_hi is not None and mv >= mv_hi) \
                    or not (tr_lo <= tr < tr_hi):
                continue
            if ST_PAT.search(name):
                continue
            sym = str(x.get("symbol") or "")
            if len(sym) != 8 or not sym[2:].isdigit():
                continue
            out.append({"code": sym, "name": name})
        page += 1
        time.sleep(0.25)
    seen = {}
    for x in out:
        seen[x["code"]] = x
    return list(seen.values())


def _universe_qt(seed_codes):
    """兜底通道：腾讯 qt.gtimg.cn 批量行情（每批 60 只），
    按实时 总市值(idx45,亿)/换手率(idx38)/名称 过滤。GBK 编码。"""
    out = []
    for i in range(0, len(seed_codes), 60):
        batch = seed_codes[i:i + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read().decode("gbk", "replace")
        except Exception:
            continue
        for m in re.finditer(r'v_(\w+)="([^"]*)"', txt):
            f = m.group(2).split("~")
            if len(f) < 46:
                continue
            try:
                tr, mv = float(f[38]), float(f[45]) * 1e8
            except (IndexError, ValueError):
                continue
            name = f[1]
            if mv < 2_500_000_000:          # 只保留下限，市值上不封顶
                continue
            if not (0.3 <= tr < 25.0):
                continue
            if ST_PAT.search(name):
                continue
            out.append({"code": m.group(1), "name": name})
    return out


def universe(mv_lo=2500000000, mv_hi=None, tr_lo=0.3, tr_hi=25.0):
    """全市场粗筛（对齐原 westock filter 口径，2026-11-04 起按用户要求取消市值上限）：
    市值 ≥ mv_lo（mv_hi=None 时上不封顶）、换手率 tr_lo~tr_hi、剔除 ST/退市风险警示。
    主通道：东财 clist 分页；被限流时降级到 新浪 hs_a 全市场列表，
    最后用 腾讯 qt 批量行情验证历史代码清单。
    当日已成功获取过的候选池直接复用（universe_cache.json），避免重复拉取。
    返回 [{"code":"sz300447","name":"全信股份"}]"""
    try:  # 当日缓存命中：直接复用（东财限流时每页重试极慢，必须短路）
        uc = json.load(open(_UNI_CACHE, encoding="utf-8"))
        if (uc.get("date") == datetime.date.today().isoformat()
                and len(uc.get("stocks") or []) >= 1500):
            return uc["stocks"]
    except Exception:
        pass
    pool = _universe_em(mv_lo, mv_hi, tr_lo, tr_hi)
    if len(pool) >= 1500:
        try:
            json.dump({"date": datetime.date.today().isoformat(), "stocks": pool},
                      open(_UNI_CACHE, "w", encoding="utf-8"))
        except Exception:
            pass
        return pool
    # 降级①：东财被限流 → 新浪 hs_a 全市场列表（可枚举全部 A 股，含大市值）
    pool = _universe_sina(mv_lo, mv_hi, tr_lo, tr_hi)
    if len(pool) >= 1500:
        try:
            json.dump({"date": datetime.date.today().isoformat(), "stocks": pool},
                      open(_UNI_CACHE, "w", encoding="utf-8"))
        except Exception:
            pass
        return pool
    # 降级②：用本地缓存的代码清单（昨日全市场池）走腾讯 qt 通道复核
    seed = []
    try:
        uc = json.load(open(_UNI_CACHE, encoding="utf-8"))
        if uc.get("stocks"):
            seed = [x["code"] for x in uc["stocks"]]
    except Exception:
        pass
    if len(seed) < 1500:
        # 从 K 线缓存的历史代码清单补充种子
        try:
            kc = json.load(open(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "kline_cache.json"), encoding="utf-8")).get("data") or {}
            seed = list(dict.fromkeys(seed + list(kc.keys())))
        except Exception:
            pass
    if not seed:
        return []
    pool = _universe_qt(seed)
    if len(pool) >= 1500:
        try:
            json.dump({"date": datetime.date.today().isoformat(), "stocks": pool},
                      open(_UNI_CACHE, "w", encoding="utf-8"))
        except Exception:
            pass
    return pool


def _universe_em(mv_lo=2500000000, mv_hi=None, tr_lo=0.3, tr_hi=25.0):
    """东财 clist 分页粗筛（主通道，见 universe()）。mv_hi=None 表示不设市值上限"""
    out, pn, total = [], 1, None
    while pn <= 250:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:0+t:81,m:1+t:2,m:1+t:23"
               "&fields=f12,f14,f20,f8" % pn)
        rows, ttl = None, None
        for attempt in range(3):          # 单页失败重试（接口会限流）
            s = _get(url, timeout=12)
            if s:
                try:
                    d = json.loads(s).get("data") or {}
                    rows, ttl = d.get("diff") or [], d.get("total")
                    if rows:
                        break
                except Exception:
                    pass
            time.sleep(0.8 + attempt)
        if not rows:
            break
        total = ttl if ttl is not None else total
        for x in rows:
            try:
                mv, tr = float(x["f20"]), float(x["f8"])
            except (TypeError, ValueError):
                continue  # 停牌/无数据
            name = str(x.get("f14") or "")
            if mv < mv_lo or (mv_hi is not None and mv >= mv_hi) \
                    or not (tr_lo <= tr < tr_hi):
                continue
            if ST_PAT.search(name):
                continue
            out.append({"code": _prefix(x["f12"]), "name": name})
        if (total is not None and pn * 100 >= total) or len(rows) < 100:
            break
        pn += 1
        time.sleep(0.25)
    # 分页拉取期间可能有变动，按代码去重
    seen = {}
    for x in out:
        seen[x["code"]] = x
    return list(seen.values())


def _kline_em(code6, days=320, end=""):
    """北交所K线（腾讯 ifzq 不提供北交所历史日K）→ 东财 push2his，前复权。
    东财直接给真实逐bar换手率(f61)。"""
    e = end.replace("-", "") if end else "20500101"
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid=0.{code6}&klt=101&fqt=1&lmt={days}&end={e}"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    s = _get(url, timeout=20)
    if not s:
        return None
    try:
        d = (json.loads(s).get("data") or {}).get("klines") or []
    except Exception:
        return None
    out = []
    for line in d:
        p = line.split(",")
        if len(p) < 11:
            continue
        try:
            out.append({"date": p[0], "open": float(p[1]), "last": float(p[2]),
                        "high": float(p[3]), "low": float(p[4]),
                        "volume": float(p[5]),
                        "exchange": round(float(p[10]), 4)})
        except (IndexError, TypeError, ValueError):
            continue
    if len(out) < 60:
        return None
    out.sort(key=lambda x: x["date"])
    return out


def _kline_sina(code, days=320):
    """兜底通道：新浪日线（腾讯全域名被封时启用）。
    注意：新浪给的是【不复权】价，除权日会有跳空，因子会受影响；
    成交量单位是股，统一 /100 转手以与腾讯口径一致。
    2026-11-04 实测：bj 前缀新浪同样返回日线（bj920002 验证通过），
    东财北交所通道（_kline_em）被限流时用它兜底。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={days}")
    _throttle()
    try:
        req = urllib.request.Request(url, headers=_UA_SINA)
        with urllib.request.urlopen(req, timeout=20) as r:
            s = r.read().decode("utf-8", "replace")
        d = json.loads(s)
    except Exception:
        return None
    if not isinstance(d, list) or len(d) < 60:
        return None
    out = []
    for x in d:
        try:
            out.append({"date": x["day"][:10], "open": float(x["open"]),
                        "last": float(x["close"]), "high": float(x["high"]),
                        "low": float(x["low"]),
                        "volume": float(x["volume"]) / 100.0,
                        "exchange": 0.0})
        except (KeyError, TypeError, ValueError):
            continue
    if len(out) < 60:
        return None
    out.sort(key=lambda z: z["date"])
    _STATS["sina"] += 1
    return out


def kline(code, days=320, end=""):
    """腾讯前复权日K。code 形如 sz300447。返回按日期正序的 bar 列表，
    字段与旧 westock 接口一致：date/open/last/high/low/volume/exchange。
    end 传 YYYY-MM-DD 可取历史截止（补跑基准日用）。北交所走东财通道。"""
    if code.startswith("bj"):
        b = _kline_em(code[2:], days, end or os.environ.get("KLINE_END", ""))
        if b:
            return b
        # 东财北交所通道被限流 → 新浪兜底（补跑历史模式不用新浪，不复权且无法指定截止日）
        if os.environ.get("KLINE_END"):
            _STATS["kline_fail"] += 1
            return None
        b = _kline_sina(code, days)
        if not b:
            _STATS["kline_fail"] += 1
        return b
    end = end or os.environ.get("KLINE_END", "")
    param = f"{code},day,,{end},{days},qfq" if end else f"{code},day,,,{days},qfq"
    q = urllib.parse.quote(param)
    # 2026-11-02：web.ifzq.gtimg.cn 在批量取数后被网关拒绝（HTTP 501，非限流可恢复），
    # 实测 ifzq.gtimg.cn / proxy.finance.qq.com 两个入口返回同样的 qfqday 结构 → 多域名回退。
    s = None
    for base in KLINE_HOSTS:
        if _is_dead(base):
            continue
        s = _get(base + q, timeout=20, host=base)
        if s and '"qfqday"' in s[:4000]:
            break
        s = None
    if not s:
        # 补跑历史模式不要用新浪（不复权 + 无法指定截止日）
        if os.environ.get("KLINE_END"):
            _STATS["kline_fail"] += 1
            return None
        b = _kline_sina(code, days)
        if not b:
            _STATS["kline_fail"] += 1
        return b
    _STATS["tencent"] += 1
    try:
        d = json.loads(s)
    except Exception:
        return None
    node = (d.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not isinstance(rows, list) or len(rows) < 60:
        return None
    # 流通股本（股）≈ qt.流通市值(亿, idx44) / 现价(idx3)；用于推算逐bar换手率
    shares = 0.0
    qt = (node.get("qt") or {}).get(code) or []
    try:
        fmv, price = float(qt[44]), float(qt[3])
        if fmv > 0 and price > 0:
            shares = fmv * 1e8 / price
    except (IndexError, TypeError, ValueError):
        pass
    out = []
    for r in rows:
        try:
            o, c, h, l, v = (float(r[1]), float(r[2]), float(r[3]),
                             float(r[4]), float(r[5]))
        except (IndexError, TypeError, ValueError):
            continue
        # 主板/创业板 volume 单位为手；科创板(68)返回股数——
        # 用"换手率不可能超过100%"自动纠正量纲
        ex = (v * 100.0 / shares * 100.0) if shares else 0.0
        if ex > 100.0 and shares:
            ex = v / shares * 100.0
        out.append({"date": r[0], "open": o, "last": c, "high": h, "low": l,
                    "volume": v, "exchange": round(min(ex, 100.0), 4)})
    if not out:
        return None
    out.sort(key=lambda x: x["date"])
    return out
