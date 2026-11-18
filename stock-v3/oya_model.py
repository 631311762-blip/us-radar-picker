# -*- coding: utf-8 -*-
r"""
奥雅式「试盘→破位→盘中放量突破」主升浪模型 · 正式选股模块（V5 主榜）
================================================================================
2026-11-18 用户授权：禁改文件约束解除，以「奥雅最优(F方案)」为主推送模型，
旧 v3.1 降为参照观察。

形态来源（sz300949 奥雅股份 2026-10-21 实证 + 2026 全市场 191 日回测）：
  横盘箱体 → 放量试盘(压力线=试盘最高) → 破位洗盘 → 盘中放量突破压力线(提示日)
  回测：F 方案 5日收益 +3.61%、胜率 56%、g10 17.2%、命中率 25.3%（旧v3.1: -3.77%/30.3%/9.59%/12.3%）

首日可得的准入条件（T日收盘即可判定）：
  箱体振幅≤28% + 试盘(量比≥2/冲高≥3%/回落≥2%) + 破位(跌破试盘支撑)
  + 提示日(涨幅≥5%/量比≥2/量≥试盘0.9/盘中最高≥压力线0.99/非涨停)
次日量能比 1.0~2.0 在 T+1 才可确认 → 作为「跟踪确认」在次日跟踪段标记，不做首推前置。

返回结构与 每日推荐.build() 完全一致，daily_v3.py 可直接切换主榜来源。
读 kline_cache.json / factor_fit.json，不改任何模型权重 / holdout / 状态文件。
"""
import os, sys, json, datetime
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import features as F
import model2 as M
import 每日推荐 as R          # 复用取数/因子/审计/立信/首推跟踪基建
import lixin_gate

FIT = json.load(open(os.path.join(HERE, "factor_fit.json"), encoding="utf-8"))
W = FIT["weights"]
FACTORS = list(W.keys())
TOT = sum(W.values())

# ── F 方案形态参数（与 backtest_oya.SCHEMES['F_奥雅最优'] 一致）────────────
PARAMS = dict(
    box_len=25, box_amp=28.0,
    probe_look_back=25, probe_look_fwd=45, probe_min_gap=3,
    probe_vr=2.0, probe_rise=3.0, probe_fall=2.0,
    sig_chg=5.0, sig_vr=2.0, vol_vs_probe=0.9,
    break_tol=1.0, require_breakdown=True, break_mode="intraday",
    conf_lo=1.0, conf_hi=2.0,     # 次日跟踪确认区间（T+1 用）
)
MODEL_TAG = "v5-oya"


def volma20(b):
    n = len(b); ma = [0.0] * n
    for j in range(20, n):
        ma[j] = sum(x["volume"] for x in b[j - 20:j]) / 20.0
    return ma


def find_probe(b, i, ma, p):
    """在 i 之前 [5,45] 交易日找最近的放量冲高回落试盘日 → (idx, high, vol, vr)"""
    start = max(p["probe_look_back"], i - p["probe_look_fwd"])
    end = i - p["probe_min_gap"]
    for j in range(end, start - 1, -1):
        if ma[j] <= 0:
            continue
        vr = b[j]["volume"] / ma[j]
        if vr < p["probe_vr"]:
            continue
        rise = (b[j]["high"] / b[j - 1]["last"] - 1) * 100
        fall = (b[j]["high"] - b[j]["last"]) / b[j]["high"] * 100
        if rise >= p["probe_rise"] and fall >= p["probe_fall"]:
            return j, b[j]["high"], b[j]["volume"], round(vr, 2)
    return None


def box_check(b, j, p):
    seg = b[max(0, j - p["box_len"]):j + 1]
    if len(seg) < p["box_len"] * 0.7:
        return False, None
    hi = max(x["high"] for x in seg); lo = min(x["low"] for x in seg)
    avg = sum(x["last"] for x in seg) / len(seg)
    amp = (hi - lo) / avg * 100
    return amp <= p["box_amp"], dict(amp=round(amp, 1), hi=round(hi, 2), lo=round(lo, 2))


def breakdown(b, pday, i, boxlo, p):
    support = max(boxlo, b[pday]["low"])
    if i <= pday + 1:
        return False, None
    low_after = min(b[j]["low"] for j in range(pday + 1, i))
    if low_after < support * (1 - p["break_tol"] / 100.0):
        return True, round((low_after / support - 1) * 100, 1)
    return False, None


def detect(code, b, idx, p, ma=None):
    """判断 b[idx] 是否为奥雅式提示日；返回信号证据 dict 或 None（T日可得字段）"""
    if idx < 60 or idx >= len(b):
        return None
    ma = ma or volma20(b)
    c0 = b[idx]["last"]; prev = b[idx - 1]["last"]
    chg = (c0 / prev - 1) * 100
    vr = b[idx]["volume"] / ma[idx] if ma[idx] else 0
    if chg < p["sig_chg"] or vr < p["sig_vr"]:
        return None
    if chg >= M.board_limit(code) - 0.5:      # 涨停买不进
        return None
    probe = find_probe(b, idx, ma, p)
    if not probe:
        return None
    pday, pressure, pvol, pvr = probe
    if p["break_mode"] == "intraday":
        ok = b[idx]["high"] >= pressure * (1 - p["break_tol"] / 100.0)
    else:
        ok = c0 >= pressure * (1 - p["break_tol"] / 100.0)
    if not ok:
        return None
    if b[idx]["volume"] < pvol * p["vol_vs_probe"]:
        return None
    inbox, box = box_check(b, pday, p)
    if not inbox:
        return None
    bd, bd_pct = breakdown(b, pday, idx, box["lo"], p)
    if p["require_breakdown"] and not bd:
        return None
    return dict(
        oya=1, oya_probe_date=b[pday]["date"], oya_pressure=round(pressure, 2),
        oya_break=round((b[idx]["high"] / pressure - 1) * 100, 2),
        oya_vol_x=round(b[idx]["volume"] / pvol, 2), oya_probe_vr=pvr,
        oya_box_amp=box["amp"], oya_breakdown=bd, oya_bd_pct=bd_pct,
        oya_stop=round(b[idx]["low"] * 0.96, 2),   # 破位线：突破日最低×0.96
        oya_nxt_ratio=None, oya_confirm="待次日确认",
    )


def confirm_next(code, b, idx, p):
    """T+1 确认：次日量比是否在 [conf_lo, conf_hi]（收盘后回填）"""
    if idx + 1 >= len(b) or idx <= 0:
        return None, "数据不足"
    ratio = b[idx + 1]["volume"] / b[idx]["volume"]
    ok = p["conf_lo"] <= ratio <= p["conf_hi"]
    return round(ratio, 2), ("确认通过" if ok else
                             ("放量过猛" if ratio > p["conf_hi"] else "缩量不足"))


def _load_oya_history():
    """已推过的奥雅信号（首推去重）：(date, code) 列表"""
    try:
        db = json.load(open(os.path.join(HERE, "predictions.json"), encoding="utf-8"))
        return [(d.get("date"), d.get("code")) for d in db
                if d.get("model") == MODEL_TAG and d.get("date") and d.get("code")]
    except Exception:
        return []


def _audit(cand):
    """复用现有审计 + 立信离线闸门；返回 (ok, blocked)"""
    import audit_check
    aud = audit_check.main([x["code"] for x in cand], verbose=False)
    blocked = []
    for x in cand:
        x["firm"] = aud.get(x["code"], {}).get("firm", "")
        if (aud.get(x["code"], {}).get("status") == "LIXIN"
                or lixin_gate.is_excluded(x["code"])):
            blocked.append(x)
    ok = [x for x in cand if x not in blocked]
    return ok, blocked


def build(topn=6):
    p = PARAMS
    pool = R.universe()
    name = {x["code"]: x["name"] for x in pool}
    cache, key = R.load_cache()
    today = datetime.date.today().isoformat()
    NOW = datetime.datetime.now()
    # 收盘后缓存必须覆盖当天，否则作废重拉（与 每日推荐.build 同口径）
    if (NOW.hour * 60 + NOW.minute) >= 15 * 60 + 5 and key != today:
        cache = {}
    todo = [x for x in pool if x["code"] not in cache]
    if todo:
        print(f"奥雅模型：K线缓存 {len(cache)} 只，需补拉 {len(todo)} 只...")
        def fetch(x):
            return x["code"], R.kline(x["code"])
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, b in ex.map(fetch, todo):
                if b:
                    cache[c] = b
        last_d = max((v[-1]["date"] for v in cache.values() if v), default=today)
        pc = os.path.join(HERE, "kline_cache.json")
        tmp = pc + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(dict(key=last_d, data=cache), fp)
        os.replace(tmp, pc)
        R.mark_cache(cache, last_d)

    # 计算全市场因子（与 每日推荐 同源）+ 当日横截面打分
    rows = []
    def work(x):
        b = cache.get(x["code"])
        if not b or len(b) < 200:
            return None
        idx = len(b) - 1
        # 盘中当日K线不完整则退一根
        if idx >= 1 and b[-1]["date"] == today and (NOW.hour * 60 + NOW.minute) < 15 * 60 + 5:
            idx -= 1
        f = F.factors_at(b, idx, x["code"])
        if not f:
            return None
        f["code"] = x["code"]; f["name"] = name.get(x["code"], "")
        f["_b"] = b; f["_idx"] = idx
        return f
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(work, pool):
            if r:
                rows.append(r)
    if len(rows) < 1000:
        raise RuntimeError(f"奥雅模型：有效样本仅 {len(rows)} 只，拒绝出榜")
    dc = Counter(x["date"] for x in rows)
    base_date, nmax = dc.most_common(1)[0]
    if nmax < len(rows) * 0.95:
        raise RuntimeError(f"奥雅模型：基准日不一致（{base_date} 仅 {nmax}/{len(rows)}），拒绝出榜")

    maps = {k: sorted(r[k] for r in rows) for k in FACTORS}
    for r in rows:
        r["pct"] = [round(F.percentile(maps[k], r[k]), 4) for k in FACTORS]
        r["score"] = round(sum(W[k] * r["pct"][j] for j, k in enumerate(FACTORS)) / TOT * 100, 2)
    n_universe = len(rows)

    # ── 形态准入：对全市场每只票判定当天是否为奥雅提示日 ──
    hits = []
    for r in rows:
        b = r.pop("_b"); idx = r.pop("_idx")
        try:
            ev = detect(r["code"], b, idx, p)
        except Exception:
            ev = None
        if ev:
            r.update(ev)
            r["_b"] = b; r["_idx"] = idx
            hits.append(r)
    print(f"奥雅形态命中 {len(hits)} 只（基准日 {base_date}）")

    # 首推去重：同一只票 30 交易日内已推过则不再推（转为跟踪）
    hist = _load_oya_history()
    last_push = {}
    for d, c in hist:
        if c and d and (c not in last_push or d > last_push[c]):
            last_push[c] = d
    cal = sorted(set(x["date"] for x in rows))
    di = {d: i for i, d in enumerate(cal)}
    fresh_hits = []
    followed = []
    for r in hits:
        lp = last_push.get(r["code"])
        if lp and lp in di and base_date in di and (di[base_date] - di[lp]) <= 30:
            r["oya_repeat"] = 1
            followed.append(r)
        else:
            fresh_hits.append(r)

    # 立信闸门（全市场名单，命中即剔）
    try:
        fresh_hits, lx = lixin_gate.apply(fresh_hits)
    except Exception:
        lx = []

    # 按打分排序 → 板块配额（主板3 + 创业/科创3）
    fresh_hits.sort(key=lambda x: -x["score"])
    mains = [x for x in fresh_hits if x.get("board") not in ("创业板", "科创板")]
    cybs = [x for x in fresh_hits if x.get("board") in ("创业板", "科创板")]
    final = mains[:3] + cybs[:3]
    if len(final) < topn:
        used = {x["code"] for x in final}
        rest = [x for x in mains[3:] + cybs[3:] if x["code"] not in used]
        final += rest[:topn - len(final)]

    # 审计一票否决（在最终名单上）
    final, blocked = _audit(final)

    # 首推跟踪天数（沿用 predictions 里该票最早入选日）
    try:
        pdb = json.load(open(os.path.join(HERE, "predictions.json"), encoding="utf-8"))
    except Exception:
        pdb = []
    hist_all = {}
    for d in pdb:
        c, dt = d.get("code"), d.get("date", "")
        if c and dt and (c not in hist_all or dt < hist_all[c]):
            hist_all[c] = dt
    for x in final:
        first = hist_all.get(x["code"])
        if not first or first >= base_date:
            x["since"] = dict(first=base_date, days=1, fresh=True)
        else:
            b = cache.get(x["code"]) or []
            dates = [z["date"] for z in b]
            days = (dates.index(base_date) - dates.index(first) + 1
                    if first in dates and base_date in dates else 1)
            x["since"] = dict(first=first, days=max(1, days), fresh=False)
        x["dsc"] = 0          # 提示日 = 突破当天
        x["launch_gain"] = None

    return dict(
        allpicks=final[:], latpicks=final, cybpicks=[], blocked=blocked,
        shape_excluded=[], shape_excluded_total=0,
        lixin_excluded=lx, lixin_excluded_total=len(lx),
        base_date=base_date, universe=n_universe, pool=rows,
        followed=followed[:12], model=MODEL_TAG,
    )


def record(r):
    """写 predictions.json（带 model=v5-oya 标记，供首推去重与结算）"""
    db = []
    f = os.path.join(HERE, "predictions.json")
    if os.path.exists(f):
        try:
            db = json.load(open(f, encoding="utf-8"))
        except Exception:
            db = []
    have = {(d.get("date"), d.get("code"), d.get("model")) for d in db}
    for x in r["latpicks"]:
        kind = "C-创科" if x.get("board") in ("创业板", "科创板") else "B-埋伏"
        pdate = x.get("date") or r["base_date"]     # 该票真实提示日（形态命中当天）
        k = (pdate, x["code"], MODEL_TAG)
        if k in have:
            continue
        db.append(dict(model=MODEL_TAG, kind=kind, date=pdate,
                       code=x["code"], name=x["name"], close=x["close"],
                       score=x["score"], factors=FACTORS, pct=x.get("pct"),
                       dsc=0, launch_gain=None, since=x.get("since"),
                       oya_probe_date=x.get("oya_probe_date"),
                       oya_pressure=x.get("oya_pressure"),
                       oya_break=x.get("oya_break"),
                       oya_vol_x=x.get("oya_vol_x"),
                       oya_breakdown=x.get("oya_breakdown"),
                       oya_stop=x.get("oya_stop")))
    json.dump(db, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    out = dict(base_date=r["base_date"], universe=r["universe"],
               generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               model=MODEL_TAG, PICKS=r["latpicks"], blocked=r["blocked"],
               followed=r.get("followed", []))
    json.dump(out, open(os.path.join(HERE, "今日推荐.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def legacy_ref(rows, topn=3):
    """旧 v3.1 参照：用同一份已算好的 rows，按旧口径(新鲜度分层+打分+buyable)挑 Top N。
    只作对照展示，不进主榜、不结算、不推送为推荐。"""
    try:
        ref = [x for x in rows if R.buyable(x)]
        def tier(x):
            d = x.get("dsc", 99)
            return 0 if d <= 1 else (1 if d <= 3 else 2)
        ref.sort(key=lambda x: (tier(x), -x["score"]))
        # 立信过滤（复用已加载名单）
        ref = [x for x in ref if not lixin_gate.is_excluded(x["code"])]
        mains = [x for x in ref if x.get("board") not in ("创业板", "科创板")]
        cybs = [x for x in ref if x.get("board") in ("创业板", "科创板")]
        out = mains[:2] + cybs[:1]
        if len(out) < topn:
            used = {x["code"] for x in out}
            out += [x for x in ref if x["code"] not in used][:topn - len(out)]
        return out[:topn]
    except Exception:
        return []


def render_legacy_md(ref):
    if not ref:
        return []
    md = ["### 📎 旧模型 v3.1 参照（仅供对照，非今日主推）"]
    for x in ref:
        md.append(f"- {x['name']} `{x['code']}` 分{x['score']:.0f}　"
                  f"启动第{x.get('dsc',99)+1 if x.get('dsc',99)<99 else '?'}天")
    md.append("")
    return md


def render_oya_md(r):
    """当日主推：精简到每只 1 行关键信息（用户 2026-11-18 要求）"""
    md = []
    fp = r.get("latpicks", [])
    md.append("### 🎯 今日主推（奥雅式放量突破·首日推）")
    if not fp:
        md.append("　今日无符合奥雅形态的新提示日（宁缺勿滥，不硬凑）。")
    for i, x in enumerate(fp, 1):
        bd = {"创业板": "创", "科创板": "科"}.get(x.get("board", ""), "主")
        md.append(
            f"{i}. **{x['name']}**[{bd}] `{x['code']}` 分{x['score']:.0f}｜"
            f"收{x['close']} 当日{x['day_chg']:+.1f}%｜"
            f"破{x.get('oya_pressure')}({x.get('oya_break'):+.0f}%) 量{x.get('oya_vol_x')}× "
            f"{'洗盘✓' if x.get('oya_breakdown') else ''}")
    md.append("")
    return md


STOP_FILE = "oya_follow_stop.json"   # 破位停止台账：code -> 首推日（已宣告停止，不再推送）


def _load_stopped():
    try:
        return json.load(open(os.path.join(HERE, STOP_FILE), encoding="utf-8"))
    except Exception:
        return {}


def _save_stopped(d):
    json.dump(d, open(os.path.join(HERE, STOP_FILE), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def _first_picks():
    """从 predictions 取所有 v5-oya 首推（每只票取最早首推日），返回 [{code,name,date,stop,close}]"""
    try:
        db = json.load(open(os.path.join(HERE, "predictions.json"), encoding="utf-8"))
    except Exception:
        return []
    first = {}
    for d in db:
        if d.get("model") != MODEL_TAG or not d.get("code") or not d.get("date"):
            continue
        c, dt = d["code"], d["date"]
        if c not in first or dt < first[c]["date"]:
            first[c] = dict(code=c, name=d.get("name", ""), date=dt,
                            close=d.get("close"), stop=d.get("oya_stop"),
                            pressure=d.get("oya_pressure"))
    return list(first.values())


def _c0_px(x, ret):
    """由首推收盘价与累计涨幅反推当前价"""
    c0 = x["close"] or 0
    return c0 * (1 + ret / 100.0)


def _vol_ma(b, j, n=5):
    """第 j 根之前 n 根（不含 j）的均量"""
    w = b[max(0, j - n):j]
    if not w:
        return 0.0
    return sum(z.get("volume", 0) for z in w) / len(w)


def _bad_signals(b, i, stop):
    """提示日之后逐根扫描，返回最早出现的看跌/终止信号 (日期, 原因, 说明)
       终止信号三类：破位（收盘跌破止损线）/ 放量下跌 / 阴包阳"""
    for j in range(i + 1, len(b)):
        z = b[j]
        prev = b[j - 1] if j > 0 else None
        # 1) 破位：收盘跌破止损线
        if z["last"] < stop:
            return z["date"], "破位", f"收盘{z['last']:.2f}跌破止损{stop}"
        # 2) 放量下跌：跌幅≥5% 且 成交量≥前5日均量×1.5
        if prev:
            vma = _vol_ma(b, j, 5)
            chg = (z["last"] / prev["last"] - 1) * 100 if prev["last"] else 0.0
            if chg <= -5.0 and vma > 0 and z.get("volume", 0) >= vma * 1.5:
                return z["date"], "放量下跌", f"{chg:.1f}%且量{z.get('volume',0):.0f}≥前5日均量×1.5"
        # 3) 阴包阳：当日阴线实体完全吞没前一日阳线实体
        if prev and prev["last"] > prev["open"] and z["last"] < z["open"]:
            if z["open"] >= prev["last"] and z["last"] <= prev["open"]:
                return z["date"], "阴包阳", f"开{z['open']:.2f}收{z['last']:.2f}吞没前日阳线"
    return None, None, None


def render_follow_md(r):
    """历史推荐跟踪（用户 2026-11-18二次修订）：
       · 只跟【当前仍上涨】（累计收益>0）的票，涨不动的不再展示；
       · 终止信号三类：破位 / 放量下跌 / 阴包阳，命中即标注说明、写停止台账，之后不再推送；
       · 已停止的票永久退出跟踪。"""
    picks = _first_picks()
    if not picks:
        return []
    try:
        cache = json.load(open(os.path.join(HERE, "kline_cache.json"), encoding="utf-8"))["data"]
    except Exception:
        return []
    stopped = _load_stopped()
    md = ["### 🔁 历史推荐跟踪（只跟涨的｜破位/放量下跌/阴包阳即停）"]
    active, just_stopped = [], []
    for x in picks:
        code = x["code"]
        if stopped.get(code) == x["date"]:      # 已宣告停止，永久退出
            continue
        b = cache.get(code)
        if not b:
            continue
        idxs = [i for i, z in enumerate(b) if z["date"] == x["date"]]
        if not idxs:
            continue
        i = idxs[0]
        c0 = x["close"] if x["close"] else b[i]["last"]
        stop = x["stop"] if x["stop"] else round(b[i]["low"] * 0.96, 2)
        after = b[i + 1:]
        sd, sreason, sdetail = _bad_signals(b, i, stop)
        last = b[-1]["last"]
        ret = (last / c0 - 1) * 100
        hi = max((z["high"] for z in after), default=c0)
        hi_ret = (hi / c0 - 1) * 100
        days = len(after)
        if sd:
            just_stopped.append((x, ret, hi_ret, stop, days, sd, sreason, sdetail))
        elif ret > 0:
            # 只跟还涨的
            active.append((x, ret, hi_ret, stop, days))

    # 未触发终止信号且仍上涨：一直跟
    for x, ret, hi_ret, stop, days in active:
        flag = "🔥" if ret >= 20 else "↗"
        md.append(f"- {flag} **{x['name']}** `{x['code']}` 首推{x['date']} 第{days+1}日｜"
                  f"现价{_c0_px(x, ret):.2f}　累计 **{ret:+.1f}%**（最高{hi_ret:+.1f}%）｜"
                  f"止损{stop} 仍强势")
    # 刚触发终止信号：说明一次并写入台账
    for x, ret, hi_ret, stop, days, sd, sreason, sdetail in just_stopped:
        md.append(f"- ⛔ **{x['name']}** `{x['code']}` 首推{x['date']}｜"
                  f"{sd} {sreason}（{sdetail}）　跟踪终止，累计{ret:+.1f}%（最高{hi_ret:+.1f}%）")
        stopped[x["code"]] = x["date"]
    if just_stopped:
        _save_stopped(stopped)
    if not active and not just_stopped:
        return []
    md.append("")
    return md
