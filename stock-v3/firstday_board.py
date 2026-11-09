# -*- coding: utf-8 -*-
"""
启动首日观察榜（纯展示，2026-11-09 应用户「能不能第一天就推荐」新增）
================================================================================
【定位】只把「当天=放量突破启动日（dsc=0）且买得进」的票额外列出来给用户看，
        解决「推出来时已经涨了好几天」的体感问题。

【严格边界 —— 不碰模型】
  · 不改主榜选股（latpicks）、不改 9 因子权重 / factor_fit.json / holdout.json
  · 不改 model2.py 的 P 参数字典、不改任何交易规则
  · 不写 predictions.json / feedback_db 等状态文件，不参与结算、不进化、不记账
  · 不推送额外消息，只是主推送正文里追加一段 markdown
  · 数据来源：build() 已算好的全市场因子行 r["pool"]（内存复用，不重复读缓存）
  · 整段调用包在 try/except 里，异常绝不影响主闭环

【为什么不是「推荐」】
  历史回放：dsc=0 的结算样本极少；H1 影子假设模拟 438 条严格首日票，
  5 日胜率 33.8%、平均收益 -2.18%，且 78% 是涨停票收盘根本买不进。
  故本榜明确标注「观察/参考，不构成推荐」，是否纳入正式选股须走
  影子实盘 → 满 60 条结算 → 三重验证闸门裁定，人工不得手改。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _buyable(row):
    """与主模型 每日推荐.buyable 完全同口径：按板块涨停幅度剔除买不进的。"""
    try:
        import model2 as M
        lim = M.board_limit(row["code"])
    except Exception:
        lim = 9.5
    cap = 8.0 if lim < 15 else (15.0 if lim < 25 else 22.0)
    return row.get("day_chg", 0) < cap


def render(pool, lixin_blocked=None, maxn=8):
    """返回 markdown 行列表；无合格票或数据异常时返回 []（调用方需判空）。"""
    lixin_blocked = lixin_blocked or set()
    if not pool:
        return []
    cands = []
    for r in pool:
        if r.get("dsc") != 0:
            continue
        if r.get("code") in lixin_blocked:
            continue
        # 2026-11-09：与形态闸门保持一致，破位反弹票不进观察榜。
        # build() 过滤后 pool 本就不含这些票，这里是开关关闭/异常时的双重保险。
        if r.get("shape_bad"):
            continue
        if not _buyable(r):
            continue
        cands.append(r)
    if not cands:
        return []
    cands.sort(key=lambda x: -(x.get("score") or 0))

    md = ["### 👀 启动首日观察榜（dsc=0：今日刚放量突破，非推荐）",
          "　口径：当日涨≥5% 且量>2×20日均量、收盘创/近20日新高；"
          "已剔除涨停买不进、立信审计股与破位反弹票。",
          "　⚠️ 仅供观察，**不纳入主榜打分排序、不写状态、不参与结算**。历史回测未证明首日票更优，",
          "　　且首日多为涨停/大涨、次日实际难成交；是否纳入正式选股需走影子验证闸门（人工不得手改）。",
          ""]
    for i, r in enumerate(cands[:maxn], 1):
        name = (r.get("name") or r.get("code") or "").strip()
        gain = r.get("launch_gain")
        gain_s = f"累计起涨{gain:+.1f}%" if isinstance(gain, (int, float)) else "累计起涨—"
        chg = r.get("day_chg", 0)
        md.append(
            f"{i}. **{name}** `{r.get('code','')}`〔{r.get('board','')}〕"
            f" 收 {r.get('close',0):.2f}　当日 {chg:+.2f}%　{gain_s}"
            f"　量比 {r.get('day_vr',0):.2f}　模型分 {r.get('score',0):.1f}")
    if len(cands) > maxn:
        md.append(f"　… 另有 {len(cands)-maxn} 只合格首日票未列出（按模型分截尾）")
    return md
