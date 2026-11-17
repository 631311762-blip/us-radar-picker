# -*- coding: utf-8 -*-
r"""
主升浪 v3.1 · 每日收盘后自动跑【回溯 → 进化 → 选股 → 推送】
================================================================================
执行顺序很关键，闭环才成立：
  ① feedback.run()  回溯昨日推荐 → 结算到期样本 → 因子归因 → 条件更新权重
  ② 重载模型模块（若权重已更新，必须重新读取 factor_fit.json）
  ③ build()         用【当天最新权重】重新给全市场打分选股
  ④ record()        记录今日推荐（含因子百分位，供日后归因）
  ⑤ 推送微信        昨日跟踪 + 今日三榜 + 模型复盘

用法：
  python daily_v3.py                # 完整跑（默认每榜 6 只）
  python daily_v3.py --top 3        # 每榜只取 3 只
  python daily_v3.py --nopush       # 只跑不推
  python daily_v3.py --noevolve     # 跳过进化，只用现权重选股
"""
import os, sys, json, datetime, importlib.util, importlib

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# 推送实现：本地有 wechat-push 技能就用它；云端沙箱没有该路径时，
# 回落到同目录的 wechat_push.py（读 sckey.txt 或环境变量 SERVERCHAN_SENDKEY）。
_LSKILL = r"C:\Users\zhangjh\.workbuddy\skills\wechat-push\scripts\push.py"
PUSH = _LSKILL if os.path.exists(_LSKILL) else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wechat_push.py")


def fmt(row, board_show=True):
    arrow = "+" if row["day_chg"] >= 0 else ""
    bd = f"[{row.get('board', '-')}]" if board_show else ""
    # 展示标签：「试盘→回踩60日线→十字星」形态。
    # 12.6 万样本检验：该形态 0.43× 随机（p=0.047，显著低于随机），故【不参与排序】，
    # 标签措辞必须中性偏警示，避免被当成买入信号。
    tag = ""
    if row.get("probe_recent") and row.get("ma60_touch") and row.get("doji"):
        tag = "　🔍回踩60线(形态对照·非信号)"
    # 启动天数标签：dsc = 距【本波最早一次放量突破】（涨≥5%且量>2倍）的交易日数。
    # 2026-11-04 修正：旧口径取"最近一次"放量日，启动后再放量大阳会把起涨日重置，
    # 出现「涨停2天后首推却标启动当日」的误导。现在 dsc=0 表示突破当天，
    # dsc=N 表示突破发生在 N 个交易日前（本波已走到第 N+1 天）。
    # 追高提示：launch_gain = 现价相对起涨日前收盘的累计涨幅，≥20% 明示追高风险。
    d = row.get("dsc", 99)
    g = row.get("launch_gain")
    if d == 0:
        fresh = "　·启动当日(第1天)"
    elif d <= 10:
        fresh = f"　·启动第{d + 1}天"
    elif d < 99:
        fresh = f"　·启动第{d + 1}天⚠⚠(偏晚)"
    else:
        fresh = "　·未放量启动"
    if g is not None and d <= 10:
        fresh += f"　·距起涨累计{g:+.1f}%" + ("⚠追高风险" if g >= 20 else "")
    # 选入跟踪标签：该票首次被模型选入推荐至今的交易日数（用户 2026-11-04 要求）
    s = row.get("since") or {}
    if s.get("fresh"):
        fresh += f"　·选入第1日(今日新选入)"
    elif s:
        fresh += f"　·选入第{s.get('days','?')}日(自{s.get('first','?')}首推)"
    return (f"**{row['name']}**{bd} `{row['code']}` 分{row['score']:.0f}{tag}{fresh}  \n"
            f"　收 {row['close']}　当日 {arrow}{row['day_chg']:.2f}%　"
            f"5日 {arrow}{row['chg5']:.1f}%　10日 {arrow}{row['chg10']:.1f}%  \n"
            f"　[行情](https://gu.qq.com/{row['code']})｜"
            f"{row.get('firm', '').replace('会计师事务所', '')}")


def main():
    # 云端备份：模型代码 + 状态文件回传资料库（用户 2026-11-05 要求：
    # 每天的产出上云，聊天记录/沙箱丢了也能恢复最新模型并回溯历史）
    def _cloud_backup(r):
        try:
            import cloud_backup
            cloud_backup.main(base_date=r.get("base_date", ""))
        except Exception as e:
            print("[警告] 云端备份失败（不影响本地状态与推送）：", e)

    topn = 6
    push = True
    evolve = True
    args = sys.argv[1:]
    if "--top" in args:
        i = args.index("--top")
        if i + 1 < len(args) and args[i + 1].isdigit():
            topn = int(args[i + 1])
    if "--nopush" in args:
        push = False
    if "--noevolve" in args:
        evolve = False

    # ── ① 回溯与进化（在选股之前：让今天的选股用上最新权重）
    fb = dict(ok=False, changed=False, msg=[], track=[], last_date="", att=None,
              evo=None, hold_stats=None, n_new=0)
    if evolve:
        try:
            import feedback
            print("=" * 78)
            print("【第一步】回溯昨日推荐 → 结算 → 归因 → 进化模型")
            print("=" * 78)
            fb = feedback.run()
            for m in fb.get("msg", []):
                print("  ·", m)
            if fb.get("changed"):
                print("  ⚙️ 权重已更新，重新加载模型模块")
        except Exception as e:
            print("  [警告] 回溯环节异常，继续用现权重选股：", e)

    # ── ②③ 加载模型并选股（失败必须中止，绝不推空榜——2026-11-02 空榜事故的教训）
    try:
        import 每日推荐 as R
        import model2 as M
        if fb.get("changed"):
            importlib.reload(R)          # 关键：重新读取 factor_fit.json 的新权重
        ver = json.load(open(os.path.join(HERE, "factor_fit.json"),
                             encoding="utf-8")).get("ver", "v3.1")
        print("\n" + "=" * 78)
        print(f"【第二步】用 {ver} 权重给全市场打分选股")
        print("=" * 78)
        r = R.build(topn)

        # ── ②③b 推送前最终硬闸门（2026-11-13 用户：「立信审计票不该推送给我」）
        #    无条件生效：只要流水线共同前置层产出的 lixin_exclude.json 存在，
        #    主榜里出现名单内个股就直接 ABORT —— 不 record、不推送、不污染台账。
        #    这是独立于打分阶段 LIXIN_GATE 的最后一道防线，专门堵住
        #    「联网审计降级为 UNKNOWN → 被当成通过」这一类静默漏筛。
        try:
            import lixin_gate
            if lixin_gate.guard_enabled() and lixin_gate.list_loaded():
                bad = lixin_gate.main_picks_hit(r["latpicks"])
                if bad:
                    _info = lixin_gate.info()
                    raise RuntimeError(
                        "主榜含流水线立信排除集内个股，拒绝记录与推送："
                        + "、".join(f"{p['name']}({p['code']})" for p in bad)
                        + f"（排除集 {_info['n']} 只，更新于 {_info['updated']}）")
        except RuntimeError:
            raise
        except Exception as e:
            print("  [警告] 推送前立信终检异常（不影响主流程）：", e)

        R.show(r)
        if "--norecord" not in sys.argv:   # 调试用：跳过预测记录写入，不污染结算口径
            R.record(r)
    except Exception as e:
        print(f"\n[ABORT] 选股环节失败，本次不推送：{e}")
        with open(os.path.join(HERE, "push_log.jsonl"), "a", encoding="utf-8") as fp:
            fp.write(json.dumps(
                dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                     date=datetime.date.today().isoformat(), ok=False,
                     msg=f"ABORT: {e}"), ensure_ascii=False) + "\n")
        sys.exit(3)

    picks, latpicks = r["allpicks"], r["latpicks"]

    # ── ④b 影子假设跟踪（H1）：只记录/结算/评估，不改任何模型文件（2026-11-08 挂观察名单）
    sh = dict(msg=[], md=[])
    try:
        import shadow
        print("\n" + "=" * 78)
        print("【影子环节】H1 假设跟踪：记录 → 结算 → 闸门评估")
        print("=" * 78)
        sh = shadow.run()
        for m in sh.get("msg", []):
            print("  ·", m)
    except Exception as e:
        print("  [警告] 影子跟踪异常，不影响主流程：", e)

    # ── ④c 影子假设跟踪（H2）：涨停基因因子降权 A/B 对照（2026-11-18 用户要求走影子试验）
    #    与主榜同池同口径，只记不推不改；两组各自结算满60条才裁定。独立于 H1。
    sh2 = dict(msg=[], md=[])
    try:
        import shadow_h2
        print("\n" + "=" * 78)
        print("【影子环节】H2 假设跟踪：记录 → 结算 → 闸门评估")
        print("=" * 78)
        sh2 = shadow_h2.run()
        for m in sh2.get("msg", []):
            print("  ·", m)
    except Exception as e:
        print("  [警告] H2 影子跟踪异常，不影响主流程：", e)
    blocked, seen = [], set()
    for b in r["blocked"]:
        if b["code"] in seen:
            continue
        seen.add(b["code"])
        blocked.append(b)
    cyb = r.get("cybpicks", [])
    logf = os.path.join(HERE, "push_log.jsonl")

    # ── ⑤ 推送前两道闸（2026-11-02 两次空榜事故的教训：
    #    数据源断裂时照样"推送成功"，把 Server酱 每天仅 5 条的额度浪费在空榜上）
    if not latpicks and not picks:
        print("\n[FAIL] 单榜为空（候选池或取数异常），已拒绝推送空榜")
        with open(logf, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(dict(
                ts=datetime.datetime.now().isoformat(timespec="seconds"),
                date=r["base_date"], ok=False, msg="空榜拦截：单榜为空"),
                ensure_ascii=False) + "\n")
        sys.exit(4)
    # 同一基准日、同一份清单已成功推送过 → 跳过（避免重复推送吃掉当天额度）
    cur_codes = sorted(p["code"] for p in latpicks)
    if "--force-push" not in sys.argv:
        try:
            with open(logf, encoding="utf-8") as fp:
                for line in fp:
                    rec = json.loads(line)
                    if (rec.get("ok") and rec.get("date") == r["base_date"]
                            and rec.get("picks") == cur_codes):
                        print(f"\n[SKIP] {r['base_date']} 同一份清单已于 {rec.get('ts')} "
                              f"推送成功，本次跳过（加 --force-push 可强推）")
                        sys.exit(0)
        except (FileNotFoundError, ValueError):
            pass

    # ── ⑤ 组装推送
    md = [f"> **主升浪 V4**｜基准日 **{r['base_date']}**｜候选池 {r['universe']} 只\n"]

    # 昨日跟踪（用户最关心：昨天推的到底行不行）
    if fb.get("track"):
        md.append(f"### 📌 昨日（{fb['last_date']}）推荐跟踪")
        for o in fb["track"]:
            kinds = "/".join(k.replace("-", "") for k in o["kinds"])
            arrow = "🔴" if o["gain"] >= 0 else "🟢"
            md.append(f"- {arrow} **{o['name']}** `{o['code']}`〔{kinds}〕　{o['days']}日 "
                      f"**{'+' if o['gain']>=0 else ''}{o['gain']:.2f}%**"
                      f"｜最高 {'+' if o['best']>=0 else ''}{o['best']:.2f}%"
                      f"｜最低 {'+' if o['worst']>=0 else ''}{o['worst']:.2f}%"
                      f"｜{o['status']}")
        md.append("")

    md.append("### 🎯 今日主推（新鲜度优先：先取启动1~2天的票，不足再退3~4天；同层内按模型分排序，已剔除涨停股）")
    if latpicks:
        n_d1 = sum(1 for p in latpicks if p.get("dsc", 99) <= 1)
        n_d3 = sum(1 for p in latpicks if 1 < p.get("dsc", 99) <= 3)
        n_late = sum(1 for p in latpicks if 3 < p.get("dsc", 99) < 99)
        md.append(f"　新鲜度：{n_d1}/{len(latpicks)} 只属启动 2 天内（★=突破当天）"
                  + (f"；{n_d3} 只启动 3~4 天" if n_d3 else "")
                  + (f"；{n_late} 只更晚（标 ⚠追高风险，新鲜票不足时的兜底）" if n_late else ""))
        md.append("")
    for i, p in enumerate(latpicks, 1):
        md.append(f"{i}. {fmt(p)}")
    md.append("")
    if blocked:
        md.append("### 已剔除（年报审计机构为立信，一票否决）")
        # 2026-11-13：并入统一流水线共同前置层的离线立信排除集，
        # 东财公告接口异常导致联网审计降级时仍能拦住（堵住 UNKNOWN 放行）。
        try:
            import lixin_gate as _lg
            if _lg.list_loaded():
                _i = _lg.info()
                md.append(f"　口径：联网公告审计 + 流水线离线排除集（{_i['n']} 只，"
                          f"更新于 {_i['updated']}），命中任一即剔除；"
                          "主榜出榜前另有最终硬闸门复核。")
        except Exception:
            pass
        md.append("　" + "、".join(f"{b['name']}({b['code']})" for b in blocked))
        md.append("")

    # ── 形态剔除名单（2026-11-09 应用户「破位反弹要先删除掉」新增）
    #    纯展示：build() 已在出榜前完成过滤，这里只把被删的票和依据列出来，
    #    不写状态、不参与结算。异常绝不影响主闭环。
    try:
        import shape_gate
        _sx_md = shape_gate.render_md(r.get("shape_excluded") or [],
                                      total=r.get("shape_excluded_total"))
        if _sx_md:
            md += _sx_md
            md.append("")
    except Exception as e:
        print("  [警告] 形态剔除名单生成失败，不影响主流程：", e)

    # ── 启动首日观察榜（2026-11-09 应用户「能不能第一天就推荐」新增）
    #    纯展示：复用 build() 已算好的全市场因子行，不改主榜/权重/参数/状态，
    #    异常绝不影响主闭环。是否纳入正式选股仍走影子验证闸门，人工不得手改。
    try:
        import firstday_board
        _lixin = {b["code"] for b in blocked}
        _fd = firstday_board.render(r.get("pool") or [], _lixin)
        if _fd:
            md += _fd
            md.append("")
    except Exception as e:
        print("  [警告] 首日观察榜生成失败，不影响主流程：", e)

    # 影子假设跟踪段
    if sh and sh.get("md"):
        md += sh["md"]
        md.append("")
    if sh2 and sh2.get("md"):
        md += sh2["md"]
        md.append("")

    # 模型复盘
    try:
        import feedback
        md += feedback.review_md(fb.get("db") or [], fb.get("att"),
                                 fb.get("evo"), None, fb.get("hold_stats"))
        md.append("")
    except Exception:
        pass

    md.append("---")
    md.append("风控：跌破近10日最低价离场；10日内浮盈≥20% 分批了结；单票仓位≤1/5。")
    md.append("\n本内容为统计研究，不构成投资建议。")

    body = "\n".join(md)
    title = f"主升浪 V4 · {r['base_date']} 主推{len(latpicks)}只"
    open(os.path.join(HERE, "push_body.md"), "w", encoding="utf-8").write(body)
    json.dump(dict(changed=fb.get("changed"), n_new=fb.get("n_new"),
                   ver=ver, base_date=r["base_date"],
                   track=fb.get("track"), att=fb.get("att"),
                   evo=fb.get("evo"),
                   ts=datetime.datetime.now().isoformat(timespec="seconds")),
              open(os.path.join(HERE, "feedback_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if not push:
        print("\n[--nopush] 已跳过推送")
        _cloud_backup(r)
        return
    spec = importlib.util.spec_from_file_location("wechat_push", PUSH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    ok, msg = m.push(title, body)
    print(("[OK] 推送成功" if ok else "[FAIL] 推送失败") + "：" + msg)

    logf = os.path.join(HERE, "push_log.jsonl")
    with open(logf, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                                 date=r["base_date"], ver=ver, ok=bool(ok), msg=str(msg),
                                 picks=cur_codes,
                                 dsc=[p.get("dsc", 99) for p in latpicks]),
                            ensure_ascii=False) + "\n")
    _cloud_backup(r)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
