#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prefilter.py —— 共同前置层入口（合并流水线第一步，全局强制，仅跑一次）
========================================================================
五个共同条件的落地顺序（在单 runner 内只跑一次，供所有模型复用）：

  ① 全市场行情记录   : 批量缓存全市场 K 线 → common/state/klines/<code>.csv
  ② 立信审计剔除     : 生成 lixin_exclude.json（25年报审计=立信 OR 26续聘改聘公告改立信）
  ③ ST 全剔除        : 生成 universe.json 样本池时按名称硬剔除 ST/*ST/退市/次新
  ④ 回溯推荐+修正模型 : 由 run_all.py 的 settle_all() 对注册表所有模型统一结算/调参
                        （本步产出 universe 后即具备条件，实际结算在模型跑完后）
  ⑤ 监管立案剔除     : 生成 regulatory_exclude.json（近一年被立案调查/涉嫌违法，
                        即使未被 ST 也物理剔除，滚动 365 天自动失效）
  ⑥ 财报窗口标注     : 刷新 report_calendar.json（定期报告预约披露日），仅用于
                        微信推送时给「临近披露」的入选股票加醒目标识，不影响选股。
  ⑦ 大解禁剔除       : 生成 unlock_exclude.json（过去+未来 30 天内「解禁股数/
                        总股本 ≥ 10%」的股票；每周六识别，周中复用）。

  ② ③ ⑤ ⑦ 属于「准入剔除」，在样本池生成阶段物理生效 —— 样本池里根本不含这些
  代码，任何下游模型（哪怕自身没写立信/ST 判断）都不可能选到，实现
  「即使模型条件里没有，也是遗漏补上」的强制约束。

产物（common/state/）：
  lixin_exclude.json  立信排除集（含命中依据）
  regulatory_exclude.json  近一年监管立案/涉嫌违法排除集（滚动窗口）
  unlock_exclude.json 大额限售解禁排除集（前后 30 天，每周六识别）
  universe.json       已剔除 ST + 立信 + 监管立案 + 大解禁 的全市场样本池
  market_daily/       2024 年起全市场日 K 分片（跨 runner 持久化，回测离线读取）
  klines/*.csv        当日模型使用的物化 K 线缓存（可重建，不入库）
  meta.json           本次运行元信息（trading_day / 各条件统计 / 降级状态 / 告警）

2026-11-13 变更（诊断报告第 2、3 项）：
  · fail-closed → fail-degraded：样本池为空且基础快照健康时，用快照强制重建
    样本池继续跑，而不是直接 exit 1 让整条流水线标红；快照也不可用才判定
    真实故障（保留失败状态，交给聚合告警与人工介入）。
  · base_universe.json 增加完整性校验（条数 / 字段 / 价格健全率），异常即告警。
  · K 线缓存覆盖率、熔断状态、降级标记写入 meta.json，覆盖率不足时推送告警，
    避免下游模型「静默使用残缺缓存」而无人知晓。
  · 告警一律 best-effort：渠道缺失/网络异常只打印日志，绝不影响退出码。

2026-11-21 提速（日行情 / 周刷新分流）：
  立信否决、监管立案、基础股票池快照（base_universe.json）的全量扫描很慢，
  每天跑会把前置层拖到 10+ 分钟。改为按星期分流——
    · 周一~周五（日增量模式）：只刷新全市场行情（K 线）+ 财报日历，
      立信/监管立案直接复用上一周六产出的排除集，样本池用缓存的
      base_universe.json 构建（ST/立信/监管剔除仍在内存生效），
      整体只留行情增量这一条耗时链路。
    · 周六（周刷新模式）：休市无新行情，跳过 K 线；集中跑立信
      （--ann-full + --f10-full 全量）、监管立案（滚动一年 full 基线）、
      大额限售解禁（前后 30 天，重新识别），并从实时列表重建
      base_universe.json / universe.json。下游模型因
      trading_day=false 自动跳过，本周一~周五即用新刷新的信息选股。
  模式由星期几自动判定（周六=5），也可用 --weekly-refresh / --no-weekly-refresh
  显式覆盖；周中发生的 ST/立信/监管变化要等到下周六才会反映（已确认取舍）。

用法：
  python3 common/prefilter.py                  # 自动按星期判定（周中增量 / 周六周刷新）
  python3 common/prefilter.py --weekly-refresh  # 强制周刷新（非周六手动补跑）
  python3 common/prefilter.py --no-weekly-refresh  # 周六也只跑日增量（复用排除集）
  python3 common/prefilter.py --f10-only      # 立信仅查 F10（快，跳过公告扫描）
  python3 common/prefilter.py --no-kline      # 跳过 K 线批量（调试）
  python3 common/prefilter.py --no-lixin     # 跳过立信更新（复用已有排除集）
"""
import os
import sys
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common_lib as cm

try:
    import notify
except Exception as _e:      # 告警模块异常绝不能阻断选股
    notify = None
    print("[prefilter] 告警模块不可用（不影响选股）：%s" % _e)

BJT = cm.BJT
ALERTS = []                 # 本次运行的告警汇总，写入 meta.json


def _alert(title, content):
    """尽最大努力发一条告警；任何失败都只记录，不影响流水线退出码。"""
    ALERTS.append({"title": title, "content": content})
    print("[prefilter][ALERT] %s | %s" % (title, content.replace("\n", " ")))
    if notify is None:
        return
    try:
        ok, detail = notify.send_alert(title, content)
        print("[prefilter][ALERT] 推送 %s：%s" % ("成功" if ok else "未送达", detail))
    except Exception as e:
        print("[prefilter][ALERT] 推送异常（忽略）：%s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--f10-only", action="store_true", help="立信仅 F10 通道，跳过 26 公告扫描")
    ap.add_argument("--f10-full", action="store_true", help="立信 A 通道强制全量重扫 F10（默认复用已入库命中，省约4分钟）")
    ap.add_argument("--ann-full", action="store_true", help="立信 B 通道跑 2026 全年基线（默认增量窗口）")
    ap.add_argument("--no-kline", action="store_true", help="跳过全市场 K 线批量缓存（调试）")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过当日行情快照批量写入，退回逐只 K 线（调试/回退）")
    ap.add_argument("--no-fast-universe", action="store_true", help="禁用样本池快照优先（回退东财列表退避重试，调试用）")
    ap.add_argument("--no-kz", action="store_true", help="跳过凯众不复权 K 线预填（调试）")
    ap.add_argument("--no-lixin", action="store_true", help="跳过立信排除集更新，复用已有")
    ap.add_argument("--no-regulatory", action="store_true", help="跳过监管立案排除集更新，复用已有")
    ap.add_argument("--no-unlock", action="store_true", help="跳过大解禁排除集更新，复用已有")
    ap.add_argument("--reg-full", action="store_true", help="监管立案扫描滚动一年基线（首次建库）")
    ap.add_argument("--no-report-risk", action="store_true", help="跳过定期报告预约披露日历刷新，复用已有")
    ap.add_argument("--weekly-refresh", action="store_true",
                    help="强制周刷新模式：全量重扫立信/监管立案/基础快照（周六自动启用）")
    ap.add_argument("--no-weekly-refresh", action="store_true",
                    help="禁用周刷新模式：即使周六也只更新行情，复用上周排除集")
    args = ap.parse_args()

    # ---- 运行模式判定：周六自动周刷新，其余日增量；显式 flag 可覆盖 ----
    weekday = datetime.datetime.now(BJT).weekday()   # 周一=0 ... 周日=6
    if args.weekly_refresh:
        weekly_refresh = True
    elif args.no_weekly_refresh:
        weekly_refresh = False
    else:
        weekly_refresh = (weekday == 5)               # 周六=5
    _wd_cn = "一二三四五六日"[weekday]
    print("[prefilter] 星期%s → 运行模式：%s"
          % (_wd_cn, "周刷新（立信/监管立案/快照全量，跳过 K 线）"
             if weekly_refresh else "日增量（仅行情，复用上周排除集）"))

    today = datetime.datetime.now(BJT).strftime("%Y-%m-%d")
    print("=" * 64)
    print("共同前置层 prefilter  %s" % today)
    print("=" * 64)

    # 【提速】快照优先开关：健康的基础快照直接复用 + 腾讯批量补当日价，
    # 跳过东财列表接口在云 IP 下的 300s×多轮退避重试（实测烧掉约 10 分钟）。
    fast_universe = not args.no_fast_universe
    print("[prefilter] 样本池快照优先：%s" % ("开启" if fast_universe else "关闭"))

    meta = {
        "date": today,
        "generated_at": datetime.datetime.now(BJT).isoformat(timespec="seconds"),
        "degraded": False,
        "alerts": ALERTS,
    }

    # ---- 交易日 / 行情更新判定 ----
    is_td, quote_date, _ = cm.check_trading_day()
    meta["quote_date"] = quote_date
    meta["trading_day"] = bool(is_td)
    meta["weekly_refresh"] = bool(weekly_refresh)
    meta["weekday"] = weekday
    # 周刷新模式（周六）休市无新行情，但仍需跑立信/监管立案/快照全量，
    # 因此绕开「非交易日直接返回」的硬闸门；其余非交易日（周日/节假日）照旧跳过。
    if not is_td and not weekly_refresh:
        print("[prefilter] 非交易日 / 行情未更新（最新日 %s ≠ 当日 %s），"
              "整体跳过，仅产出 meta。" % (quote_date, today))
        cm.save_meta(meta)
        return 0

    # ---- 基础股票池快照健康校验（降级兜底的数据源必须先可信）----
    snap_ok, snap_reason, snap_rows = cm.check_base_universe_health()
    meta["snapshot_health"] = {
        "ok": snap_ok, "reason": snap_reason, "rows": len(snap_rows),
    }
    print("[prefilter] 基础快照健康校验：%s（%d 只）"
          % ("PASS" if snap_ok else "FAIL: " + snap_reason, len(snap_rows)))
    if not snap_ok:
        _alert("基础股票池快照异常",
               "base_universe.json 健康校验失败：%s。\n"
               "一旦实时行情接口被阻断，流水线将失去兜底数据源，请尽快重新种入富字段快照。"
               % snap_reason)

    # ---- 模式生效开关：周刷新全量 / 日增量复用（显式 flag 始终优先） ----
    refresh_base_snapshot = weekly_refresh              # 周六从实时列表重建 base_universe.json
    do_lixin_full = weekly_refresh and not args.no_lixin      # 周六立信全量：F10 重扫 + 公告全年
    do_reg_full = weekly_refresh and not args.no_regulatory   # 周六监管立案：滚动一年 full 基线
    skip_kline = args.no_kline or weekly_refresh        # 周六休市无新行情，跳过 K 线与凯众预填

    # ---- 条件②：立信审计排除集（25年报=立信 OR 26续聘/改聘公告改立信） ----
    # 日增量（周一~五）直接复用上周六产出的排除集，不再扫描；周六周刷新才全量重扫。
    if args.no_lixin or not weekly_refresh:
        lixin = cm.load_lixin_exclude()
        why = "显式 --no-lixin" if args.no_lixin else "日增量模式（非周六）"
        print("[prefilter] %s：复用已有立信排除集 %d 只" % (why, len(lixin)))
        meta["lixin_count"] = len(lixin)
        meta["lixin_reused"] = True
    else:
        import lixin as lx
        base_u = cm.get_universe(verbose=False, apply_lixin=False,
                                 apply_regulatory=False, apply_unlock=False,
                                 prefer_snapshot=(not refresh_base_snapshot))
        codes = [x["code"] for x in base_u]
        # F10 通道逐股扫描约 4 分钟，命中结果与历史并集合并、永不自动释放（见 lixin.build）。
        # 日常（非周六）本分支不会进入；周刷新强制 F10 全量重扫 + 公告全年基线，
        # 手动 --f10-only / --f10-full / --ann-full 仍可覆盖。
        has_f10_base = bool(cm.load_lixin_exclude())
        skip_f10 = (not args.f10_full) and has_f10_base and (not do_lixin_full)
        if skip_f10:
            print("[prefilter] 立信 A 通道(F10)复用已入库命中（省约4分钟）；"
                  "全量重扫请加 --f10-full")
        else:
            print("[prefilter] 立信 A 通道(F10)全量重扫（周刷新 / --f10-full）")
        _, stats = lx.build(codes, do_ann=not args.f10_only,
                            ann_mode="full" if (args.ann_full or do_lixin_full) else "incremental",
                            skip_f10=skip_f10)
        meta["lixin_count"] = stats.get("total_excluded", 0)
        meta["lixin_stats"] = stats
        meta["lixin_f10_skipped"] = bool(skip_f10)
        meta["lixin_reused"] = False

    # ---- 条件⑤：近一年监管立案/涉嫌违法排除集（滚动 365 天自动失效） ----
    # 日增量（周一~五）复用上周六产出的排除集；周六周刷新跑滚动一年 full 基线。
    if args.no_regulatory or not weekly_refresh:
        reg = cm.load_regulatory_exclude()
        why = "显式 --no-regulatory" if args.no_regulatory else "日增量模式（非周六）"
        print("[prefilter] %s：复用已有监管立案排除集 %d 只" % (why, len(reg)))
        meta["regulatory_count"] = len(reg)
        meta["regulatory_reused"] = True
    else:
        import regulatory as rg
        base_u = cm.get_universe(verbose=False, apply_lixin=False,
                                 apply_regulatory=False, apply_unlock=False,
                                 prefer_snapshot=(not refresh_base_snapshot))
        # 首次部署没有排除集文件或周刷新，跑滚动一年基线，避免只扫最近 10 天漏掉
        # 一年内已发生的立案；其余日增量本分支不进入。
        first_run = not os.path.exists(cm.REGULATORY_JSON)
        rmode = "full" if (args.reg_full or first_run or do_reg_full) else "incremental"
        _, rstats = rg.build([x["code"] for x in base_u], mode=rmode)
        meta["regulatory_count"] = rstats.get("total_excluded", 0)
        meta["regulatory_stats"] = rstats
        meta["regulatory_reused"] = False
        if rstats.get("scan_empty"):
            _alert("监管立案扫描为空",
                   "本次公告接口扫描 0 条（可能被限流），已保留历史排除记录、"
                   "未释放任何标的；请检查东财公告接口连通性。")

    # ---- 条件⑦：大额限售解禁排除集（过去+未来 30 天，解禁股数/总股本 ≥ 10%）----
    # 周六周刷新重建；周中复用上周六产出，读取时按当天窗口自动失效。
    if args.no_unlock or not weekly_refresh:
        unlock = cm.load_unlock_exclude()
        why = "显式 --no-unlock" if args.no_unlock else "日增量模式（非周六）"
        print("[prefilter] %s：复用已有大解禁排除集 %d 只" % (why, len(unlock)))
        meta["unlock_count"] = len(unlock)
        meta["unlock_reused"] = True
    else:
        try:
            import unlock_risk as ur
            _, ustats = ur.build(verbose=True)
            meta["unlock_count"] = ustats.get("total_excluded", 0)
            meta["unlock_stats"] = ustats
            meta["unlock_reused"] = False
            if ustats.get("scan_empty"):
                _alert("解禁数据扫描为空",
                       "东财解禁接口本次返回 0 条（可能被限流），已保留历史"
                       "排除记录、未释放任何标的；请检查接口连通性。")
        except Exception as e:
            unlock = cm.load_unlock_exclude()
            print("[prefilter] 大解禁识别失败，复用已有排除集 %d 只：%s"
                  % (len(unlock), e))
            meta["unlock_count"] = len(unlock)
            meta["unlock_reused"] = True
            meta["unlock_error"] = str(e)
            _alert("大解禁识别失败",
                   "本次未能刷新大额解禁排除集：%s。\n已复用上次结果（%d 只），"
                   "选股不受阻断，但可能遗漏本周新增解禁标的。"
                   % (e, len(unlock)))

    # ---- 条件③ + 生成样本池（ST 名称硬剔除 + 立信/监管立案/大解禁物理剔除） ----
    # 日增量复用缓存 base_universe.json（prefer_snapshot，仅腾讯补当日价）；
    # 周刷新从实时列表拉取并刷新 base_universe.json。--no-fast-universe 在日增量下仍可关闭快照优先。
    universe = cm.get_universe(prefer_snapshot=(not refresh_base_snapshot) and fast_universe)  # 内部已应用 ST + 立信 + 监管立案 + 大解禁 剔除
    if not universe:
        # fail-degraded：常规路径（已含 get_universe 内部的快照回退）都拿不到样本池，
        # 再用「已通过健康校验」的快照强制重建一次，而不是直接 exit 1 让整条流水线红。
        if snap_ok:
            print("[prefilter][ALERT] 实时样本池为空，尝试用基础快照降级继续（%s）。" % snap_reason)
            universe = cm.get_universe(force_snapshot=True)
        if not universe:
            _alert("前置层真实故障：样本池为空",
                   "实时行情与基础快照均不可用（%s），本次流水线终止。\n"
                   "这属于真实故障，保留失败状态以便聚合告警与人工介入。" % snap_reason)
            meta["trading_day"] = False
            meta["error"] = "universe_empty"
            meta["degraded"] = False
            cm.save_meta(meta)
            return 1
        meta["degraded"] = True
        _alert("前置层已降级：使用基础股票池快照",
               "实时行情源异常，样本池改由基础快照构建（%d 只）。\n"
               "ST / 立信硬剔除照常生效，但价格字段为快照口径（可能非当日），请留意结果口径。"
               % len(universe))
    cm.save_universe(universe)
    meta["universe_count"] = len(universe)
    ustatus = cm.universe_status()
    meta["universe_source"] = "snapshot" if meta["degraded"] else ustatus.get("source", "realtime")
    from collections import Counter
    meta["board_dist"] = dict(Counter(x["board"] for x in universe))

    # ---- 条件①：全市场行情记录（一次性 K 线缓存，供所有模型复用） ----
    # 周刷新（周六）休市无新行情，跳过 K 线批量；日增量才追加当日一根。
    if skip_kline:
        why = "--no-kline" if args.no_kline else "周刷新/休市（无新行情）"
        print("[prefilter] 跳过 K 线批量缓存（%s）。" % why)
        meta["kline"] = {"skipped": True}
    else:
        market_universe = cm.get_market_universe()
        meta["market_universe_count"] = len(market_universe)

        # 【提速核心】历史分片已完整持久化（2024 起），每日只需追加当日一根。
        # 用腾讯批量快照一次写入当日全市场（约数秒），替代逐只 K 线抓取
        # （原 5576 只 × 超时叠加 = 前置层 22 分钟的主因）。
        snap = {"inserted": 0, "ok": 0, "targets": 0, "coverage": 0.0}
        if not args.no_snapshot:
            try:
                snap = cm.update_today_bars(market_universe, quote_date, verbose=True)
            except Exception as e:
                print("[prefilter] 当日快照写入异常（回退逐只抓取）：%s" % e)
                snap = {"error": str(e)}
        meta["snapshot_bars"] = snap

        # 快照已覆盖标的的 last_date 已更新到当日，fetch_klines_batch 会自动判
        # 为 fresh 跳过逐只抓取；本步退化为「补快照漏掉的少数标的 + 物化 CSV」。
        ok, fail = cm.fetch_klines_batch(
            market_universe, count=cm.KLINE_CACHE_COUNT,
            workers=cm.KLINE_WORKERS, quote_date=quote_date,
            materialize_codes=[x["code"] for x in universe])
        meta["kline_ok"] = ok
        meta["kline_fail"] = fail
        meta["kline"] = cm.kline_stats()
        meta["market_history"] = cm.MARKET_STORE.coverage()
        st = meta["kline"]
        if st.get("degraded"):
            _alert("共享K线缓存覆盖率不足",
                   "成功 %d / 需拉 %d（覆盖率 %.1f%%），失败 %d、超预算跳过 %d。\n"
                   "缺失标的会被下游模型跳过，不再逐模型访问外网；"
                   "若持续发生请调 KLINE_BATCH_BUDGET 或检查兜底源。"
                   % (st["ok"], st["targets"], st["coverage"] * 100,
                      st["fail"], st["skip"]))

    # ---- 凯众专用：不复权 K 线预填（使凯众零网络抓取，口径与新浪逐日一致）----
    if skip_kline or args.no_kz:
        print("[prefilter] 跳过凯众不复权 K 线预填。")
    else:
        try:
            kz_ok, kz_fail = cm.cache_kaizhong_klines(universe)
            meta["kz_kline_ok"] = kz_ok
            meta["kz_kline_fail"] = kz_fail
            meta["kz_kline"] = cm.kline_stats("kaizhong")
            st = meta["kz_kline"]
            if st.get("degraded"):
                _alert("凯众不复权K线覆盖率不足",
                       "成功 %d / 需预填 %d（覆盖率 %.1f%%），失败 %d、超预算跳过 %d；"
                       "凯众离线模式将跳过缺失标的，不再自行访问新浪。"
                       % (st["ok"], st["targets"], st["coverage"] * 100,
                          st["fail"], st["skip"]))
        except Exception as e:
            # 失败不阻断其它模型；凯众在正式流水线中离线跳过缺失标的。
            print("[prefilter] 凯众不复权 K 线预填失败（凯众将离线跳过缺失标的）：%s" % e)
            meta["kz_kline_error"] = str(e)

    # ---- 条件⑥：定期报告/业绩预告临近标注（仅推送提示，不影响选股）----
    if args.no_report_risk:
        print("[prefilter] --no-report-risk：复用既有预约披露日历。")
        meta["report_risk"] = {"skipped": True}
    else:
        try:
            import report_risk as rr
            rp = rr.refresh(verbose=True)
            meta["report_risk"] = rp.get("stats")
            if rp.get("stats", {}).get("scan_empty"):
                _alert("预约披露日历为空",
                       "东财预约披露接口返回 0 条（可能被限流或尚未开放下一报告期），"
                       "已保留既有日历；本次推送的财报临近标识可能不完整。")
        except Exception as e:
            # 日历刷新失败不影响选股与推送，只是缺少「临近财报」标注。
            print("[prefilter] 预约披露日历刷新失败（不影响选股）：%s" % e)
            meta["report_risk"] = {"error": str(e)}
            _alert("预约披露日历刷新失败",
                   "定期报告/业绩预告临近标注本次不可用：%s。\n"
                   "选股与推送不受影响，但入选股票的财报窗口提示可能缺失。" % e)

    # ---- 可观测性汇总：写入 meta.json 供 workflow Step Summary / 排障使用 ----
    meta["data_source_status"] = cm.universe_status()
    meta["kline_source_status"] = cm.kline_source_status()
    cm.save_meta(meta)
    print("[prefilter] 完成：模式=%s，样本池 %d 只，立信排除 %d 只（%s），"
          "监管立案排除 %d 只（%s），大解禁排除 %d 只（%s），财报窗口 %s 只，"
          "trading_day=%s，来源=%s，降级=%s，告警=%d 条"
          % ("周刷新" if weekly_refresh else "日增量",
             len(universe), meta.get("lixin_count", 0),
             "复用" if meta.get("lixin_reused") else "全量重扫",
             meta.get("regulatory_count", 0),
             "复用" if meta.get("regulatory_reused") else "全量重扫",
             meta.get("unlock_count", 0),
             "复用" if meta.get("unlock_reused") else "重建",
             (meta.get("report_risk") or {}).get("in_window", "-"), is_td,
             meta["universe_source"], meta["degraded"], len(ALERTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
