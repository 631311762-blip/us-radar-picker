#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 端到端验证（双隔离目录，不污染仓库 state / pending / 推送）。

证明三件事：
  (1) 只加字段/文本、不改筛选：筛选/评分/参数相关函数源码逐字一致 + A/B payload 核心键一致
  (2) 全流程字段落盘：payload / 归档 / 全量清单 / 环境快照 / 简报
  (3) 推荐意见可读且结合市场因素：真实环境 + 历史回补后的近 5 日趋势
"""
import contextlib
import importlib.util
import inspect
import io
import json
import os
import shutil
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CUR = os.path.join(REPO, "low_shadow", "low_shadow_screener.py")
# L1 落地前的基线版本固定在提交 abc7938，保证本脚本可重复运行
OLD = "/tmp/l1_baseline_screener.py"
OLD_COMMIT = "abc7938"
ISO_NEW = "/tmp/l1_verify_new"
ISO_OLD = "/tmp/l1_verify_old"
UNIVERSE = os.path.join(REPO, "common", "state", "universe.json")
REAL_ENV = os.path.join(REPO, "low_shadow", "state", "market_env.jsonl")
PARAMS = os.path.join(REPO, "low_shadow", "params.json")
LIXIN = os.path.join(REPO, "lingpei", "lingpei", "state", "领湃_立信排除.json")
OUT = "/tmp/l1_verify_result.json"


def load(path, name):
    if path == OLD:
        # 每次从固定基线提交重新导出，保证 A/B 对照结果确定、无残留污染
        import subprocess
        src = subprocess.check_output(
            ["git", "-C", REPO, "show", "%s:low_shadow/low_shadow_screener.py" % OLD_COMMIT])
        open(OLD, "wb").write(src)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def make_universe():
    rows = json.load(open(UNIVERSE, encoding="utf-8"))
    return [{"symbol": r["code"], "name": r["name"], "pct": r.get("pct", 0)} for r in rows]


def isolate(m, universe, iso):
    os.makedirs(iso, exist_ok=True)
    m.ARCHIVE_DIR = iso
    m.PENDING_FILE = os.path.join(iso, "pending.json")
    m.HISTORY_FILE = os.path.join(iso, "history.jsonl")
    m.MARKET_ENV_FILE = os.path.join(iso, "market_env.jsonl")
    # A/B 公平对比：两模块都指向同一份参数文件（否则 /tmp 版会读到空参数）
    m.PARAMS_FILE = PARAMS
    m.DEMO = False
    m.push = lambda t, c: True                 # 静音推送
    m.get_universe = lambda: universe         # 真实 4606 只（带 pct）
    m.get_kline = lambda sym, lookback: []    # 共享缓存为空 → 空仓分支


def run_one(m, universe, iso):
    shutil.rmtree(iso, ignore_errors=True)
    isolate(m, universe, iso)
    params = m.load_params()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        payload = m.run_review(params)
    return payload, buf.getvalue(), params


def latest(iso, prefix, suffix=".json"):
    fs = sorted(f for f in os.listdir(iso) if f.startswith(prefix) and f.endswith(suffix))
    return os.path.join(iso, fs[-1]) if fs else None


def main():
    new = load(CUR, "ls_new")
    old = load(OLD, "ls_old")
    universe = make_universe()
    res = {"universe_n": len(universe)}

    # ---- (1) 筛选/评分/参数函数源码逐字一致 ----
    # h5_check 的板块策略于 2026-11-17 独立变更（放回来主板、仅排除北交所），
    # 不属于 L1 不变量，故不纳入逐字对比；其余筛选/评分/参数函数仍须与基线一致。
    funcs = ["extract_last_features", "_check_shadow", "is_low_shadow",
             "shadow_diagnose", "pct_rank", "rank_candidates",
             "correct_params", "env_pass", "em_hist", "get_universe"]
    diffs = [fn for fn in funcs
             if inspect.getsource(getattr(old, fn)) != inspect.getsource(getattr(new, fn))]
    res["screener_func_diffs"] = diffs

    # ---- (2) A/B 全流程（各自独立隔离目录） ----
    p_new, brief_new, params = run_one(new, universe, ISO_NEW)
    p_old, brief_old, _ = run_one(old, universe, ISO_OLD)
    res["env_annotation_enabled"] = params.get("env_annotation_enabled")

    core = ["total_scanned", "data_ok_count", "audit_veto_count", "qualified_count",
            "candidate_pool", "position", "funnel", "h5_funnel", "h5_enabled",
            "params", "params_after_tune", "top_samples", "backtest"]
    ab = {k: (p_old.get(k) == p_new.get(k)) for k in core}
    res["ab_core_equal"] = ab
    res["ab_all_equal"] = all(ab.values())

    # 字段落盘校验（只看 L1 版目录）
    res["payload_has_market_env"] = "market_env" in p_new
    res["payload_has_advice"] = "advice" in p_new
    arch = latest(ISO_NEW, "run_")
    full = latest(ISO_NEW, "qualified_")
    res["archive_has_env"] = bool(arch and "market_env" in json.load(open(arch, encoding="utf-8")))
    res["fulllist_has_env"] = bool(full and "market_env" in json.load(open(full, encoding="utf-8")))

    envf = os.path.join(ISO_NEW, "market_env.jsonl")
    hist = [json.loads(x) for x in open(envf, encoding="utf-8") if x.strip()] if os.path.exists(envf) else []
    res["env_snapshot_rows"] = len(hist)
    res["env_snapshot_last"] = hist[-1] if hist else None

    res["brief_has_l1_section"] = "市场环境与推荐意见" in brief_new
    res["brief_has_position_advice"] = "仓位建议" in brief_new
    res["brief_old_has_l1_section"] = "市场环境与推荐意见" in brief_old
    res["brief_l1_lines"] = [l for l in brief_new.splitlines()
                             if l.startswith("- ") and any(
                                 k in l for k in ["大盘环境", "信号拥挤度", "趋势背景",
                                                  "仓位建议", "持有 / 止盈", "依据", "风险提示", "补充"])]
    res["brief_l1_full"] = "\n".join(res["brief_l1_lines"])

    # ---- (3) 真实场景：回填历史 + 11-13 真实漏斗 → 完整推荐意见 ----
    new.MARKET_ENV_FILE = REAL_ENV          # 只读，不写
    env = new.compute_market_env(universe, "2026-11-14")
    advice = new.env_advice(env, n_qualified=10, n_base=13)
    res["demo_env"] = env
    res["demo_advice"] = advice
    # 三档环境各给一份意见，供交付页展示
    res["demo_all_regimes"] = {}
    for rg in ["强", "中性", "弱"]:
        e = dict(env)
        e["regime"] = rg
        e["breadth"] = "普涨" if rg == "强" else ("分歧" if rg == "中性" else "普跌")
        e["eq_chg_pct"] = 1.42 if rg == "强" else (-0.3 if rg == "中性" else -2.074)
        e["adv_ratio_pct"] = 72.0 if rg == "强" else (48.0 if rg == "中性" else 13.87)
        res["demo_all_regimes"][rg] = new.env_advice(e, n_qualified=10, n_base=13)["lines"]
    advice_crowd = new.env_advice(env, n_qualified=16, n_base=20)
    res["demo_advice_crowd"] = advice_crowd

    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 控制台摘要 ----
    print("=" * 70)
    print("L1 端到端验证结果")
    print("=" * 70)
    print("样本池：%d 只（universe.json，带 pct）" % res["universe_n"])
    print("参数开关 env_annotation_enabled =", res["env_annotation_enabled"])
    print("[1] 筛选/评分/参数函数源码差异：", diffs or "无（逐字一致）")
    print("[2] A/B payload 核心键一致：", res["ab_all_equal"])
    for k, v in ab.items():
        print("      %-18s %s" % (k, v))
    print("    payload 含 market_env / advice：", res["payload_has_market_env"], res["payload_has_advice"])
    print("    归档 / 全量清单含环境字段：", res["archive_has_env"], res["fulllist_has_env"])
    print("    环境快照行数：", res["env_snapshot_rows"], "末条：", res["env_snapshot_last"])
    print("    简报含 L1 段 / 仓位建议：", res["brief_has_l1_section"], res["brief_has_position_advice"])
    print("    旧版简报含 L1 段（应为 False）：", res["brief_old_has_l1_section"])
    print("-" * 70)
    print("[3] 真实场景推荐意见（2026-11-14 环境 + 11-13 漏斗 10/13）")
    print("    环境：", json.dumps({k: env.get(k) for k in
          ["regime", "eq_chg_pct", "adv_ratio_pct", "breadth", "eq_r5_pct", "avg_n_base_5d"]},
          ensure_ascii=False))
    for l in advice["lines"]:
        print("   ", l)
    print("-" * 70)
    print("    弱市且信号拥挤示例（n_base=20）仓位建议：", advice_crowd["stance"])
    all_ok = (not diffs and res["ab_all_equal"] and res["payload_has_market_env"]
              and res["payload_has_advice"] and res["archive_has_env"]
              and res["fulllist_has_env"] and res["env_snapshot_rows"] == 1
              and res["brief_has_l1_section"] and res["brief_has_position_advice"]
              and not res["brief_old_has_l1_section"])
    print("=" * 70)
    print("总判定：", "ALL PASS" if all_ok else "存在未通过项")
    res["all_pass"] = all_ok
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("结果已写：", OUT)


if __name__ == "__main__":
    main()
