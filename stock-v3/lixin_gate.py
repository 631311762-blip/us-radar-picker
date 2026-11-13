# -*- coding: utf-8 -*-
r"""
立信审计硬闸门 · 接入流水线共同前置层（2026-11-13）
================================================================================
问题（用户反馈：「今天推荐的中重科技是立信审计的，不应该推送给我，
                  你是不是没识别流水线要求」）

  统一流水线 a-share-pipeline.yml 的共同前置层 common/prefilter.py 明确要求：
    ② 立信审计剔除 → 产出 common/state/lixin_exclude.json
    ③ 生成 universe.json（已物理剔除 ST + 立信的全市场样本池）
  其设计契约原文：「任何下游模型（哪怕自身没写立信/ST 判断）都不可能选到」。

  但主升浪 V4 在 common/registry.json 里被登记为：
    「自带 回溯/进化/选股/推送/云端备份 的独立子系统，
      不消费共同前置层 universe/K线缓存；settle=false 跳过公共回溯」
  → 主升浪走自己的 datafetch.universe() 拉候选池，绕开了流水线已剔除立信的
    universe.json；它自己的立信防线只有 每日推荐.apply_audit() → audit_check.main()
    实时联网抓东财公告，且只查候选【前 18 / 前 40 只】，本身就有覆盖盲区。

  2026-11-13 云端东财公告接口持续 502（当日多个提交在抢修「云IP持续502」），
  audit_result.json / audit_notices.json / audit_detail.json 又被 .gitignore 排除、
  不入库，GitHub Actions 干净 checkout 后无缓存 → 公告大面积抓取失败 →
  audit_check 把大量票判为 UNKNOWN → apply_audit 只剔除 status=="LIXIN" 的，
  UNKNOWN 被当成「通过」放行。
  后果：中重科技 sh603135（2026-04-27 续聘公告明确「拟聘任立信会计师事务所」，
  流水线 lixin_exclude.json 中 603135 依据 f10:立信会计师事务所）漏进主推第 3 位，
  18:02 推送成功。当日 blocked 仅 2 只（正常应远多于此），正是审计降级的旁证。

修正（本模块）
  在横截面打分【完成之后】、新鲜度分层【之前】，对【全部 rows】做一次离线硬过滤：
  直接消费流水线共同前置层产出的 common/state/lixin_exclude.json（已入库、当日
  16:05 前置层先跑刷新），命中裸 6 位代码即剔除。
    · 不依赖任何联网公告接口 → 东财 502 时依然生效
    · 与流水线口径完全一致（同一份名单，全市场 4607 只扫描，非候选前几十只）
    · 全量过滤，任何名次的立信票都进不了主榜，也进不了首日观察榜

严格边界（与「形态闸门」「立信一票否决」同级，都是出榜前硬过滤）
  · 不改 9 因子权重 / cdf / 打分 / 百分位 / 排序 / 板块配额 / 交易规则
  · 不触碰 model2.P 参数字典 / factor_fit.json / holdout.json
  · 不写 predictions.json / feedback_db 等状态，不参与结算、不触发进化
  · 名单缺失或异常时降级放行并打印醒目警告，绝不阻断主闭环（与 shape_gate 一致）
  · LIXIN_GATE=off 一键回退到 2026-11-13 修复前的行为

名单路径兼容两种布局：
  云端 CI  : <repo>/stock-v3/daily_v3.py      → <repo>/common/state/lixin_exclude.json
  本地沙箱 : /workspace/stock-v3/stock-v3/    → /workspace/stock-v3/common/state/...
  均可用环境变量 LIXIN_EXCLUDE_FILE 显式指定。
"""
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))

_PATHS = [
    os.environ.get("LIXIN_EXCLUDE_FILE", ""),
    os.path.join(HERE, "..", "common", "state", "lixin_exclude.json"),
    os.path.join(HERE, "..", "..", "common", "state", "lixin_exclude.json"),
    os.path.join(os.getcwd(), "common", "state", "lixin_exclude.json"),
    os.path.join(os.getcwd(), "..", "common", "state", "lixin_exclude.json"),
]

_MEMO = {"done": False, "path": None, "codes": set(), "reasons": {}, "updated": None}


def _load():
    if _MEMO["done"]:
        return _MEMO
    for p in _PATHS:
        if not p:
            continue
        p = os.path.abspath(p)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
            raw = d.get("codes") or []
            codes = {str(c).strip()[-6:] for c in raw if str(c).strip()}
            if not codes:
                continue
            _MEMO.update(done=True, path=p, codes=codes,
                         reasons=d.get("reasons") or {},
                         updated=d.get("updated_at") or d.get("stats", {}).get("updated_at"))
            return _MEMO
        except Exception:
            continue
    _MEMO["done"] = True
    return _MEMO


def enabled():
    return os.environ.get("LIXIN_GATE", "on").strip().lower() != "off"


def list_loaded():
    """名单文件是否成功加载（只看文件，不看任何开关）——供推送前最终硬闸门使用。"""
    return bool(_load()["codes"])


def available():
    """打分阶段闸门是否可用（受 LIXIN_GATE 控制）——供 apply_audit 兜底判定使用。
    推送前的最终硬闸门请用 list_loaded()，不受 LIXIN_GATE 影响。"""
    return enabled() and list_loaded()


def info():
    m = _load()
    return dict(path=m["path"], n=len(m["codes"]), updated=m["updated"])


def reason(code):
    return _load()["reasons"].get(str(code)[-6:], "")


def is_excluded(code):
    """code 可为 sh603135 / 603135；返回是否在流水线立信排除集内。"""
    return str(code)[-6:] in _load()["codes"]


def apply(rows):
    """打分后全量硬过滤：返回 (保留行, 被剔除行)；被剔除行按 score 降序。
    给每行打 lixin_bad / lixin_reason 标记，供首日观察榜等下游双重保险使用。"""
    if os.environ.get("LIXIN_GATE", "on").strip().lower() == "off":
        for r in rows:
            r["lixin_bad"] = False
            r["lixin_reason"] = ""
        return rows, []

    m = _load()
    codes = m["codes"]
    if not codes:
        # 关键：名单缺失属于防线降级，必须醒目告警，不能静默放行
        print("  [警告] 流水线立信排除集 common/state/lixin_exclude.json 未找到或为空，"
              "本闸门本次不生效；仅依赖联网 audit_check（东财 502 时可能漏剔）。")
        for r in rows:
            r["lixin_bad"] = False
            r["lixin_reason"] = ""
        return rows, []

    kept, excluded = [], []
    for r in rows:
        bare = str(r.get("code", ""))[-6:]
        hit = bare in codes
        r["lixin_bad"] = bool(hit)
        r["lixin_reason"] = m["reasons"].get(bare, "") if hit else ""
        (excluded if hit else kept).append(r)
    excluded.sort(key=lambda x: -(x.get("score") or 0))
    print(f"  立信闸门：按流水线排除集({len(codes)}只, {m['updated'] or '无时间戳'}) "
          f"全量剔除 {len(excluded)} 只")
    return kept, excluded


def guard_enabled():
    """推送前最终硬闸门开关，默认开。与打分阶段的 LIXIN_GATE 相互独立：
    LIXIN_GATE=off 只回退「出榜前过滤」（便于 A/B 对照），
    但绝不允许把立信票推给用户；要完全解除须显式 LIXIN_GUARD=off。"""
    return os.environ.get("LIXIN_GUARD", "on").strip().lower() != "off"


def main_picks_hit(picks):
    """推送前最终校验：返回主榜中仍命中流水线立信名单的票（正常应为空）。"""
    return [p for p in picks if is_excluded(p.get("code", ""))]


def render_md(excluded, maxn=8, total=None):
    """可选：展示被离线闸门剔除的高分票及依据（纯展示，不写状态）。"""
    if not excluded:
        return []
    md = ["### 已剔除（流水线立信排除集，离线硬闸门）",
          "　口径：消费统一流水线共同前置层产出的 `common/state/lixin_exclude.json`；"
          "不依赖联网公告，东财接口异常时仍生效。"]
    for b in excluded[:maxn]:
        md.append(f"- **{b.get('name','')}** `{b.get('code','')}` "
                  f"分{b.get('score',0):.1f}　{b.get('lixin_reason') or '立信会计师事务所'}")
    if total and total > maxn:
        md.append(f"　（全市场共剔除 {total} 只，此处仅列与原榜相关的高分票）")
    return md
