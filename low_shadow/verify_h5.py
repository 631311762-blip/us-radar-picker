#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H5 落地端到端验证：参数加载、筛选口径、调参保护、归档快照。"""
import sys, json, copy, os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import low_shadow_screener as m

m.DEMO = False
BASE = os.path.join(_HERE, "state")
ok_all = True


def chk(tag, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", tag, extra))


print("=" * 90)
print("【1】参数加载：params.json 与 DEFAULT_PARAMS 一致")
p = m.load_params()
for k in ("upper_amp_min", "vol_ratio_min", "low_pos_max_pct", "h5_enabled",
          "h5_max_drop_pct", "h5_vol_ratio_max", "h5_amount_max",
          "param_lock", "min_tune_samples", "candidate_pool"):
    chk("load_params[%s]" % k, p[k] == m.DEFAULT_PARAMS[k], "= %r" % (p[k],))
NEW_PREFIX = ("000", "001", "002", "003", "300", "301", "302",
              "600", "601", "603", "605", "688", "689")
chk("板块前缀(放主板/排除北交所)", tuple(p["h5_board_prefix"]) == NEW_PREFIX)

print()
print("=" * 90)
print("【2】H5 筛选口径：322 条回测样本应得 238 只，11-13 应得 11 只")
print("    （2026-11-17 起按用户要求把主板放回来；旧口径只创/科为 123 / 10 只）")
d = json.load(open(BASE + "/bt_oct_result.json", encoding="utf-8"))
picks = d["picks"]


def ref_h5(x):  # 独立复现当前 H5 口径（放行主板+创/科创，排除北交所）
    s = x["symbol"]
    return (s.startswith(NEW_PREFIX)
            and not s.startswith(("4", "8", "92"))  # 北交所兜底排除
            and x["pct_chg"] > -3.0 and x["vol_ratio"] <= 3.0
            and x["amount"] <= 1e9)


got = set(x["symbol"] + "@" + x["date"] for x in picks
          if m.h5_check(x["symbol"], x, p)[0])
ref = set(x["symbol"] + "@" + x["date"] for x in picks if ref_h5(x))
chk("全窗口 H5 样本数 = 238", len(got) == 238, "实得 %d" % len(got))
chk("与回测参考实现逐条一致", got == ref, "差异 %d 条" % len(got ^ ref))

y13 = [x for x in picks if x["date"] == "2026-11-13"]
got13 = [x for x in y13 if m.h5_check(x["symbol"], x, p)[0]]
chk("11-13 基线 13 只", len(y13) == 13, "实得 %d" % len(y13))
chk("11-13 H5 保留 11 只", len(got13) == 11, "实得 %d" % len(got13))
ref13 = [x for x in y13 if ref_h5(x)]
chk("11-13 与回测参考一致", set(x["symbol"] for x in got13) == set(x["symbol"] for x in ref13))
print("     H5 保留:", "、".join("%s %s" % (x["symbol"], x["name"]) for x in sorted(got13, key=lambda z: -z["score"])))
print("     H5 剔除:", "、".join("%s %s(%s)" % (x["symbol"], x["name"], m.h5_check(x["symbol"], x, p)[1])
                              for x in y13 if x not in got13))

print()
print("  --- 官方归档(东财真实成交额) 13 只交叉验证 ---")
off = json.load(open(BASE + "/qualified_20261113_171842.json", encoding="utf-8"))["candidates"]
off13 = [x for x in off if m.h5_check(x["symbol"], x, p)[0]]
chk("官方口径 H5 也为 11 只", len(off13) == 11, "实得 %d" % len(off13))
chk("官方口径与回测口径集合一致",
    set(x["symbol"] for x in off13) == set(x["symbol"] for x in got13))

print()
print("  --- H5 精选层漏斗（全窗口，按首个失败原因）---")
funnel = {}
for x in picks:
    ok, why = m.h5_check(x["symbol"], x, p)
    if not ok:
        funnel[why] = funnel.get(why, 0) + 1
print("    ", funnel, "| 合计剔除", sum(funnel.values()), "| 保留", len(got))
chk("漏斗合计 = 322 - 238", sum(funnel.values()) == 84, "实得 %d" % sum(funnel.values()))

print()
print("=" * 90)
print("【3】A/B 开关：h5_enabled=False 时恒通过")
p_off = dict(p); p_off["h5_enabled"] = False
chk("关闭后 322 只全部通过", all(m.h5_check(x["symbol"], x, p_off)[0] for x in picks))

print()
print("=" * 90)
print("【4】参数漂移修复：锁定 / 最小样本门槛 / 单参数调整")
m.save_params = lambda _p: None  # 验证期间不污染 params.json
a = m.correct_params(copy.deepcopy(p), 0.0, 1)
chk("锁定态下单样本噪声不改参数", "锁定" in a, "→ %s" % a)
p_unlock = dict(p); p_unlock["param_lock"] = False
q = dict(p_unlock)
b = m.correct_params(q, 0.0, 1)
chk("解锁但样本不足时不改参数", "样本不足" in b and q == p_unlock, "→ %s" % b)
q2 = dict(p_unlock)
c = m.correct_params(q2, 0.0, 25)
changed = [k for k in q2 if q2[k] != p_unlock[k]]
chk("样本充足时仅调整单一参数", changed == ["upper_amp_min"], "变更=%s → %s" % (changed, c))
chk("收紧幅度 = +0.01 且其它参数不动",
    abs(q2["upper_amp_min"] - 0.055) < 1e-9 and q2["vol_ratio_min"] == 1.5
    and q2["low_pos_max_pct"] == 0.15)

print()
print("=" * 90)
print("【5】归档口径：payload 使用筛选时参数快照")
src = open(os.path.join(_HERE, "low_shadow_screener.py"), encoding="utf-8").read()
chk("定义了 params_snapshot", "params_snapshot = dict(params)" in src)
chk("payload.params 引用快照", '"params": params_snapshot' in src)
chk("payload 记录调参后状态", '"params_after_tune": dict(params)' in src)
chk("run_backtest 在 payload 之前", src.index("backtest = run_backtest(params)") < src.index('"params": params_snapshot'))
chk("H5 漏斗写入 payload", '"h5_funnel": h5_funnel' in src)

print()
print("=" * 90)
print(("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
