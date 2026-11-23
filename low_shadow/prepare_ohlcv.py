# -*- coding: utf-8 -*-
"""拉全市场 2026 完整日线 OHLCV 缓存（新浪 HTTP 250根，不复权），供跨模型重算共用。"""
import os, json, time, requests, urllib3
import concurrent.futures as cf
urllib3.disable_warnings()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "low_shadow", "state")
CACHE = os.path.join(OUT, "ohlcv_cache_2026.json")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
     "Referer": "https://finance.sina.com.cn/"}
LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
KL_URL = "http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
KLEN = 250


def _parse(t):
    s = t.strip(); i = s.find("([")
    return json.loads(s[i + 1:s.rfind(")")])


def sina_kline(sym, n=KLEN):
    for _ in range(3):
        try:
            r = requests.get(KL_URL, params={"symbol": sym, "scale": 240, "ma": "no",
                             "datalen": n}, timeout=20, verify=False, headers=H)
            if r.status_code != 200 or len(r.content) < 100:
                time.sleep(0.2); continue
            arr = _parse(r.text)
            if not arr:
                time.sleep(0.2); continue
            bars = []
            for x in arr:
                try:
                    o, h, l, c, v = (float(x["open"]), float(x["high"]), float(x["low"]),
                                     float(x["close"]), float(x["volume"]))
                    if o <= 0 or h <= 0 or c <= 0 or v < 0:
                        continue
                    bars.append([x["day"], round(o,3), round(h,3), round(l,3), round(c,3), int(v)])
                except (KeyError, ValueError, TypeError):
                    pass
            return bars
        except Exception:
            time.sleep(0.2)
    return []


def sina_universe():
    out, seen = [], set()
    for pn in range(1, 100):
        arr = []
        for _ in range(3):
            try:
                r = requests.get(LIST_URL, params={"page": pn, "num": 100, "sort": "symbol",
                                 "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page"},
                                 timeout=20, verify=False, headers=H)
                if r.text.strip().startswith("["):
                    arr = json.loads(r.text)
                break
            except Exception:
                time.sleep(0.3)
        if not arr:
            break
        for x in arr:
            code = str(x.get("code", "")).zfill(6)
            sym = x.get("symbol", "")
            if not code or not sym or code in seen:
                continue
            seen.add(code)
            out.append((code, sym, x.get("name", "")))
        time.sleep(0.04)
    return out


t0 = time.time()
idx = sina_kline("sh000001")
DAYS = [b[0] for b in idx if b[0] >= "2026-01-01"]
uni = sina_universe()
print("交易日 %d | %s→%s | 全市场 %d | %.1fs" %
      (len(DAYS), DAYS[0], DAYS[-1], len(uni), time.time()-t0), flush=True)
syms = [(c, s) for c, s, n in uni]
kl = {}
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    futs = {ex.submit(sina_kline, s): c for c, s in syms}
    for f in cf.as_completed(futs):
        kl[futs[f]] = f.result()
json.dump({"days": DAYS, "uni": uni, "kl": kl},
          open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
print("OHLCV缓存完成 %d 只 | %.1fs | %s" %
      (len(kl), time.time()-t0, CACHE), flush=True)
