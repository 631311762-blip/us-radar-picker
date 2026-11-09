# -*- coding: utf-8 -*-
r"""
feedback.py —— 主升浪模型 v3.1 的「每日回溯 + 自我进化」引擎
================================================================================
★ 为什么分四层，而不是「拿昨天的涨跌直接改权重」
--------------------------------------------------------------------------------
每日只推 18 条，且模型里 chg5/vr5 本身就是动量因子。如果拿 T+1 的涨跌去改权重，
模型会立刻奖励"昨天涨得凶"的因子 → 明天推更强的追涨票 → 短期看着准、
实盘却是在高位接盘，几周内自我强化到崩盘。这是动量类模型最常见的死法。
所以这里严格分层：

  【第1层 T+1 观察】  每天看昨日推荐的当日表现、是否冲高、是否触发离场
                      → 只进报告，不改权重（1 天数据信息量≈0）
  【第2层 T+11 结算】 推荐后满 11 个交易日，结算 10 日最大浮盈 g10 / 最大回撤 d10
                      → 判定 HIT（g10≥20%）/ MISS，写入 feedback_db.jsonl
                      （launch「5日内放量启动」需 T+17，届时自动补全）
  【第3层 因子归因】  比较 HIT 组与 MISS 组的因子百分位分布，算出各因子判别力
                      （Cohen's d），找出"到底是哪几个因子把票选错了"
  【第4层 有界进化】  仅在【新增结算样本 ≥60 条】且【距上次更新 ≥5 个交易日】时触发；
                      权重单次变动限制在 ±12% 以内；
                      新旧权重都要在 holdout A 段（2.5 万条、模型从未见过的测试期样本）
                      上跑一遍，新权重退化超 3% 或无实质改善 → 放弃并回滚。
                      全过程写入 factor_fit_history.jsonl，可追溯、可回滚。

用法：
  python feedback.py              # 完整跑一遍（观察+结算+归因+条件进化）
  python feedback.py --report     # 只出报告，绝不改权重
  python feedback.py --force      # 忽略样本量门槛，强制尝试进化（人工研判时用）
  python feedback.py --rollback   # 回滚到上一版权重
  python feedback.py --history    # 查看权重变更历史
================================================================================
"""
import json, os, sys, math, argparse, datetime, shutil
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import 每日推荐 as R
import model2 as M

PREDS = os.path.join(HERE, "predictions.json")
DB = os.path.join(HERE, "feedback_db.jsonl")
HIST = os.path.join(HERE, "factor_fit_history.jsonl")
FIT = os.path.join(HERE, "factor_fit.json")
FIT_BAK = os.path.join(HERE, "factor_fit.backup.json")
HOLDOUT = os.path.join(HERE, "holdout.json")
STATE = os.path.join(HERE, "feedback_state.json")

HIT_G10 = 20.0        # 命中线：10 日内最大浮盈 ≥20%
SETTLE_DAYS = 11      # 出 g10/d10 需要的交易日
LAUNCH_DAYS = 17      # 出 launch 需要的交易日（含后续 11 日确认）
MIN_NEW = 60          # 触发进化的最小新增结算样本
MIN_GAP = 5           # 两次进化之间至少间隔 5 个交易日
MAX_STEP = 0.12       # 单个因子权重单次最大变动幅度
DEGRADE_TOL = 0.97    # holdout 上退化超过 3% → 放弃
IMPROVE_MIN = 1.005   # 提升不足 0.5% → 视为噪声，不折腾
TOPN_EVAL = 6         # holdout 验证口径：每天选 6 只（与实盘一致）


# ────────────────────────────── 基础 IO ──────────────────────────────
def jload(f, default):
    if not os.path.exists(f):
        return default
    try:
        return json.load(open(f, encoding="utf-8"))
    except Exception:
        return default


def jsave(f, obj):
    json.dump(obj, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def jappend(f, obj):
    with open(f, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(obj, ensure_ascii=False) + "\n")


def get_bars(code):
    """优先命中当日K线缓存（秒级），缓存没有再走网络"""
    cache, _ = R.load_cache()
    b = cache.get(code)
    if b and len(b) >= 200:
        return sorted(b, key=lambda x: x["date"])
    return R.kline(code, 400)


def fmt_pct(x):
    return ("+" if x >= 0 else "") + f"{x:.2f}%"


# ────────────────────────────── 第2层：结算 ──────────────────────────────
def settle_one(p):
    """结算一条推荐：按与回测 diagnose2.py 完全一致的口径算 g10/d10/launch"""
    b = get_bars(p["code"])
    if not b:
        return None
    dates = [x["date"] for x in b]
    if p["date"] not in dates:
        return None
    i = dates.index(p["date"])
    c = [x["last"] for x in b]
    h = [x["high"] for x in b]
    l = [x["low"] for x in b]
    v = [x["volume"] for x in b]
    n = len(b)
    c0 = c[i]

    out = dict(date=p["date"], code=p["code"], name=p["name"], kind=p["kind"],
               score=p["score"], close=c0, pct=p.get("pct"),
               settled_at=datetime.date.today().isoformat())
    # g10 / d10：需要推荐日之后 10 个完整交易日
    if n - 1 - i >= SETTLE_DAYS - 1:
        seg = b[i + 1:i + 1 + 10]
        out["g10"] = round((max(x["high"] for x in seg) / c0 - 1) * 100, 2)
        out["d10"] = round((min(x["low"] for x in seg) / c0 - 1) * 100, 2)
        out["g5"] = round((max(x["high"] for x in b[i + 1:i + 6]) / c0 - 1) * 100, 2)
        out["hit"] = 1 if out["g10"] >= HIT_G10 else 0
        out["settled"] = 1
    else:
        out["settled"] = 0
        out["g10"] = None
        out["d10"] = None
    # launch：与回测同口径 —— 未来5日内某日放量(量比≥1.8)且涨≥5%，
    # 且自该日起 11 日内最高价高出其收盘 20%
    if n - 1 - i >= LAUNCH_DAYS - 1:
        launch = 0
        for j in range(i + 1, min(i + 6, n - 1)):
            v20j = sum(v[j - 20:j]) / 20
            vr = v[j] / v20j if v20j else 0
            if vr >= 1.8 and (c[j] / c[j - 1] - 1) * 100 >= 5.0:
                if max(h[j:j + 11]) / c[j] - 1 >= 0.20:
                    launch = 1
                    break
        out["launch"] = launch
        out["full"] = 1
    else:
        out["launch"] = None
        out["full"] = 0
    return out


def settle(preds, done_keys):
    """对未结算的推荐做结算，返回新增样本"""
    todo = [p for p in preds
            if (p["date"], p["code"], p["kind"]) not in done_keys]
    if not todo:
        return []
    got = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(settle_one, todo):
            if r:
                got.append(r)
    new = []
    for r in got:
        k = (r["date"], r["code"], r["kind"])
        if r.get("settled"):
            if k not in done_keys:
                jappend(DB, r)
                done_keys.add(k)
                new.append(r)
    return new


# ────────────────────────────── 第1层：T+1 观察 ──────────────────────────────
def observe(preds, last_date):
    """昨日（最近一次）推荐的当下表现：每日跟踪段用"""
    cur = [p for p in preds if p["date"] == last_date]
    if not cur:
        return []
    codes = sorted({p["code"] for p in cur})
    bars = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, b in zip(codes, ex.map(get_bars, codes)):
            if b:
                bars[c] = b
    # 同一只票可能同时进 A/B/C 榜，这里合并展示
    merged = {}
    for p in cur:
        m = merged.setdefault(p["code"], dict(p, kinds=[]))
        if p["kind"] not in m["kinds"]:
            m["kinds"].append(p["kind"])
    out = []
    for code, p in merged.items():
        b = bars.get(code)
        if not b:
            continue
        dates = [x["date"] for x in b]
        if p["date"] not in dates:
            continue
        i = dates.index(p["date"])
        fut = b[i + 1:]
        if not fut:
            continue
        c0 = p["close"]
        now = fut[-1]["last"]
        hi = max(x["high"] for x in fut)
        lo = min(x["low"] for x in fut)
        # 离场规则：跌破推荐日前 10 日最低价止损；浮盈达 20% 分批了结
        stop = min(x["low"] for x in b[max(0, i - 9):i + 1])
        st = "持有"
        if lo <= stop:
            st = "已触发止损"
        if hi / c0 - 1 >= 0.20:
            st = "已触发止盈" if st == "持有" else st + "·曾达止盈"
        out.append(dict(code=code, name=p["name"], kinds=p["kinds"],
                        close=c0, now=now, days=len(fut),
                        gain=(now / c0 - 1) * 100, best=(hi / c0 - 1) * 100,
                        worst=(lo / c0 - 1) * 100, status=st,
                        stop=round(stop, 2)))
    out.sort(key=lambda x: -x["gain"])
    return out


# ────────────────────────────── 第3层：因子归因 ──────────────────────────────
def attribute(db, factors):
    """HIT 组 vs MISS 组的因子百分位均值差（标准化为 Cohen's d）"""
    hit = [r for r in db if r.get("hit") == 1 and r.get("pct")]
    mis = [r for r in db if r.get("hit") == 0 and r.get("pct")]
    if len(hit) < 8 or len(mis) < 8:
        return None
    res = []
    for j, k in enumerate(factors):
        a = [r["pct"][j] for r in hit]
        b = [r["pct"][j] for r in mis]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        va = sum((x - ma) ** 2 for x in a) / max(1, len(a) - 1)
        vb = sum((x - mb) ** 2 for x in b) / max(1, len(b) - 1)
        sd = math.sqrt((va + vb) / 2) or 1e-9
        res.append(dict(f=k, hit=ma, mis=mb, diff=ma - mb, d=(ma - mb) / sd))
    res.sort(key=lambda x: -x["d"])
    return res


# ────────────────────────────── 第4层：holdout 闸门 ──────────────────────────────
def holdout_eval(w, seg, factors):
    """在固定验证段上按权重打分、每天选 Top6，返回 (启动率, 平均10日最大浮盈)"""
    n_launch = n_hit = n_all = 0
    gsum = 0.0
    tot = sum(w)
    if tot <= 0:
        return (0.0, 0.0)
    for day in seg:
        p, y, g = day["p"], day["y"], day["g"]
        idx = sorted(range(len(y)),
                     key=lambda t: -sum(w[j] * p[t][j] for j in range(len(w))))
        for t in idx[:TOPN_EVAL]:
            n_all += 1
            n_launch += y[t]
            n_hit += 1 if g[t] >= HIT_G10 else 0
            gsum += g[t]
    if not n_all:
        return (0.0, 0.0)
    return (n_launch / n_all * 100, gsum / n_all)


def try_evolve(db, factors, W, hold, state, force=False):
    """有界权重更新。返回 (结果dict, 新权重 or None)"""
    info = dict(triggered=False, reason="", before=None, after=None,
                base=None, cand=None, applied=False)
    n_new = state.get("n_new_since_evolve", 0)
    if not force and n_new < MIN_NEW:
        info["reason"] = (f"新增结算样本 {n_new} 条，未达门槛 {MIN_NEW} 条"
                          f"（还差 {MIN_NEW - n_new} 条）")
        return info, None
    if not force and state.get("days_since_evolve", 99) < MIN_GAP:
        info["reason"] = f"距上次更新仅 {state['days_since_evolve']} 个交易日"
        return info, None
    att = attribute(db, factors)
    if not att:
        info["reason"] = "可归因样本不足（HIT/MISS 各需 ≥8 条）"
        return info, None

    base = holdout_eval([W[k] for k in factors], hold["A"], factors)
    info["triggered"] = True
    info["base"] = list(base)

    best = None
    for eta in (0.05, 0.10, 0.20):
        cand = []
        for k in factors:
            d = next(x["d"] for x in att if x["f"] == k)
            step = eta * max(-1.0, min(1.0, d))
            w0 = W[k]
            w1 = w0 * (1 + step)
            w1 = max(w0 * (1 - MAX_STEP), min(w0 * (1 + MAX_STEP), w1))
            cand.append(round(w1, 4))
        sc = holdout_eval(cand, hold["A"], factors)
        # 先比启动率，再比平均浮盈
        if best is None or sc > best[1]:
            best = (cand, sc, eta)
    cand, sc, eta = best
    info["cand"] = list(sc)
    info["eta"] = eta
    info["before"] = {k: W[k] for k in factors}
    info["after"] = {k: cand[j] for j, k in enumerate(factors)}

    if sc[0] < base[0] * DEGRADE_TOL:
        info["reason"] = (f"候选权重在 holdout 上退化：启动率 {sc[0]:.2f}% < "
                          f"现权重 {base[0]:.2f}%×{DEGRADE_TOL}，已放弃")
        return info, None
    if sc[0] < base[0] * IMPROVE_MIN and sc[1] < base[1]:
        info["reason"] = (f"提升不显著（{sc[0]:.2f}% vs {base[0]:.2f}%），"
                          f"属噪声，不动权重避免抖动")
        return info, None
    info["applied"] = True
    info["reason"] = (f"holdout 启动率 {base[0]:.2f}% → {sc[0]:.2f}%，"
                      f"平均10日最大浮盈 {base[1]:.2f}% → {sc[1]:.2f}%")
    return info, cand


# ────────────────────────────── 报告生成 ──────────────────────────────
def track_md(obs, last_date):
    """昨日推荐跟踪段"""
    if not obs:
        return []
    md = [f"### 📌 昨日（{last_date}）推荐跟踪"]
    for o in obs:
        kinds = "/".join(k.replace("-", "") for k in o["kinds"])
        arrow = "🔴" if o["gain"] >= 0 else "🟢"
        md.append(f"- {arrow} **{o['name']}** `{o['code']}`〔{kinds}〕"
                  f"　{len(o['days']) if isinstance(o['days'], list) else o['days']}日 "
                  f"**{fmt_pct(o['gain'])}**｜最高 {fmt_pct(o['best'])}｜最低 {fmt_pct(o['worst'])}"
                  f"｜{o['status']}")
    return md


def review_md(db, att, evo, state, hold_stats):
    """模型复盘段"""
    md = ["### 🔬 模型复盘（自我进化）"]
    done = [r for r in db if r.get("settled")]
    if not done:
        n_pend = len([r for r in db if not r.get("settled")])
        md.append(f"　已结算样本 0 条（{n_pend} 条仍在观察期，推荐满 11 个交易日后结算）。")
        md.append(f"　达到 {MIN_NEW} 条后才会触发权重重估——宁可慢，也不拿几天的噪声改模型。")
        return md
    hit = sum(1 for r in done if r.get("hit"))
    n = len(done)
    g = sum(r.get("g10", 0) for r in done) / n
    d = sum(r.get("d10", 0) for r in done) / n
    md.append(f"　已结算 **{n}** 条｜10日浮盈≥20% 命中 **{hit}** 条（**{hit/n*100:.1f}%**）"
              f"｜平均最大浮盈 {g:.1f}%｜平均最大回撤 {d:.1f}%")
    if hold_stats:
        md.append(f"　参照：holdout 测试期该权重下命中率 {hold_stats[0]:.1f}%，"
                  f"随机基准约 {hold_stats[1]:.1f}%")
    if att:
        top = [x for x in att if x["d"] > 0][:3]
        bot = [x for x in att if x["d"] < 0][-3:]
        if top:
            md.append("　**判对时更强**：" + "、".join(
                f"{x['f']}(d={x['d']:+.2f})" for x in top))
        if bot:
            md.append("　**判错时偏高**：" + "、".join(
                f"{x['f']}(d={x['d']:+.2f})" for x in bot))
    if evo:
        if evo.get("applied"):
            md.append(f"　⚙️ **权重已更新**：{evo['reason']}")
        elif evo.get("triggered"):
            md.append(f"　⚙️ 已评估候选权重，{evo['reason']}")
        else:
            md.append(f"　⚙️ 本次未触发更新：{evo['reason']}")
    return md


# ────────────────────────────── 主流程 ──────────────────────────────
def run(report_only=False, force=False):
    res = dict(ok=True, changed=False, evo=None, n_new=0, msg=[])
    fit = jload(FIT, None)
    if not fit:
        res["ok"] = False
        res["msg"].append("缺少 factor_fit.json")
        return res
    factors = list(fit["weights"].keys())
    W = fit["weights"]
    hold = jload(HOLDOUT, None)
    if not hold:
        res["msg"].append("缺少 holdout.json，跳过权重更新闸门")

    preds = jload(PREDS, [])
    state = jload(STATE, dict(n_new_since_evolve=0, days_since_evolve=99,
                              last_evolve=None))
    # 已结算索引
    done_keys = set()
    db = []
    if os.path.exists(DB):
        for line in open(DB, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            db.append(r)
            done_keys.add((r["date"], r["code"], r["kind"]))

    # ── 第2层 结算
    before = len(db)
    new = settle(preds, done_keys)
    db += new
    res["n_new"] = len(new)
    if new:
        res["msg"].append(f"新增结算 {len(new)} 条（累计 {len(db)} 条）")
    # launch 尚未到期的，尝试补全
    pend = [r for r in db if r.get("full") == 0]
    if pend:
        got = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(settle_one, [dict(p, kind=p["kind"]) for p in pend]):
                if r and r.get("full"):
                    got.append(r)
        if got:
            m = {(r["date"], r["code"], r["kind"]): r["launch"] for r in got}
            for r in db:
                k = (r["date"], r["code"], r["kind"])
                if k in m and r.get("launch") is None:
                    r["launch"] = m[k]
                    r["full"] = 1
            with open(DB, "w", encoding="utf-8") as fp:
                for r in db:
                    fp.write(json.dumps(r, ensure_ascii=False) + "\n")
            res["msg"].append(f"补全 {len(got)} 条的「5日启动」判定")

    # ── 第1层 观察
    last_date = max([p["date"] for p in preds], default="")
    obs = observe(preds, last_date) if preds else []
    res["track"] = obs
    res["last_date"] = last_date

    # ── 第3层 归因
    att = attribute(db, factors)
    res["att"] = att

    # ── 第4层 进化
    evo = None
    hold_stats = None
    if hold and not report_only:
        state["n_new_since_evolve"] = state.get("n_new_since_evolve", 0) + len(new)
        state["days_since_evolve"] = state.get("days_since_evolve", 99) + 1
        base_l = holdout_eval([W[k] for k in factors], hold["A"], factors)
        rand = sum(sum(s["y"]) for s in hold["A"]) / max(1, sum(len(s["y"]) for s in hold["A"])) * 100
        hold_stats = (base_l[0], rand)
        evo, cand = try_evolve(db, factors, W, hold, state, force=force)
        if cand:
            shutil.copy(FIT, FIT_BAK)
            old = dict(fit)
            fit["weights"] = {k: cand[j] for j, k in enumerate(factors)}
            fit["evolved"] = dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                                  from_ver=old.get("ver", "v3.1"),
                                  samples=len([r for r in db if r.get("settled")]))
            fit["ver"] = "v3.1+evo" + datetime.date.today().strftime("%m%d")
            jsave(FIT, fit)
            jappend(HIST, dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                               reason=evo["reason"], eta=evo.get("eta"),
                               before=evo["before"], after=evo["after"],
                               base=evo["base"], cand=evo["cand"],
                               n_samples=len([r for r in db if r.get("settled")]),
                               att=[dict(x) for x in (att or [])]))
            state.update(n_new_since_evolve=0, days_since_evolve=0,
                         last_evolve=datetime.date.today().isoformat())
            res["changed"] = True
            res["msg"].append("权重已更新：" + evo["reason"])
        else:
            res["msg"].append("权重未变：" + (evo or {}).get("reason", ""))
    elif report_only:
        res["msg"].append("[--report] 只读模式，不修改权重")
    jsave(STATE, state)
    res["evo"] = evo
    res["hold_stats"] = hold_stats
    res["db"] = db
    res["factors"] = factors
    return res


def rollback():
    if not os.path.exists(HIST):
        print("没有可回滚的历史")
        return
    lines = [json.loads(x) for x in open(HIST, encoding="utf-8") if x.strip()]
    if not lines:
        print("历史为空")
        return
    last = lines[-1]
    fit = jload(FIT, {})
    fit["weights"] = last["before"]
    fit["ver"] = "回滚至" + (last["ts"][:10])
    jsave(FIT, fit)
    with open(HIST, "w", encoding="utf-8") as fp:
        for x in lines[:-1]:
            fp.write(json.dumps(x, ensure_ascii=False) + "\n")
    print("已回滚到", last["ts"], "之前的权重：")
    for k, v in last["before"].items():
        print(f"  {k:<10}{v}")


def show_history():
    if not os.path.exists(HIST):
        print("尚无权重变更记录")
        return
    for x in [json.loads(l) for l in open(HIST, encoding="utf-8") if l.strip()]:
        print(f"\n[{x['ts']}] 样本 {x.get('n_samples')} 条  eta={x.get('eta')}")
        print("  " + x.get("reason", ""))
        b, a = x.get("before", {}), x.get("after", {})
        for k in b:
            print(f"    {k:<10}{b[k]:>7.3f} → {a.get(k, 0):>7.3f}"
                  f"  ({(a.get(k,0)/b[k]-1)*100:+.1f}%)" if b[k] else "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="主升浪模型回溯与进化引擎")
    ap.add_argument("--report", action="store_true", help="只出报告不改权重")
    ap.add_argument("--force", action="store_true", help="忽略样本量门槛强制评估")
    ap.add_argument("--rollback", action="store_true", help="回滚上一版权重")
    ap.add_argument("--history", action="store_true", help="查看权重变更历史")
    a = ap.parse_args()
    if a.history:
        show_history()
    elif a.rollback:
        rollback()
    else:
        r = run(report_only=a.report, force=a.force)
        for m in r["msg"]:
            print("[i]", m)
        print(f"\n新增结算 {r['n_new']} 条；权重{'已更新' if r['changed'] else '未变'}")
        if r.get("track"):
            print(f"\n昨日（{r['last_date']}）推荐跟踪：")
            for o in r["track"]:
                print(f"  {o['name']:<8}{o['code']:<10}{'/'.join(o['kinds']):<12}"
                      f"{fmt_pct(o['gain']):>9}  最高{fmt_pct(o['best']):>9}  {o['status']}")
        if r.get("att"):
            print("\n因子判别力（HIT组均值 - MISS组均值，标准化）：")
            for x in r["att"]:
                print(f"  {x['f']:<10} HIT {x['hit']*100:>5.1f}  MISS {x['mis']*100:>5.1f}"
                      f"  d={x['d']:+.3f}")
