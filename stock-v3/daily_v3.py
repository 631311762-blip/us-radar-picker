# -*- coding: utf-8 -*-
r"""
主升浪 V5「奥雅式放量突破」· 每日收盘后自动跑【回溯 → 进化 → 选股 → 推送】
================================================================================
2026-11-18 用户授权：禁改文件约束解除，主榜由 v3.1 切换为「奥雅最优」模型
（oya_model.py，试盘→破位→盘中放量突破；旧 v3.1 降为参照观察）。

执行顺序很关键，闭环才成立：
  ① feedback.run()  回溯昨日推荐 → 结算到期样本 → 因子归因 → 条件更新权重
  ② 重载模型模块（若权重已更新，必须重新读取 factor_fit.json）
  ③ oya_model.build() 用奥雅形态准入 + 当天最新权重打分选股（旧v3.1作参照）
  ④ oya_model.record() 记录今日推荐（含形态证据，供日后归因）
  ⑤ 推送微信（2026-11-18 起）：今日奥雅主榜样本 + 历史推荐持续表现
                    其余（旧模型参照/昨日跟踪/首日观察榜/影子假设/模型复盘/剔除名单）
                    仅内部计算与记账，不再进微信正文

用法：
  python daily_v3.py                # 完整跑（全量输出当日合格信号）
  python daily_v3.py --nopush       # 只跑不推
  python daily_v3.py --noevolve     # 跳过进化，只用现权重选股
  python daily_v3.py --legacy       # 额外跑旧 v3.1 作为参照（默认用同池轻量参照）
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

    push = True
    evolve = True
    args = sys.argv[1:]
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

    # ── ②③ 加载奥雅模型并选股（失败必须中止，绝不推空榜——2026-11-02 空榜事故的教训）
    try:
        import oya_model as OY
        import model2 as M
        if fb.get("changed"):
            importlib.reload(OY)         # 关键：重新读取 factor_fit.json 的新权重
        ver = json.load(open(os.path.join(HERE, "factor_fit.json"),
                             encoding="utf-8")).get("ver", "v3.1")
        print("\n" + "=" * 78)
        print(f"【第二步】奥雅式形态准入 + {ver} 权重打分选股（旧v3.1作参照）")
        print("=" * 78)
        r = OY.build()

        # ── ②③b 推送前最终硬闸门（2026-11-13 用户：「立信审计票不该推送给我」）
        #    无条件生效：只要流水线共同前置层产出的 lixin_exclude.json 存在，
        #    主榜里出现名单内个股就直接 ABORT —— 不 record、不推送、不污染台账。
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

        OY.show = getattr(OY, "show", None)
        if OY.show:
            OY.show(r)
        else:
            for p in r["latpicks"]:
                print(f"  主推 {p['name']} {p['code']} 分{p['score']}")
        if "--norecord" not in sys.argv:   # 调试用：跳过预测记录写入，不污染结算口径
            OY.record(r)
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

    # ── ⑤ 推送前置校验（2026-11-02 空榜事故的教训：数据源断裂时照样"推送成功"，
    #    浪费 Server酱 额度）。V5 奥雅：主榜当天无形态命中是合法结果（宁缺勿滥），
    #    但若候选池本身异常（build 已抛错中止）则不推送——build 的成功即代表数据正常。
    if r.get("universe", 0) < 1000:
        print("\n[FAIL] 候选池异常（<1000），已拒绝推送")
        with open(logf, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(dict(
                ts=datetime.datetime.now().isoformat(timespec="seconds"),
                date=r["base_date"], ok=False, msg="候选池异常拦截"),
                ensure_ascii=False) + "\n")
        sys.exit(4)
    if not latpicks:
        print("\n[提示] 今日无符合奥雅形态的新提示日，将推送空榜说明+历史跟踪。")
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

    # ── ⑤ 组装推送（2026-11-18 起用户要求：微信只推【新模型样本】+【历史推荐持续表现】）
    #    旧 v3.1 参照、昨日跟踪、首日观察榜、影子假设、模型复盘、各类剔除名单
    #    一律不再进微信正文；各模块自身的计算/记账闭环照常执行，不受影响。
    md = [f"> **主升浪 V5·奥雅式突破**｜基准日 **{r['base_date']}**｜候选池 {r['universe']} 只\n"]

    # 新模型当日样本（试盘→破位→盘中放量突破首日推）
    try:
        md += OY.render_oya_md(r)
    except Exception as e:
        print("  [警告] 奥雅主榜渲染异常：", e)
        for i, p in enumerate(latpicks, 1):
            md.append(f"{i}. {fmt(p)}")

    # 历史推荐持续表现（只跟涨的，破位/放量下跌/阴包阳即停）
    try:
        md += OY.render_follow_md(r)
    except Exception as e:
        print("  [警告] 奥雅跟踪段生成失败，不影响主流程：", e)

    # 模型原型标杆样本（用户 2026-11-20 要求，推文结尾）
    try:
        md += OY.render_samples_md()
    except Exception as e:
        print("  [警告] 标杆样本段生成失败，不影响主流程：", e)

    md.append("---")
    md.append("风控：提示日收盘价买；跌破试盘支撑/突破日最低×0.96 离场；持有5个交易日；"
              "次日开盘涨幅>7%不追；单票仓位≤1/5。")
    md.append("\n📌 模型原型：以上选股基于奥雅股份(300949)、冰山冷热(000530)、果麦文化(301052)，提示日为放量突破首日（奥雅09-08试盘→10-21突破、冰山冷热09-03试盘→10-13突破、果麦文化09-01试盘→10-12突破），基本特征：横盘→放量试盘→破位洗盘→放量突破首日得出。")
    md.append("\n📈 最优策略：提示日次日开盘买入，持有至次日(D+2)最高价卖出（2026年回溯胜率66%、平均收益+3.6%，按盘中最高价理想成交）；跌破买入价-5%止损（止损用于控制回撤，回溯显示会略降理想收益）。")
    md.append("\n本内容为统计研究，不构成投资建议。")

    body = "\n".join(md)
    title = f"主升浪V5·奥雅突破 · {r['base_date']} 主推{len(latpicks)}只"
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
