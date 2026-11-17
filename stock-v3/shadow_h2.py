# -*- coding: utf-8 -*-
"""
shadow_h2.py —— H2 影子假设跟踪器（涨停基因因子降权）

用途：验证「把 lu10/lu20/shrink 三个涨停基因因子权重减半」是否让模型选出
更好的票。与真实主榜【同池同口径】，唯一变量是权重：
  · control    = 当前真实权重（factor_fit.json, v3.1+evo1113）
  · experiment = 涨停3因子权重×0.5 后重新归一化
两组各自用 feedback.settle_one 同口径结算 g10/hit，各满 60 条后裁定。

全程【只写自己的账本】：hypothesis_h2.json / shadow_h2_log.jsonl /
shadow_h2_db.jsonl。绝不写 factor_fit.json / predictions.json /
feedback_db.jsonl / holdout.json / 任何主榜状态文件。不联网、不改模型。

命令：
  python3 shadow_h2.py            # record + settle + evaluate（供 daily_v3 调用）
  python3 shadow_h2.py record     # 只记当日两组名单
  python3 shadow_h2.py settle     # 只结算
  python3 shadow_h2.py evaluate   # 只评估闸门
  python3 shadow_h2.py report     # 输出 Markdown 复盘段
"""
import os, sys, json, bisect, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WATCH = os.path.join(HERE, "hypothesis_h2.json")
SLOG = os.path.join(HERE, "shadow_h2_log.jsonl")
SDB = os.path.join(HERE, "shadow_h2_db.jsonl")
FIT = os.path.join(HERE, "factor_fit.json")
CACHE = os.path.join(HERE, "kline_cache.json")

DECAY_KEYS = ("lu10", "lu20", "shrink")
DECAY_FACTOR = 0.5
KIND_C = "SHADOW2-C"       # control 组结算标记
KIND_E = "SHADOW2-E"       # experiment 组结算标记


def jload(f, default):
    try:
        with open(f, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def jappend(f, obj):
    with open(f, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(obj, ensure_ascii=False) + "\n")


def watch():
    w = jload(WATCH, None)
    if not w:
        raise RuntimeError("缺少 hypothesis_h2.json")
    return w


def save_watch(w):
    with open(WATCH, "w", encoding="utf-8") as fp:
        json.dump(w, fp, ensure_ascii=False, indent=1)


def two_weights():
    """返回 (control权重, experiment权重, 因子顺序)。减半后重新归一化。"""
    fit = jload(FIT, None)
    if not fit:
        raise RuntimeError("缺少 factor_fit.json")
    wc = dict(fit["weights"])
    factors = list(wc.keys())
    we = {k: (v * DECAY_FACTOR if k in DECAY_KEYS else v) for k, v in wc.items()}
    return wc, we, factors


def score_of(r, w, factors):
    tot = sum(w.values())
    return round(sum(w[k] * r["pct"][j] for j, k in enumerate(factors)) / tot * 100, 2)


def _tier(x):
    d = x.get("dsc", 99)
    return 0 if d <= 1 else (1 if d <= 3 else 2)


def _board(code):
    return "创业板" if code.startswith(("sz30", "sz301")) else (
        "科创板" if code.startswith("sh68") else "主板")


def _buyable(r):
    """与 每日推荐.buyable 同口径：按板块涨停幅度剔除逼近涨停的。"""
    import model2 as M
    lim = M.board_limit(r["code"])
    cap = 8.0 if lim < 15 else (15.0 if lim < 25 else 22.0)
    return r["day_chg"] < cap


def _select(rows, w, factors, topn=6):
    """与 build() 完全一致的选票链：打分→闸门已在外层做→buyable→新鲜度分层→板块配额。"""
    for r in rows:
        r["_s2"] = score_of(r, w, factors)
    latent = [r for r in rows if _buyable(r)]
    fresh = sorted(latent, key=lambda x: (_tier(x), -x["_s2"]))
    mains = [x for x in fresh if x.get("board") not in ("创业板", "科创板")]
    cybs = [x for x in fresh if x.get("board") in ("创业板", "科创板")]
    final = mains[:3] + cybs[:3]
    if len(final) < topn:
        used = {x["code"] for x in final}
        rest = [x for x in mains[3:] + cybs[3:] if x["code"] not in used]
        final += rest[:topn - len(final)]

    def pack(x):
        return dict(code=x["code"], name=x.get("name", ""), board=x.get("board", ""),
                    score=round(x["_s2"], 2), pct=x.get("pct"),
                    close=x["close"], dsc=x.get("dsc", 99),
                    day_chg=x["day_chg"], brk20=x.get("brk20"),
                    limitup=bool(x["day_chg"] >= (19.8 if x.get("board") in ("创业板", "科创板") else 9.8)))
    return [pack(x) for x in final]


# ────────────────────────────── 记录 ──────────────────────────────
def record():
    """用缓存全市场算因子→pct→两组打分，各自选票，记一组对照名单。"""
    import features as F
    import model2 as M
    wc, we, factors = two_weights()
    k = jload(CACHE, None)
    if not k or not k.get("data"):
        return ["[影子H2] 缺少 kline_cache.json，跳过记录"]
    data = k["data"]

    # 基准日 = 缓存内多数票的最后交易日（避免个别停牌票干扰）
    from collections import Counter
    last_days = Counter(b[-1]["date"] for b in data.values() if b)
    if not last_days:
        return ["[影子H2] 缓存无有效K线，跳过记录"]
    base = last_days.most_common(1)[0][0]

    # 幂等：同基准日不重复记
    for line in (open(SLOG, encoding="utf-8").read().splitlines()
                 if os.path.exists(SLOG) else []):
        try:
            if json.loads(line).get("date") == base:
                return [f"[影子H2] {base} 已记录过，跳过"]
        except Exception:
            pass

    rows = []
    for code, bars in data.items():
        if len(bars) < 200:
            continue
        hist = [b for b in bars if b["date"] <= base]
        if not hist or hist[-1]["date"] != base:
            continue
        # 52周活跃度（与 build 一致）
        w250 = hist[-250:]
        try:
            if max(x["high"] for x in w250) <= min(x["low"] for x in w250) * 1.5:
                continue
        except Exception:
            pass
        f = F.factors_at(hist, len(hist) - 1, code)
        if not f:
            continue
        f["code"] = code
        f["board"] = M.board_name(code)
        rows.append(f)
    if len(rows) < 1000:
        return [f"[影子H2] 有效样本仅 {len(rows)}，跳过记录"]
    maps = {kk: sorted(r[kk] for r in rows) for kk in factors}
    for r in rows:
        r["pct"] = [round(bisect.bisect_left(maps[kk], r[kk]) / len(maps[kk]), 4)
                    for kk in factors]

    # 闸门（与 build 同源，离线、不联网）
    try:
        import lixin_gate
        rows, _ = lixin_gate.apply(rows)
    except Exception as e:
        return [f"[影子H2] 立信闸门异常，跳过记录：{e}"]
    try:
        import shape_gate
        rows, _ = shape_gate.apply(rows, data, base)
    except Exception as e:
        return [f"[影子H2] 形态闸门异常，跳过记录：{e}"]

    ctrl = _select(rows, wc, factors)
    exp = _select(rows, we, factors)
    jappend(SLOG, dict(date=base, n_pool=len(rows),
                       control=ctrl, experiment=exp,
                       w_control=wc, w_experiment=we,
                       ts=datetime.datetime.now().isoformat(timespec="seconds")))
    w = watch()
    w["shadow"]["n_days_recorded"] = w["shadow"].get("n_days_recorded", 0) + 1
    w["shadow"]["groups"]["control"]["n_recorded"] = w["shadow"]["groups"]["control"].get("n_recorded", 0) + len(ctrl)
    w["shadow"]["groups"]["experiment"]["n_recorded"] = w["shadow"]["groups"]["experiment"].get("n_recorded", 0) + len(exp)
    save_watch(w)
    cset = {p["code"] for p in ctrl}
    eset = {p["code"] for p in exp}
    return [f"[影子H2] {base} 池 {len(rows)} 只 → control/experiment 各记 {len(ctrl)}/{len(exp)} 只"
            f"（重叠 {len(cset & eset)} 只，差异 {cset ^ eset}）"]


# ────────────────────────────── 结算 ──────────────────────────────
def _rows(f):
    if not os.path.exists(f):
        return []
    out = []
    for line in open(f, encoding="utf-8"):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def settle():
    """两组各自用 feedback.settle_one 同口径结算，写入 shadow_h2_db.jsonl。"""
    import feedback
    from concurrent.futures import ThreadPoolExecutor
    if not os.path.exists(SLOG):
        return ["[影子H2] 无记账，跳过结算"]
    db = _rows(SDB)
    done = {(r["date"], r["code"], r.get("kind")): r for r in db}
    todo, seen = [], set()
    for line in open(SLOG, encoding="utf-8"):
        try:
            day = json.loads(line)
        except Exception:
            continue
        for gk, kind in (("control", KIND_C), ("experiment", KIND_E)):
            for p in day.get(gk, []):
                key = (day["date"], p["code"], kind)
                if key in done or key in seen:
                    continue
                seen.add(key)
                todo.append(dict(code=p["code"], name=p.get("name", ""), kind=kind,
                                 date=day["date"], score=p.get("score"), pct=p.get("pct")))
    # 已满 10 交易日但 launch 未补全的，到期重试补全
    pend = [r for r in db if r.get("full") == 0]
    if not todo and not pend:
        return [f"[影子H2] 无待结算（库内 {len(db)} 条）"]
    jobs = todo + [dict(code=r["code"], name=r["name"], kind=r["kind"],
                        date=r["date"], score=r.get("score"), pct=r.get("pct"))
                   for r in pend]
    got = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(feedback.settle_one, jobs):
            if r:
                got.append(r)
    n_new = n_fix = 0
    bykey = {(r["date"], r["code"], r.get("kind")): r for r in got}
    for key, r in bykey.items():
        if key in done:
            old = done[key]
            if r.get("full") and old.get("full") != 1:
                old["launch"] = r.get("launch")
                old["full"] = 1
                n_fix += 1
        elif r.get("settled"):
            jappend(SDB, r)
            done[key] = r
            n_new += 1
    if n_fix:
        with open(SDB, "w", encoding="utf-8") as fp:
            for r in done.values():
                fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    return [f"[影子H2] 结算尝试 {len(jobs)} 条：新入库 {n_new}（累计 {len(done)}），补全 launch {n_fix} 条"]


# ────────────────────────────── 评估闸门 ──────────────────────────────
def evaluate():
    w = watch()
    db = _rows(SDB)
    need = w["protocol"]["settle_need"]

    def stat(kind):
        s = [r for r in db if r.get("kind") == kind and r.get("settled") and r.get("g10") is not None]
        if not s:
            return dict(n=0, g10=None, hit=None), 0
        g = [r["g10"] for r in s]
        return (dict(n=len(s), g10=round(sum(g) / len(g), 2),
                     hit=round(sum(1 for x in g if x >= 20.0) / len(g) * 100, 1)), len(s))

    sc, nc = stat(KIND_C)
    se, ne = stat(KIND_E)
    w["shadow"]["groups"]["control"]["n_settled"] = nc
    w["shadow"]["groups"]["control"]["stat"] = sc if nc else None
    w["shadow"]["groups"]["experiment"]["n_settled"] = ne
    w["shadow"]["groups"]["experiment"]["stat"] = se if ne else None
    msg = [f"[影子H2] 结算进度 control {nc}/{need}、experiment {ne}/{need} 条"]
    if ne >= need and nc >= need:
        ok = se["g10"] >= sc["g10"] and se["hit"] >= sc["hit"]
        w["status"] = "promote_ready" if ok else "rejected"
        w["evaluated"] = datetime.datetime.now().isoformat(timespec="seconds")
        w["verdict"] = dict(control=sc, experiment=se,
                            gate="试验组 g10 均值与命中率双双不低于对照组")
        msg.append(f"[影子H2] 闸门裁定：试验 g10 {se['g10']}%/命中 {se['hit']}% vs "
                   f"对照 {sc['g10']}%/命中 {sc['hit']}% → {w['status']}")
    elif ne >= need:
        w["status"] = "await_control"
        msg.append("[影子H2] 试验组已满，对照组未满，继续等待")
    save_watch(w)
    return msg, w


# ────────────────────────────── 报告 ──────────────────────────────
def report_md(w=None):
    w = w or watch()
    g = w["shadow"]["groups"]
    sc, se = g["control"]["stat"], g["experiment"]["stat"]
    nc, ne = g["control"]["n_settled"], g["experiment"]["n_settled"]
    icon = {"watching": "👀", "await_control": "⏳", "promote_ready": "✅", "rejected": "❌"}.get(w["status"], "👀")
    lines = [f"### {icon} 影子假设 H2（{w['name']}）｜状态 {w['status']}"]
    lines.append("　内容：涨停基因三因子 lu10/lu20/shrink 权重减半、其余不变重新归一化；"
                 "与主榜同池同口径，两组各自记账结算，不碰 factor_fit.json。")
    lines.append(f"　进度：control {nc}/60、experiment {ne}/60 条结算；"
                 f"已记 {w['shadow'].get('n_days_recorded', 0)} 个交易日。")
    if sc and se:
        lines.append(f"　对照：control g10 {sc['g10']}%/命中 {sc['hit']}% ｜ "
                     f"experiment g10 {se['g10']}%/命中 {se['hit']}%")
    v = w.get("verdict")
    if v:
        lines.append(f"　裁定 **{w['status']}**（试验组需双双不低于对照组）。")
    return lines


def run():
    msgs = []
    msgs += record()
    msgs += settle()
    ev, w = evaluate()
    msgs += ev
    return dict(msg=msgs, md=report_md(w), watch=w)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "record":
        out = record()
    elif cmd == "settle":
        out = settle()
    elif cmd == "evaluate":
        out, w = evaluate()
    elif cmd == "report":
        print("\n".join(report_md()))
        sys.exit(0)
    else:
        out = run()["msg"]
    print("\n".join(out))
