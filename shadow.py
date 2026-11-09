# -*- coding: utf-8 -*-
"""
shadow.py —— 影子假设跟踪器（hypothesis_watch.json 注册制）

用途：把「人工提出的选股规则修改假设」挂进观察名单，每日用与真实推荐完全
相同的结算口径做影子记账，样本攒够后与真实推荐组对照，通过闸门才标记
promote_ready。全程【只写自己的文件】，绝不修改 factor_fit.json /
holdout.json / predictions.json / feedback_db.jsonl。

命令：
  python3 shadow.py            # record + settle + evaluate 一条龙（供 daily_v3 调用）
  python3 shadow.py record     # 只记当日影子名单
  python3 shadow.py settle     # 只结算
  python3 shadow.py evaluate   # 只评估闸门
  python3 shadow.py report     # 输出Markdown复盘段（供推送正文引用）
"""
import os, sys, json, bisect, datetime, math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WATCH = os.path.join(HERE, "hypothesis_watch.json")
SLOG = os.path.join(HERE, "shadow_log.jsonl")
SDB = os.path.join(HERE, "shadow_db.jsonl")
FIT = os.path.join(HERE, "factor_fit.json")
CACHE = os.path.join(HERE, "kline_cache.json")

KIND = "SHADOW-H1"


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
        raise RuntimeError("缺少 hypothesis_watch.json")
    return w


def save_watch(w):
    with open(WATCH, "w", encoding="utf-8") as fp:
        json.dump(w, fp, ensure_ascii=False, indent=1)


# ────────────────────────────── 记录 ──────────────────────────────
def record():
    """对最新交易日计算 H1 影子名单（dsc==0 & brk20>=-2，含涨停，得分前30%）"""
    import features as F
    w = watch()
    hyp = w["definition"]
    fit = jload(FIT, None)
    if not fit:
        return ["[影子] 缺少 factor_fit.json，跳过记录"]
    Wd = fit["weights"]; FACTORS = list(Wd.keys()); tot = sum(Wd.values())

    k = jload(CACHE, None)
    if not k or not k.get("data"):
        return ["[影子] 缺少 kline_cache.json，跳过记录"]
    data = k["data"]
    base = max(b["date"] for b in next(iter(data.values())))
    # 幂等：同基准日不重复记
    for line in (open(SLOG, encoding="utf-8").read().splitlines() if os.path.exists(SLOG) else []):
        try:
            if json.loads(line).get("date") == base:
                return [f"[影子] {base} 已记录过，跳过"]
        except Exception:
            pass

    rows = []
    for code, bars in data.items():
        if len(bars) < 200:
            continue
        hist = [b for b in bars if b["date"] <= base]
        if not hist or hist[-1]["date"] != base:
            continue
        f = F.factors_at(hist, len(hist) - 1, code)
        if not f:
            continue
        f["code"] = code
        rows.append(f)
    if not rows:
        return [f"[影子] {base} 全市场因子为空，跳过记录"]
    cols = {kk: sorted(r[kk] for r in rows) for kk in FACTORS}

    def pct(kk, x):
        return bisect.bisect_left(cols[kk], x) / len(cols[kk])

    # H1 池：dsc==0 且 brk20>=-2（不剔涨停、不看收敛）
    pool = [r for r in rows if r.get("dsc", 99) == 0 and r.get("brk20", -99) >= -2]
    if not pool:
        jappend(SLOG, dict(date=base, n_pool=0, picks=[], ts=_now()))
        return [f"[影子] {base} H1池为空（已记空档）"]
    for r in pool:
        r["pctv"] = [round(pct(kk, r[kk]), 4) for kk in FACTORS]
        r["score"] = sum(Wd[kk] * r["pctv"][j] for j, kk in enumerate(FACTORS)) / tot * 100
    pool.sort(key=lambda x: -x["score"])
    cut = pool[max(0, int(len(pool) * 0.3) - 1)]["score"]   # 前30%
    sel = [r for r in pool if r["score"] >= cut]

    def board(code):
        return ("创科" if code.startswith(("sz30", "sz301", "sh68")) else "主板")

    picks = [dict(code=r["code"], name=r.get("name", ""), kind=board(r["code"]),
                  score=round(r["score"], 2), close=r["close"], pct=r["pctv"],
                  day_chg=r["day_chg"], brk20=r["brk20"], box30=r["box30"],
                  vr5=r["vr5"], dsc=r["dsc"],
                  limitup=bool(r["day_chg"] >= (19.8 if board(r["code"]) == "创科" else 9.8)))
             for r in sel]
    jappend(SLOG, dict(date=base, n_pool=len(pool), score_cut=round(cut, 2),
                       picks=picks, ts=_now()))
    w["shadow"]["n_recorded"] = w["shadow"].get("n_recorded", 0) + 1
    save_watch(w)
    return [f"[影子] {base} H1池 {len(pool)} 只 → 前30%记 {len(picks)} 只"
            f"（含涨停 {sum(1 for p in picks if p['limitup'])} 只）"]


# ────────────────────────────── 结算 ──────────────────────────────
def settle():
    """用 feedback.settle_one 同口径结算影子记录，写入 shadow_db.jsonl。
    镜像 feedback 口径：不满 11 交易日的只尝试、不入库（等下轮重试）；
    已入库但 launch 未满 17 日的到期后原地补全。"""
    import feedback
    from concurrent.futures import ThreadPoolExecutor
    if not os.path.exists(SLOG):
        return ["[影子] 无记录，跳过结算"]
    db = _rows(SDB)
    done = {(r["date"], r["code"]): r for r in db}
    todo, seen = [], set()
    for line in open(SLOG, encoding="utf-8"):
        try:
            day = json.loads(line)
        except Exception:
            continue
        for p in day.get("picks", []):
            key = (day["date"], p["code"])
            if key in done or key in seen:
                continue
            seen.add(key)
            todo.append(dict(code=p["code"], name=p.get("name", ""), kind=KIND,
                             date=day["date"], score=p.get("score"),
                             pct=p.get("pct")))
    pend = [r for r in db if r.get("full") == 0]
    if not todo and not pend:
        return [f"[影子] 无待结算记录（库内 {len(db)} 条）"]
    got = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        jobs = todo + [dict(code=r["code"], name=r["name"], kind=r["kind"],
                            date=r["date"], score=r.get("score"), pct=r.get("pct"))
                       for r in pend]
        for r in ex.map(feedback.settle_one, jobs):
            if r:
                got.append(r)
    n_new = n_fix = 0
    bykey = {(r["date"], r["code"]): r for r in got}
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
    return [f"[影子] 结算尝试 {len(jobs)} 条：新入库 {n_new} 条（累计 {len(done)}），补全 launch {n_fix} 条"]


# ────────────────────────────── 评估闸门 ──────────────────────────────
def evaluate():
    w = watch()
    sh_rows = _rows(SDB)
    real_rows = [r for r in _rows(os.path.join(HERE, "feedback_db.jsonl"))
                 if not str(r.get("kind", "")).startswith("SHADOW")]
    sh = [r for r in sh_rows if r.get("settled") and r.get("g10") is not None]
    real = [r for r in real_rows if r.get("settled") and r.get("g10") is not None]
    need_s = w["protocol"]["shadow_settle_need"]
    need_r = w["protocol"]["real_settle_need"]

    def stat(rows):
        if not rows:
            return None
        g = [r["g10"] for r in rows]
        return dict(n=len(rows), g10=round(sum(g) / len(g), 2),
                    hit=round(sum(1 for x in g if x >= 20.0) / len(g) * 100, 1))

    ss, rs = stat(sh), stat(real)
    w["shadow"]["n_settled"] = len(sh)
    w["shadow"]["stat"] = ss
    msg = [f"[影子] 已结算 {len(sh)}/{need_s} 条；真实组已结算 {len(real)}/{need_r} 条"]

    if ss:
        w["shadow"]["stat"] = ss
    if ss and ss["n"] >= need_s:
        if rs and rs["n"] >= need_r:
            ok = ss["g10"] >= rs["g10"] and ss["hit"] >= rs["hit"]
            w["status"] = "promote_ready" if ok else "rejected"
            w["evaluated"] = _now()
            w["verdict"] = dict(shadow=ss, real=rs,
                                gate="g10均值与命中率双双不低于真实组")
            msg.append(f"[影子] 闸门裁定：影子 g10 {ss['g10']}%/命中 {ss['hit']}% vs "
                       f"真实 {rs['g10']}%/命中 {rs['hit']}% → {w['status']}")
        else:
            w["status"] = "await_real"
            msg.append("[影子] 影子样本已满，真实推荐组未满，继续等待")
    save_watch(w)
    return msg, w


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


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ────────────────────────────── 报告 ──────────────────────────────
def report_md(w=None):
    w = w or watch()
    st = w["shadow"].get("stat")
    icon = {"watching": "👀", "await_real": "⏳", "promote_ready": "✅", "rejected": "❌"}.get(w["status"], "👀")
    lines = [f"### {icon} 影子假设 H1（{w['name']}）｜状态 {w['status']}"]
    lines.append(f"　内容：{w['definition']['relax_1']}；{w['definition']['relax_2']}；"
                 f"{w['definition']['trim']}。只记账不动模型。")
    if st:
        lines.append(f"　影子已结算 {st['n']} 条：g10均值 {st['g10']}%、命中率(≥20%) {st['hit']}%")
    v = w.get("verdict")
    if v:
        lines.append(f"　对照真实组：g10 {v['real']['g10']}%、命中 {v['real']['hit']}% → 裁定 **{w['status']}**")
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
