# -*- coding: utf-8 -*-
r"""
形态闸门 · 剔除「破位反弹」（2026-11-09 用户要求）
================================================================================
问题
  v3.1「每日推荐.py」把「横盘收敛 + 首次放量突破箱体」简化成了 9 因子横截面
  百分位打分，丢掉了 v2 的形态约束（model2.P 里 box30_max=26 / converge_max=5）。
  后果：超跌破位后的第一根反弹大阳，chg5 / lu10 / brk20 / dist_hi 等因子数值
  都很大、百分位很高，反而被当成「强势突破」选进主榜。
  实盘 2026-11-09 六只主推里有 4 只属此类（智动力、侨银股份、华升股份、
  顺钠股份），用户指出「不是横盘收敛形态，是破位反弹，这种明显的错误筛选
  要先删除掉」。全市场复算：分层序前 20 名里 15 只都是「启动前创过 60 日新低」。

修正
  新增一道【形态硬闸门】，在横截面打分【完成之后】、新鲜度分层【之前】执行。
  不改 9 因子权重、不改打分、不改排序、不改板块配额、不改交易规则；
  不触碰 model2.P 参数字典 / factor_fit.json / holdout.json。
  与「立信审计一票否决」「buyable 涨停剔除」同级，都是出榜前的硬过滤。

判定范围
  仅对 dsc <= 5（近 5 日内确有放量启动动作）的票判定。没有「反弹」这个动作的
  横盘/阴跌票不叫破位反弹，且本来也不会进主榜，不该被本闸门管。
  实测：不加此限制会把全市场 85% 的票误列入剔除名单，语义与统计都被污染。

判定（3 条里命中 >=2 条 → 破位反弹，剔除）
  ① 先破位：本波启动日之前 10 个交易日内，创过 60 日新低  ← K线直接证据
  ② 没突破箱体：brk20 < -1%（收盘仍在前 20 日高点下方；与 H1「准突破」同口径）
  ③ 仍在深坑：brk60 < -12%（收盘仍低于 60 日前高 12% 以上）
  佐证（仅展示，不参与判定）：box30 近30日振幅（v2 收敛线 26%）、ma20up、chg60

为什么是「三选二」
  · 单看①会误杀「挖坑后真突破」的票：幸福蓝海 10-23 创新低，但 11-09 已
    brk20=+4.29 / brk60=-0.57，是真突破 → 只命中 1 条，保留。
  · 单看②/③会漏掉「已反弹到 20 日线上、但仍在 60 日深坑」的票：华升股份
    brk20=+0.41（不算②），但 brk60=-21.45 且启动前连创新低 → 命中 2 条，剔除。
  · 三选二正好覆盖「先破位 + 没走出来」这一类，且不误伤趋势票：美晨科技
    无创新低、brk20=+8.51、brk60=+8.51 → 命中 0 条，保留。
  · 用户点名的智动力、侨银股份均为 3/3 全命中。

开关
  环境变量 SHAPE_GATE=off 可一键停用，回到原行为（便于 A/B 对照与回退）。
  本模块异常时原样放行，绝不阻断主闭环。
"""
import os

# 阈值集中于此，便于审计；这些是【形态定义常数】，不是模型参数，不参与任何拟合
BRK20_MIN = -1.0        # 收盘需站上前 20 日高点，允许 1% 内的准突破
BRK60_MIN = -12.0       # 收盘距 60 日前高不得超过 -12%
NEWLOW_LOOKBACK = 10    # 启动日之前回溯 10 个交易日
NEWLOW_WINDOW = 60      # 60 日新低
MIN_HITS = 2            # 命中条数阈值
DSC_MAX = 5             # 只判近 5 日内有放量启动动作的票（必须有「反弹」）


def _pre_newlow(bars, idx, dsc):
    """启动日之前 NEWLOW_LOOKBACK 根内，是否出现过 60 日新低。返回日期列表。
    口径与 2026-11-09 全市场复算脚本一致（dsc=99 未启动时按最近一日回溯）。"""
    s = idx - dsc if dsc < 99 else idx
    if s < NEWLOW_WINDOW:
        return []
    lo = [x["low"] for x in bars]
    out = []
    for j in range(max(0, s - NEWLOW_LOOKBACK), s + 1):
        w = lo[max(0, j - NEWLOW_WINDOW + 1):j + 1]
        if w and lo[j] <= min(w) + 1e-9:
            out.append(bars[j]["date"])
    return out


def judge(row, bars=None, idx=None):
    """返回 (是否破位反弹, 命中理由列表, 证据dict)"""
    dsc = row.get("dsc", 99)
    # 没有启动动作 → 不是「反弹」，本闸门不管（避免把横盘/阴跌票一起误删）
    if dsc > DSC_MAX:
        return False, [], dict(box30=row.get("box30"), ma20up=row.get("ma20up"),
                               chg60=row.get("chg60"), brk20=row.get("brk20"),
                               brk60=row.get("brk60"), pre_newlow=[])
    reasons = []
    newlow = []
    if bars and idx is not None:
        newlow = _pre_newlow(bars, idx, dsc)
    if newlow:
        reasons.append(f"启动前10日内创60日新低({','.join(newlow[-2:])})")
    b20 = row.get("brk20", 0) or 0
    b60 = row.get("brk60", 0) or 0
    if b20 < BRK20_MIN:
        reasons.append(f"未突破20日箱体(brk20={b20:.2f}%)")
    if b60 < BRK60_MIN:
        reasons.append(f"仍处中期深坑(brk60={b60:.2f}%)")
    ev = dict(box30=row.get("box30"), ma20up=row.get("ma20up"),
              chg60=row.get("chg60"), brk20=b20, brk60=b60,
              pre_newlow=newlow)
    return len(reasons) >= MIN_HITS, reasons, ev


def apply(rows, cache, base_date=""):
    """
    给 rows 每行打 shape_bad / shape_reasons / shape_evidence 标记。
    返回 (保留行, 被剔除行)；被剔除行按 score 降序，供推送端展示。
    """
    if os.environ.get("SHAPE_GATE", "on").strip().lower() == "off":
        for r in rows:
            r["shape_bad"] = False
            r["shape_reasons"] = []
            r["shape_evidence"] = {}
        return rows, []
    kept, excluded = [], []
    for r in rows:
        bars = cache.get(r.get("code")) or []
        idx = len(bars) - 1
        if bars and r.get("date") and bars[-1].get("date") != r["date"]:
            dates = [x["date"] for x in bars]
            if r["date"] in dates:
                idx = dates.index(r["date"])
        try:
            bad, reasons, ev = judge(r, bars, idx) if bars else (False, [], {})
        except Exception:
            bad, reasons, ev = False, [], {}
        r["shape_bad"] = bool(bad)
        r["shape_reasons"] = reasons
        r["shape_evidence"] = ev
        (excluded if bad else kept).append(r)
    excluded.sort(key=lambda x: -(x.get("score") or 0))
    return kept, excluded


def render_md(excluded, maxn=8, total=None):
    """推送端展示：形态剔除名单（纯展示，不写状态、不参与结算）。
    excluded 是已按「原本会进榜优先」精简过的展示名单；total 是全市场实际剔除数。"""
    if not excluded:
        return []
    md = ["### 已剔除（破位反弹，非「横盘收敛 + 放量突破箱体」）",
          "　判定：启动前创 60 日新低 / 未突破 20 日箱体 / 仍处 60 日深坑，三选二即剔除；"
          "分数与排序均不变，仅在出榜前过滤。"]
    for b in excluded[:maxn]:
        ev = b.get("shape_evidence") or {}
        md.append(
            f"- **{b.get('name','')}** `{b.get('code','')}` 分{b.get('score',0):.0f}　"
            f"30日振幅 {ev.get('box30','-')}%｜距20日高 {ev.get('brk20','-')}%｜"
            f"距60日高 {ev.get('brk60','-')}%　{' + '.join(b.get('shape_reasons',[]))}")
    if total:
        md.append(f"　（本次形态闸门全市场共剔除 {total} 只，此处仅列与原榜相关的前 "
                  f"{min(maxn, len(excluded))} 只）")
    elif len(excluded) > maxn:
        md.append(f"　（另有 {len(excluded)-maxn} 只同类票未列出）")
    return md
