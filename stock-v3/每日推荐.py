# -*- coding: utf-8 -*-
"""
主升浪买点 · 每日推荐 v3
================================================================================
模型来源：400 只样本 × 500 日K线的全市场回测（不是主观经验）
  测试期 2026-03-26~09-16（该段数据从未参与建模）：
  · 随机基准：5个交易日内真正放量启动 1.67%，10日内最大浮盈≥20% 11.94%
  · 【B】可买入版（剔除各板块涨停股）：5日内启动 4.58%（2.7×），浮盈≥20% 18.06%，回撤 -11.36%
  · 【A】追强势版（含涨停股，买不进）：5日内启动 8.19%（4.9×），浮盈≥20% 22.78%，回撤 -11.77%

用法：
  python 每日推荐.py pick              # 每日推荐最符合的 6 只（默认，B版为主推）
  python 每日推荐.py pick 10           # 推荐 10 只
  python 每日推荐.py diag sz001216     # 单只诊断，打印全部因子与得分
  python 每日推荐.py review            # 回溯已推荐记录，判定对错
  python 每日推荐.py audit 代码...     # 查年报审计机构

--------------------------------------------------------------------------------
★ 板块差异（v3.1 修正）：涨停判定阈值随板块变化 ——
  主板 10% / 创业板·科创板 20% / 北交所 30%。
  此前统一用 9.5% 判定，会把创业板、科创板涨 10~19% 的日子误记成"涨停"，
  导致 shrink（涨停后缩量回踩，权重 1.16）等因子失真。已按板块修正并全量重算。
★ 关于"试盘后隔 1/2/3 个月"：已单独回测验证 ——
  只在"1个月±3日"窗口轻微有效（2.61% vs 基准 2.04%，+28%），2/3 个月窗口无效。
  把它加入打分会【降低】准确率，因此仅作为参考标签展示，不参与排序。
★ 关于"试盘→回踩60日线→十字星企稳"（2026-10-30 全市场检验）：
  12.6万样本上全形态提升倍数仅 0.48×（A/B 两段归零），缩量与十字星单独看也是负贡献。
  判无效不入模，仅作展示标签 🔍试盘回踩企稳（features.py 的 ma60_touch/doji/probe_recent/vol_dry）。
--------------------------------------------------------------------------------
"""
import json, subprocess, sys, os, argparse, datetime, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import features as F
import model2 as M
import audit_check
import datafetch

# 2026-11-02：WorkBuddy 更新移除了 westock 本地脚本（westock-tool / westock-data），
# 取数层切换到 datafetch.py（K线=腾讯行情后端 ifzq.gtimg.cn，候选池=东财粗筛）。
NODE = "node"
ROOT = r"E:/app/workbuddy/resources/app.asar.unpacked/resources/builtin-skills"
TOOL = os.path.join(ROOT, "westock-tool/scripts/index.js")
DATA = os.path.join(ROOT, "westock-data/scripts/index.js")
FIT = "factor_fit.json"
PREDS = "predictions.json"
EXCLUDE_FIRM = "立信"

FITDATA = json.load(open(os.path.join(HERE, FIT), encoding="utf-8"))
W = FITDATA["weights"]
FACTORS = list(W.keys())

NOW = datetime.datetime.now()
MARKET_OPEN = NOW.weekday() < 5 and (NOW.hour * 60 + NOW.minute) < 15 * 60 + 5


def run(cmd, timeout=120):
    for _ in range(2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", timeout=timeout)
            if r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
    return None


def kline(code, days=320, cache=None):
    if cache is not None and code in cache:
        return cache[code]
    d = datafetch.kline(code, days)
    if not d:
        return None
    if len(d) < 60:
        return None
    return d


# 市值分段逻辑已并入 datafetch.universe()（全量分页拉取，无 2000 条上限问题）
ST_PAT = datafetch.ST_PAT


def universe():
    """粗筛：市值 ≥25 亿（不设上限）+ 换手 0.3~25% + 剔除 ST。走 datafetch 公共行情层。"""
    pool = datafetch.universe()
    if len(pool) < 1500:
        raise RuntimeError(
            f"候选池仅 {len(pool)} 只（正常约 2700），疑似数据源异常，拒绝出榜")
    return pool


_CACHE_MEMO = {"key": None, "data": None}


def load_cache():
    """读取内存缓存；独立模式才可使用旧 JSON，统一流水线严格走历史分片。"""
    if _CACHE_MEMO["data"] is not None:
        return _CACHE_MEMO["data"], _CACHE_MEMO["key"]
    if datafetch.shared_ready():
        return {}, None
    f = os.path.join(HERE, "kline_cache.json")
    if not os.path.exists(f):
        return {}, None
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        return {}, None
    _CACHE_MEMO["data"] = d.get("data", {})
    _CACHE_MEMO["key"] = d.get("key")
    return _CACHE_MEMO["data"], _CACHE_MEMO["key"]


def mark_cache(cache, key):
    """回写记忆化（build 补拉后调用），让后续模块复用同一份内存数据"""
    _CACHE_MEMO["data"] = cache
    _CACHE_MEMO["key"] = key


def pct_map(vals):
    v = sorted(vals)
    return lambda x: F.percentile(v, x)


def buyable(r):
    """次日是否买得进：按板块涨停幅度剔除已涨停/逼近涨停的。
    主板 10% → 剔除当日涨 ≥8%；创业板/科创板 20% → 剔除 ≥15%；北交所 30% → 剔除 ≥22%。
    规则经测试期验证：5日内启动 4.58%（随机 1.67%，2.7×），
    且创业板+科创板入选占比由 21.0% 提升到 24.0%。"""
    lim = M.board_limit(r["code"])
    cap = 8.0 if lim < 15 else (15.0 if lim < 25 else 22.0)
    return r["day_chg"] < cap


def build(topn=6):
    pool = universe()
    print(f"候选宇宙 {len(pool)} 只（市值 ≥25 亿，上不封顶）")
    name = {p["code"]: p["name"] for p in pool}
    today = datetime.date.today().isoformat()
    if datafetch.shared_ready():
        # 正式流水线一次顺序扫描历史分片；旧 kline_cache.json 不参与选股。
        cache = datafetch.load_market_cache(
            [p["code"] for p in pool], count=320,
            end=os.environ.get("KLINE_END", ""))
        key = max((v[-1]["date"] for v in cache.values() if v), default="")
        mark_cache(cache, key)
        todo = []
        print(f"统一历史库离线载入 {len(cache)} 只，基准日 {key}")
    else:
        cache, key = load_cache()
        # 独立补跑模式：历史基准日必须全量按 KLINE_END 重取，禁用磁盘缓存
        if os.environ.get("KLINE_END"):
            cache, key = {}, None
        # 缓存 key 记录的是【该批数据的最后交易日】，不是运行日期。
        # 收盘后（15:05 起）必须覆盖到"今天"，否则说明缓存是旧数据，作废重拉；
        # 未收盘时用最近一个交易日的数据出榜是正常的，不能因为 key≠today 就白等半小时重拉。
        if (NOW.hour * 60 + NOW.minute) >= 15 * 60 + 5 and key != today:
            print(f"  缓存停留于 {key}，而今天({today})已收盘 → 作废重拉")
            cache = {}
        todo = [p for p in pool if p["code"] not in cache]
    if todo:
        print(f"K线缓存 {len(cache)} 只，需补拉 {len(todo)} 只"
              f"（并发取数，约 {max(1, len(todo) // 100)} 分钟）...")

        def fetch(p):
            return p["code"], kline(p["code"])

        def fetch_all(items, workers, label):
            got = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for c, b in ex.map(fetch, items):
                    got += 1
                    if b:
                        cache[c] = b
                    if got % 200 == 0:
                        print(f"    {label} 已取 {got}/{len(items)}，成功 {len(cache)}")
            return got

        # 首轮 6 路并发（原 12 路：2026-11-02 实测 12 路抓 1700 只后腾讯三域名
        # 同时被网关 501 封锁，取数层已有全局 7 req/s 限速与域名熔断，这里同步降并发）
        fetch_all(todo, 6, "首轮")
        for rnd, (w, delay) in enumerate([(4, 0.3), (2, 0.8)], 1):
            missed = [p for p in todo if p["code"] not in cache]
            if not missed:
                break
            print(f"    重试轮 {rnd}：{len(missed)} 只未取到，降并发重试...")
            time.sleep(delay)
            fetch_all(missed, w, f"重试{rnd}")
        st = datafetch.stats() if hasattr(datafetch, "stats") else {}
        if st.get("sina"):
            print(f"    [注意] {st['sina']} 只走了新浪兜底通道（不复权，因子略有偏差）")
        if st.get("dead_hosts"):
            print(f"    [注意] 已熔断域名：{st['dead_hosts']}")
        ok = sum(1 for p in todo if p["code"] in cache)
        # 熔断只针对大面积取数失败（≥50 只，说明行情接口真挂了）。
        # 小批量失败（池子夜间自然轮换出的次新股，K线不足60根，新浪/腾讯均无历史）
        # 属正常现象——新股本来也过不了打分的 ≥200 根门槛，不应整体拒绝出榜。
        # 真正的数据质量兜底在下游：有效样本<1000 拒绝 + 基准日一致性 95% 校验。
        if len(todo) >= 50 and ok < len(todo) * 0.6:
            raise RuntimeError(
                f"K线取数成功率过低（{ok}/{len(todo)}），疑似行情接口异常，拒绝出榜")
        if ok < len(todo):
            print(f"    [注意] {len(todo) - ok} 只未取到K线（多为上市不足60个交易日的次新股，"
                  f"自动跳过，不影响出榜）")
        # key 必须写"数据真实的最后交易日"，绝不能写运行日期——
        # 否则早上预跑会把昨天的数据盖上今天的 key，收盘后就会误命中、推重复榜单。
        # KLINE_END 补跑模式（历史基准日）严禁写共享缓存：补拉的股票带今日数据、
        # 旧缓存停在基准日，混合写入会让 key 失真、污染后续正常推送。
        if os.environ.get("KLINE_END"):
            mark_cache(cache, "BACKFILL")
        else:
            last_d = max((v[-1]["date"] for v in cache.values() if v), default=today)
            # 必须原子写：2026-11-02 一次运行被 kill 时正好在 dump，
            # 直接把 kline_cache.json 写成半截 JSON（42MB 全废），当天缓存全部重拉。
            p_cache = os.path.join(HERE, "kline_cache.json")
            tmp = p_cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(dict(key=last_d, data=cache), fp)
            os.replace(tmp, p_cache)
            mark_cache(cache, last_d)
    else:
        print(f"命中今日K线缓存 {len(cache)} 只，直接计算因子")

    dropped = [0]
    def work(p):
        b = cache.get(p["code"])
        if not b or len(b) < 200:
            return None
        # 52周活跃度筛选（原 westock filter 的 Week52High > Week52Low*1.5，
        # 现改为本地按近250根K线计算，口径一致）
        w250 = b[-250:]
        try:
            if max(x["high"] for x in w250) <= min(x["low"] for x in w250) * 1.5:
                return None
        except Exception:
            pass
        idx = len(b) - 1
        # 只有「最后一根K线就是今天、且尚未收盘」才剔除（日内数据残缺）。
        # 不能只看钟点——早上跑的时候今天还没有K线，按钟点判断会误取到前一日。
        if idx >= 1 and b[-1]["date"] == today and \
                (NOW.hour * 60 + NOW.minute) < 15 * 60 + 5:
            idx = len(b) - 2
            dropped[0] += 1
        # 必须传 code：涨停判定阈值随板块变化（主板10% / 创业板·科创板20% / 北交所30%）
        f = F.factors_at(b, idx, p["code"])
        if not f:
            return None
        f["code"] = p["code"]; f["name"] = name.get(p["code"], "")
        return f
    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(work, pool):
            if r:
                rows.append(r)
    base_date = rows[0]["date"] if rows else ""
    # 基准日一致性校验：缓存污染（混合交易日数据）时拒绝出榜——
    # 2026-11-02 事故：补跑与正常数据混写缓存，把周五榜单当今天的推了
    if rows:
        from collections import Counter
        dc = Counter(x["date"] for x in rows)
        base_date, n_max = dc.most_common(1)[0]
        if n_max < len(rows) * 0.95:
            raise RuntimeError(
                f"基准日不一致（{base_date} 仅占 {n_max}/{len(rows)}），"
                f"疑似K线缓存污染，拒绝出榜")
    if len(rows) < 1000 or not base_date:
        raise RuntimeError(
            f"有效样本仅 {len(rows)} 只、基准日 '{base_date}'（正常约 2600 只），"
            f"疑似数据源残缺，拒绝出榜")
    print(f"有效 {len(rows)} 只，基准日 {base_date}"
          f"{f'（盘中，{dropped[0]} 只已剔除当日不完整K线）' if dropped[0] else ''}")

    # 每日横截面百分位 → 加权综合分（与回测完全一致的做法）
    maps = {k: pct_map([r[k] for r in rows]) for k in FACTORS}
    tot = sum(W.values())
    for r in rows:
        # 保存当日横截面百分位：事后归因要靠它判断「是哪几个因子把这只票推上来的」
        r["pct"] = [round(maps[k](r[k]), 4) for k in FACTORS]
        r["score"] = round(sum(W[k] * r["pct"][j]
                               for j, k in enumerate(FACTORS)) / tot * 100, 2)
    rows.sort(key=lambda x: -x["score"])
    n_universe = len(rows)          # 闸门前的有效样本数，推送端仍显示全市场口径

    def _tier(x):
        d = x.get("dsc", 99)
        if d <= 1:
            return 0
        if d <= 3:
            return 1
        return 2

    # 记录「无闸门时」的主榜候选序，供形态/立信剔除名单聚焦到原本会进榜的票
    pre_seq = sorted([x for x in rows if buyable(x)],
                     key=lambda x: (_tier(x), -x["score"]))[:16]
    pre_rank = {x["code"]: i for i, x in enumerate(pre_seq)}

    # ── 立信硬闸门（2026-11-13 用户反馈「中重科技是立信的，不该推送」）──────
    #    消费统一流水线共同前置层产出的 common/state/lixin_exclude.json，
    #    在【横截面打分之后】对【全部 rows】做离线硬过滤。
    #    · 不依赖联网公告 → 东财 502 / audit 缓存不入库时仍然生效，堵住 UNKNOWN 放行
    #    · 与流水线口径完全一致（同一份全市场名单），非只查候选前几十只
    #    · 位置与形态闸门同级：分数仍由全市场 n_universe 只算出，闸门只做
    #      出榜前硬过滤，不改分数 / 权重 / 排序 / 板块配额 / 交易规则
    #    · 不触碰 model2.P / factor_fit.json / holdout.json / 任何状态文件
    #    · LIXIN_GATE=off 可回退；名单缺失或异常降级放行并告警，绝不阻断主闭环
    lixin_excluded = []
    try:
        import lixin_gate
        rows, lixin_excluded = lixin_gate.apply(rows)
        if lixin_excluded:
            print(f"  立信闸门高分剔除：{'、'.join(x['name'] for x in lixin_excluded[:4])}")
    except Exception as e:
        print("  [警告] 立信闸门异常，本次跳过该过滤：", e)

    # ── 形态闸门（2026-11-09 用户要求：剔除「破位反弹」错筛）──────────────
    #    位置必须在【横截面打分之后】：每只票的分数仍由全市场 n_universe 只的
    #    百分位算出，闸门只做「出榜前硬过滤」，不改变任何一只票的分数与排序。
    #    与「立信审计一票否决」「buyable 涨停剔除」同级，不改 9 因子权重、不改
    #    model2.P / factor_fit.json / holdout.json、不改交易规则与结算口径。
    #    独立模块 shape_gate.py；SHAPE_GATE=off 可一键回退；异常不阻断主闭环。
    #    过滤后 pool 也同步干净，首日观察榜不会再出现破位反弹票。
    shape_excluded = []
    try:
        import shape_gate
        rows, shape_excluded = shape_gate.apply(rows, cache, base_date)
        if shape_excluded:
            print(f"  形态闸门：剔除破位反弹 {len(shape_excluded)} 只"
                  f"（其中高分票：{'、'.join(x['name'] for x in shape_excluded[:4])}）")
    except Exception as e:
        print("  [警告] 形态闸门异常，本次跳过该过滤：", e)

    def apply_audit(cand):
        aud = audit_check.main([x["code"] for x in cand], verbose=False)
        # 2026-11-13 修复：东财公告接口 502 / 云端无审计缓存时，audit_check 会把
        # 大量票判为 UNKNOWN。旧逻辑「只剔 LIXIN、UNKNOWN 当通过」正是中重科技
        # 漏进主榜的直接原因。这里再叠加流水线离线排除集做双保险：
        #   命中流水线名单 → 无论联网审计返回什么都剔除；
        #   UNKNOWN 票若不在名单中 → 仍放行，但打印醒目降级告警供人工察觉。
        try:
            import lixin_gate
            gate_on = lixin_gate.available()
        except Exception:
            gate_on = False
        n_unknown = sum(1 for x in cand
                        if aud.get(x["code"], {}).get("status") == "UNKNOWN")
        if n_unknown:
            print(f"  [审计降级] 本次 {n_unknown}/{len(cand)} 只候选联网审计返回 UNKNOWN"
                  f"（公告接口异常/缓存缺失）；离线立信排除集"
                  f"{'已启用兜底' if gate_on else '不可用，请立即核查'}")

        def _bad(x):
            if aud.get(x["code"], {}).get("status") == "LIXIN":
                return True
            return gate_on and lixin_gate.is_excluded(x["code"])

        blocked = [x for x in cand if _bad(x)]
        ok = [x for x in cand if not _bad(x)]
        for x in ok:
            x["firm"] = aud.get(x["code"], {}).get("firm", "")
        return ok, blocked          # 不再在函数内截断，由调用方按需截取

    all_picks, blk1 = apply_audit(rows[:topn + 12])
    all_picks = all_picks[:topn]
    # ② 可买入版：按【各自板块的涨停幅度】剔除已涨停/逼近涨停的，次日能按市价从容买入
    latent_src = [x for x in rows if buyable(x)]

    # ── 新鲜度优先（2026-11-04 用户要求：别再推"已启动好几天"的票）——
    #    痛点：9 因子里 chg5/chg10/chg20 都按涨幅给百分位，启动中段的票天然高分，
    #    启动第 1 天的票（刚突破，累计涨幅还小）反被挤出前 6。全市场实测（11-04）：
    #    可买入的 dsc=0 票分散在第 139/152/244/265/388 名，原逻辑永远轮不到它们。
    #    改法：新鲜度定资格、模型分定座次——不推翻打分模型，只改入选顺序。
    #    第 0 层 dsc≤1（启动 1~2 天，含突破当天）→ 第 1 层 dsc≤3（3~4 天）
    #    → 第 2 层其余按分数兜底（保证榜单不空）。
    #    注意 dsc=0 当天已涨停的票仍会被 buyable() 剔除（符合"次日涨幅>7%不追"），
    #    所以"启动第1天且买得进"天然稀缺，全市场通常只有个位数，属正常现象。
    #    _tier() 已在打分后统一定义（形态剔除名单也要用同一分层口径）。
    fresh_sorted = sorted(latent_src, key=lambda x: (_tier(x), -x["score"]))
    # 审计一票否决必须在【分层后】的序列上做：新鲜票原始分数排名可能在一百名开外，
    # 旧的"只查前 18 名"做法会把它们整批漏掉，等于白分层
    lat_ok, blk2 = apply_audit(fresh_sorted[:topn * 2 + 28])
    # ── 单榜（2026-11-04 用户要求取消 A/B/C 三榜）：
    #    主板 3 只 + 创业/科创板 3 只；两侧都在【新鲜度分层序】内取头部位次，
    #    一侧不足 3 只时由另一侧按同序补足到 6 只
    mains = [x for x in lat_ok if x.get("board") not in ("创业板", "科创板")]
    cybs = [x for x in lat_ok if x.get("board") in ("创业板", "科创板")]
    final = mains[:3] + cybs[:3]
    if len(final) < 6:
        used = {x["code"] for x in final}
        rest = [x for x in mains[3:] + cybs[3:] if x["code"] not in used]
        final += rest[:6 - len(final)]
    # ── 首次选入日 → 跟踪天数：查 predictions.json 该票最早一次入选日期，
    #    按该票自身 K 线数交易日；无历史记录 = 今日新选入（第1日）
    try:
        pdb = json.load(open(os.path.join(HERE, "predictions.json"),
                             encoding="utf-8"))
    except Exception:
        pdb = []
    hist = {}
    for d in pdb:
        c, dt = d.get("code"), d.get("date", "")
        if c and dt and (c not in hist or dt < hist[c]):
            hist[c] = dt
    for p in final:
        first = hist.get(p["code"])
        if not first or first >= base_date:
            p["since"] = dict(first=base_date, days=1, fresh=True)
        else:
            b = cache.get(p["code"]) or []
            dates = [x["date"] for x in b]
            days = (dates.index(base_date) - dates.index(first) + 1
                    if first in dates and base_date in dates else 1)
            p["since"] = dict(first=first, days=max(1, days), fresh=False)
    # 形态剔除名单：优先「原本会进榜」的票（用户实际看到的错筛），再补高分同类票
    shape_shown = sorted(
        shape_excluded,
        key=lambda x: (pre_rank.get(x["code"], 999), -(x.get("score") or 0)))[:12]
    # 立信剔除名单：同样优先「原本会进榜」的高分票，让用户直接看到
    # 「中重科技这类票已被拦下」。并入 blocked 展示（语义同为立信一票否决），按代码去重。
    lixin_shown = sorted(
        lixin_excluded,
        key=lambda x: (pre_rank.get(x["code"], 999), -(x.get("score") or 0)))[:12]
    blocked_all, seen_code = [], set()
    for b in blk1 + blk2 + lixin_shown:
        if b["code"] not in seen_code:
            seen_code.add(b["code"])
            blocked_all.append(b)
    return dict(allpicks=all_picks, latpicks=final, cybpicks=[],
                blocked=blocked_all, shape_excluded=shape_shown,
                shape_excluded_total=len(shape_excluded),
                lixin_excluded=lixin_shown,
                lixin_excluded_total=len(lixin_excluded),
                base_date=base_date, universe=n_universe, pool=rows)


def show(r):
    picks, blocked = r["latpicks"], r["blocked"]
    base_date = r["base_date"]
    if blocked:
        seen = set()
        print(f"\n✗ 剔除（年报审计机构为{EXCLUDE_FIRM}，一票否决）：")
        for b in blocked:
            if b["code"] in seen:
                continue
            seen.add(b["code"])
            print(f"    {b['name']:<8}{b['code']:<10} 分{b['score']:.1f}  {EXCLUDE_FIRM}会计师事务所")

    print(f"\n★ 今日主推单榜（新鲜度优先：先取启动1~2天，不够再退3~4天；同层内按模型分排序，已剔除涨停股）")
    print(f"   基准日 {base_date}｜下一交易日可参考")
    print("=" * 150)
    print(f"{'#':<3}{'名称':<9}{'代码':<10}{'板块':<7}{'得分':<7}{'收盘':<8}{'当日%':<8}{'近5日%':<8}"
          f"{'近10日%':<9}{'启动进度':<22}{'选入跟踪':<18}{'审计机构'}")
    print("-" * 150)
    for i, p in enumerate(picks, 1):
        firm = (p.get("firm") or "未识别").replace("会计师事务所", "")[:12]
        s = p.get("since") or {}
        if s.get("fresh"):
            track = "今日新选入(第1日)"
        else:
            track = f"第{s.get('days','?')}日(自{s.get('first','?')})"
        d = p.get("dsc", 99); g = p.get("launch_gain")
        if d == 0:
            prog = "★启动第1天(今日刚突破)"
        elif d < 99:
            prog = f"第{d + 1}天" + (f"·累计{g:+.1f}%" + ("⚠追高" if g and g >= 20 else "")
                                    if g is not None else "")
        else:
            prog = "未放量启动"
        print(f"{i:<3}{p['name']:<9}{p['code']:<10}{p.get('board','-'):<7}"
              f"{p['score']:<7.1f}{p['close']:<8.2f}"
              f"{p['day_chg']:<8.2f}{p['chg5']:<8.1f}{p['chg10']:<9.1f}"
              f"{prog:<22}{track:<18}{firm}")
    print("-" * 150)
    print("  「启动进度」= dsc（距本波最早放量突破日天数）：第1天即突破当天，累计%=距起涨前收盘涨幅；")
    print("  「选入跟踪」= 该票首次被模型选入推荐至今的交易日数（含当日）。")
    print("\n  风控：跌破近10日最低价离场；10日内浮盈≥20% 分批了结；单票仓位不超过 1/5。")


def record(r):
    db = []
    f = os.path.join(HERE, PREDS)
    if os.path.exists(f):
        try:
            db = json.load(open(f, encoding="utf-8"))
        except Exception:
            db = []
    have = {(d.get("date"), d.get("code"), d.get("kind")) for d in db}
    # 单榜后 kind 按板块映射记录，保留与历史一致的结算归因分组
    for p in r["latpicks"]:
        kind = "C-创科" if p.get("board") in ("创业板", "科创板") else "B-埋伏"
        k = (r["base_date"], p["code"], kind)
        if k in have:
            continue
        db.append(dict(model="v3", kind=kind, date=r["base_date"], code=p["code"],
                       name=p["name"], close=p["close"], score=p["score"],
                       factors=FACTORS, pct=p.get("pct"),
                       dsc=p.get("dsc"), launch_gain=p.get("launch_gain"),
                       since=p.get("since")))
    json.dump(db, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 形态剔除名单精简落盘（只留判定依据，便于事后审计，不参与结算）
    sx = [dict(code=x["code"], name=x["name"], score=x["score"],
               reasons=x.get("shape_reasons", []),
               evidence=x.get("shape_evidence", {}))
          for x in r.get("shape_excluded", [])]
    # 立信剔除名单精简落盘（2026-11-13：流水线离线排除集命中依据）
    lx = [dict(code=x["code"], name=x["name"], score=x["score"],
               reason=x.get("lixin_reason", ""))
          for x in r.get("lixin_excluded", [])]
    out = dict(base_date=r["base_date"], universe=r["universe"],
               generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               PICKS=r["latpicks"], blocked=r["blocked"],
               shape_excluded=sx, lixin_excluded=lx,
               lixin_excluded_total=r.get("lixin_excluded_total", len(lx)))
    json.dump(out, open(os.path.join(HERE, "今日推荐.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def review():
    """回溯：判定已推荐记录的成败，并给出后续表现"""
    db = []
    if os.path.exists(os.path.join(HERE, PREDS)):
        db = json.load(open(os.path.join(HERE, PREDS), encoding="utf-8"))
    old = []
    wl = "watchlist.json"
    if os.path.exists(os.path.join(HERE, wl)):
        try:
            old = json.load(open(os.path.join(HERE, wl), encoding="utf-8"))
        except Exception:
            old = []
    # v1 模型 2026-10-23 收盘的推荐
    v1 = [dict(date="2026-10-23", code=x["code"], name=x["name"],
               close=x["close"], score=x.get("score", 0), model="v1") for x in old]
    for x in [y for y in db if y.get("model") == "v3"]:
        v1.append(x)
    if not v1:
        print("没有可回溯的记录")
        return
    print(f"回溯 {len(v1)} 条推荐记录...\n")
    by_model = {}
    for p in v1:
        b = kline(p["code"])
        if not b:
            continue
        dates = [x["date"] for x in b]
        if p["date"] not in dates:
            continue
        i = dates.index(p["date"])
        seg = b[i:] if i + 1 < len(b) else b
        future = seg[1:]
        if not future:
            continue
        c0 = b[i]["last"]
        hi5 = max(x["high"] for x in future[:5]); hi10 = max(x["high"] for x in future[:10])
        lo10 = min(x["low"] for x in future[:10])
        res = dict(code=p["code"], name=p["name"], date=p["date"], score=p["score"],
                   close=c0, now=future[-1]["last"], days=len(future),
                   gain_now=(future[-1]["last"] / c0 - 1) * 100,
                   best10=(hi10 / c0 - 1) * 100, best5=(hi5 / c0 - 1) * 100,
                   worst10=(lo10 / c0 - 1) * 100)
        res["hit"] = res["best10"] >= 20
        by_model.setdefault(p.get("kind","A"), []).append(res)

    for mdl, rs in by_model.items():
        n = len(rs)
        h = sum(1 for r in rs if r["hit"])
        print(f"【{mdl} 模型】共 {n} 条，10日内最大浮盈≥20% 命中 {h} 条，命中率 {h/n*100:.1f}%"
              f"｜平均最大浮盈 {sum(r['best10'] for r in rs)/n:.1f}%"
              f"｜平均最大回撤 {sum(r['worst10'] for r in rs)/n:.1f}%")
        rs.sort(key=lambda x: -x["best10"])
        print(f"{'名称':<9}{'代码':<10}{'推荐日':<12}{'推荐价':<8}{'持仓天':<7}"
              f"{'现价':<8}{'至今%':<9}{'5日最大%':<10}{'10日最大%':<11}判定")
        print("-" * 106)
        for r in rs:
            tag = "✓命中" if r["hit"] else ("接近" if r["best10"] >= 12 else "✗失败")
            print(f"{r['name']:<9}{r['code']:<10}{r['date']:<12}{r['close']:<8.2f}"
                  f"{r['days']:<7}{r['now']:<8.2f}{r['gain_now']:<9.1f}"
                  f"{r['best5']:<10.1f}{r['best10']:<11.1f}{tag}")
        print()


def diag(code):
    b = kline(code, 500)
    if not b:
        print("无数据")
        return
    i = len(b) - 1
    if i >= 1 and b[-1]["date"] == datetime.date.today().isoformat() and \
            (NOW.hour * 60 + NOW.minute) < 15 * 60 + 5:
        i = len(b) - 2
    f = F.factors_at(b, i, code)
    if not f:
        print("数据不足")
        return
    rows = json.load(open(os.path.join(HERE, "diag_rows2.json"), encoding="utf-8"))
    print(f"{f['name'] if 'name' in f else code} {code}  {f['date']}  收盘 {f['close']}")
    print(f"{'因子':<12}{'当前值':<12}{'全市场百分位':<14}{'权重':<8}{'贡献'}")
    print("-" * 62)
    tot = sum(W.values())
    s = 0
    for k in FACTORS:
        pr = F.percentile(sorted(r[k] for r in rows), f[k])
        c = W[k] * pr / tot * 100
        s += c
        print(f"{k:<12}{f[k]:<12}{pr*100:<14.1f}{W[k]:<8.1f}{c:.2f}")
    print("-" * 62)
    print(f"{'综合得分':<12}{s:.1f}")
    print(f"\n距上次放量试盘：{f['gap'] if f['gap']!=999 else '超过120个交易日'} 个交易日"
          f"（试盘日 {f['last_probe_date'] or '-'}，当日量能 {f['last_probe_vr']} 倍）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="主升浪买点每日推荐 v3")
    ap.add_argument("mode", nargs="?", default="pick",
                    choices=["pick", "review", "diag", "audit"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    if a.mode == "pick":
        n = int(a.args[0]) if a.args and a.args[0].isdigit() else 6
        r = build(n)
        show(r)
        record(r)
    elif a.mode == "review":
        review()
    elif a.mode == "diag":
        for c in a.args:
            diag(c)
    elif a.mode == "audit":
        res = audit_check.main(list(a.args), verbose=False)
        for c in a.args:
            o = res.get(c, {})
            print(f"{c:<12}{o.get('status',''):<9}{o.get('firm','')}")
