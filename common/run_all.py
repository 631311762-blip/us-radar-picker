#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py —— 模型编排器（合并流水线的第二步）
========================================================================
前置：prefilter.py 已完成四个共同条件的基础工作（交易日判定 / 样本池
      [含 ST+立信剔除] / 全市场 K 线缓存 / 立信排除集）。

本编排器：
  1) 读 common/registry.json（新增模型只要在此登记，无需改 workflow）。
  2) 逐个执行模型命令，cwd = 仓库根；注入统一的 STATE_DIR 等环境变量。
  3) 单个模型失败不阻断其余模型（异常隔离），最后汇总。
  4) 对每个已登记模型跑一次 ctx.review() 回溯结算 + ctx.params() 落盘，
     使「回溯推荐 / 修正模型」对任何模型都自动生效（条件④）。

用法：
  python3 common/run_all.py                 # 跑注册表全部 enabled 模型
  python3 common/run_all.py --only yidian  # 只跑指定模型（验证/补跑）
  python3 common/run_all.py --list         # 列出注册模型
"""
import os
import sys
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
REGISTRY = os.path.join(BASE_DIR, "registry.json")

sys.path.insert(0, BASE_DIR)
import common_lib as cm
import kit


def load_registry():
    with open(REGISTRY, "r", encoding="utf-8") as fh:
        return json.load(fh)


# 单模型超时：模型已通过共同前置层离线读缓存，正常几秒~几十秒完成。
# 历史上 3600s 超时会导致偶发的进程启动阻塞卡满 1 小时，把一个模型的问题
# 放大成整条流水线超时（2026-11-19 善水卡 3600s + 低位 3581s 拖垮全局）。
# 超时即杀掉并自动重试一次（偶发阻塞重试即恢复）；并行后单模型超时不再
# 累加到全局，设为 300s 足以覆盖任何正常离线扫描 + 一次重试的最坏情形。
MODEL_TIMEOUT = int(os.environ.get("MODEL_TIMEOUT", "300"))
MODEL_RETRIES = int(os.environ.get("MODEL_RETRIES", "1"))

# 并行度：9 个模型彼此独立（各自读同一份共享 K 线缓存、写各自 state 子目录），
# 无共享写冲突，可安全并行。默认 9 路全开；受 runner CPU/内存限制可用环境变量
# 调低（如 MODEL_WORKERS=4）。设为 1 即退回串行，便于排障对比。
MODEL_WORKERS = max(1, int(os.environ.get("MODEL_WORKERS", "9")))


def _run_once(cmd, env, timeout):
    """执行一次模型命令，返回 (returncode, 是否超时)。"""
    try:
        r = subprocess.run(cmd, shell=True, cwd=REPO_ROOT, env=env, timeout=timeout)
        return r.returncode, False
    except subprocess.TimeoutExpired:
        return 124, True
    except Exception as e:
        print("[run_all] 模型启动异常: %s" % e, flush=True)
        return 1, False


def run_model(m):
    name, cmd = m["name"], m["cmd"]
    label = m.get("label", name)
    print("\n" + "=" * 64)
    print("[run_all] 启动模型：%s (%s)" % (label, name))
    print("[run_all] cmd: %s" % cmd)
    print("=" * 64, flush=True)
    env = os.environ.copy()
    env["STATE_DIR"] = cm.STATE_DIR
    env["COMMON_STATE_DIR"] = cm.STATE_DIR
    env["COMMON_UNIVERSE"] = cm.UNIVERSE_JSON
    env["COMMON_KLINE_DIR"] = cm.KLINE_DIR
    env["COMMON_LIXIN"] = cm.LIXIN_JSON
    env["TZ"] = "Asia/Shanghai"
    t0 = datetime.now()
    code = 1
    for attempt in range(MODEL_RETRIES + 1):
        code, timed_out = _run_once(cmd, env, MODEL_TIMEOUT)
        if code == 0 or not timed_out:
            break
        # 仅超时才重试：启动偶发阻塞重试通常秒过；真失败不重试避免浪费时间。
        if attempt < MODEL_RETRIES:
            print("[run_all] 模型 %s 超时(%ss)，第 %d 次自动重试…"
                  % (name, MODEL_TIMEOUT, attempt + 1), flush=True)
    dt = (datetime.now() - t0).total_seconds()
    print("[run_all] 模型 %s 结束，退出码=%s，耗时 %.1fs" % (name, code, dt), flush=True)
    return {"name": name, "label": label, "exit": code, "seconds": round(dt, 1)}


def settle_all(names):
    """对每个模型跑回溯结算 + 参数修正落盘（条件④ 强制生效）。"""
    print("\n[run_all] === 统一回溯结算 / 参数修正 ===", flush=True)
    out = []
    for name in names:
        try:
            ctx = kit.ModelContext(name)
            rv = ctx.review(save=True)
            out.append({
                "model": name,
                "batches": rv["batches"], "samples": rv["samples"],
                "win_rate": rv["win_rate"],
            })
            print("[run_all] %-12s 回溯批次=%d 样本=%d 综合胜率=%s"
                  % (name, rv["batches"], rv["samples"], rv["win_rate"]), flush=True)
        except Exception as e:
            print("[run_all] %s 回溯失败: %s" % (name, e), flush=True)
    return out


def main():
    args = sys.argv[1:]
    reg = load_registry()
    models = [m for m in reg["models"] if m.get("enabled", True)]

    if "--list" in args:
        for m in models:
            print("%-12s %-16s %s" % (m["name"], m.get("label", ""), m["cmd"]))
        return 0

    if "--only" in args:
        i = args.index("--only")
        want = set(args[i + 1].split(",")) if i + 1 < len(args) else set()
        models = [m for m in models if m["name"] in want]

    print("[run_all] 注册模型 %d 个，本次执行 %d 个" % (len(reg["models"]), len(models)))

    # 并行执行：取最慢模型耗时，而非串行累加。单模型异常隔离（run_model 内部
    # 已兜底返回 exit=1），线程池 here 再包一层保险，确保任何模型抛错都不阻断
    # 其余结果与 state 回写。结果按注册表顺序排列，保证汇总稳定可复现。
    results_by_name = {}
    workers = min(MODEL_WORKERS, len(models)) if models else 1
    print("[run_all] 并行度 = %d（单模型超时 %ss，重试 %d 次）"
          % (workers, MODEL_TIMEOUT, MODEL_RETRIES), flush=True)
    if workers <= 1:
        for m in models:
            r = run_model(m)
            results_by_name[r["name"]] = r
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_model, m): m["name"] for m in models}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    print("[run_all] 模型 %s 线程级异常（已隔离）: %s" % (name, e), flush=True)
                    r = {"name": name, "label": name, "exit": 1, "seconds": 0.0}
                results_by_name[name] = r
    results = [results_by_name[m["name"]] for m in models]

    # 条件④：回溯 + 参数修正，对所有已登记模型生效（即使模型自身没实现）。
    # settle=false 的模型（如主升浪，自带 feedback 闭环）跳过公共回溯，避免无谓报错。
    settle_names = [m["name"] for m in reg["models"]
                    if m.get("enabled", True) and m.get("settle", True)]
    settle = settle_all(settle_names)

    print("\n" + "=" * 64)
    print("[run_all] 汇总")
    for r in results:
        print("  %-12s exit=%-4s %.1fs" % (r["name"], r["exit"], r["seconds"]))
    ok = sum(1 for r in results if r["exit"] == 0)
    print("  成功 %d / %d" % (ok, len(results)))
    print("  回溯结算模型数：%d" % len(settle))
    print("=" * 64)

    # 汇总写盘供 workflow 回写 / 排障
    summary = {
        "generated_at": datetime.now(cm.BJT).isoformat(timespec="seconds"),
        "results": results, "settlements": settle,
    }
    with open(os.path.join(cm.STATE_DIR, "run_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return 0  # 单模型失败不使整体失败，避免阻断 state 回写


if __name__ == "__main__":
    sys.exit(main())
