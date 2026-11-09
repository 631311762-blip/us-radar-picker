#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock-v3 每日自动运行调度器（沙箱无 cron/systemd，故常驻自带 sleep 到点执行）

职责：
  1. 每个交易日（周一~周五）收盘后 RUN_HOUR:RUN_MIN 自动运行 daily_v3.py 完整闭环；
  2. 运行后自动 git commit 当日成果，并同步到 origin/main（应对远端被 CI 持续推进）；
  3. 去重：当天已成功运行则跳过，绝不重复跑；
  4. 沙箱唤醒补跑：进程被拉起时若当天未跑且已过计划时刻，立即补跑一次；
  5. 非交易日 / 行情源未更新由 daily_v3.py 自身安全 ABORT，本调度器不干预、不补跑。

用法：
  python3 auto_runner.py            # 前台运行（调试）
  bash start_auto.sh               # 幂等后台自启（推荐）

注意：推送凭证已写入 .git/config 的 remote URL；commit 时设置 SKIP_AUTOPUSH=1
      以由本脚本统一控制 fetch→merge→push，避免与 post-commit 钩子双重推送。
"""
import os
import sys
import time
import subprocess
import datetime

STOCK_DIR = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(STOCK_DIR, ".auto_runner.pid")
STATE = os.path.join(STOCK_DIR, ".last_run_date")
RUN_HOUR = 17
RUN_MIN = 30


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def already_ran_today():
    try:
        return open(STATE).read().strip() == datetime.date.today().isoformat()
    except Exception:
        return False


def mark_ran():
    try:
        open(STATE, "w").write(datetime.date.today().isoformat())
    except Exception as e:
        log(f"写状态文件失败: {e}")


def run_daily():
    """返回 True 表示真正跑了（工作日）；周末返回 False。"""
    wd = datetime.date.today().weekday()
    if wd >= 5:
        log("今日为周末，跳过（行情由 daily_v3.py 自行判断交易日）")
        return False
    log_file = os.path.join(STOCK_DIR, f"auto_run_{datetime.date.today():%Y%m%d}.log")
    log(f"启动 daily_v3.py -> {log_file}")
    with open(log_file, "w") as f:
        p = subprocess.run(
            [sys.executable, "daily_v3.py"],
            cwd=STOCK_DIR,
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    log(f"daily_v3.py 退出码={p.returncode}")
    return True


def git_sync():
    """自动提交当日变更并同步远端（应对远端 CI 持续推进）。"""
    env = dict(os.environ)
    env["SKIP_AUTOPUSH"] = "1"  # 脚本统一控制推送，跳过 post-commit 钩子的二次推送
    subprocess.run(["git", "add", "-A"], cwd=STOCK_DIR, env=env)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=STOCK_DIR, env=env)
    if r.returncode == 0:
        log("git: 无变更，跳过 commit")
        return
    msg = f"daily: 选股结果自动同步 {datetime.date.today():%Y-%m-%d}"
    subprocess.run(["git", "commit", "-m", msg], cwd=STOCK_DIR, env=env)
    log("git: 已提交，开始同步远端 (应对 CI 持续推进) ...")
    for attempt in range(6):
        subprocess.run(["git", "fetch", "origin"], cwd=STOCK_DIR)
        cur = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=STOCK_DIR,
            capture_output=True, text=True,
        ).stdout.strip()
        rem = subprocess.run(
            ["git", "rev-parse", "origin/main"], cwd=STOCK_DIR,
            capture_output=True, text=True,
        ).stdout.strip()
        if cur == rem:
            log("git: 已与远端一致")
            return
        subprocess.run(
            ["git", "merge", "origin/main", "--no-edit",
             "-m", "merge: 同步上游最新改动"], cwd=STOCK_DIR,
        )
        pr = subprocess.run(["git", "push", "origin", "main"], cwd=STOCK_DIR, env=env)
        if pr.returncode == 0:
            log("git: push 成功")
            return
        log(f"git: push 第 {attempt + 1} 次失败，重试 ...")
        time.sleep(6)
    log("git: 多次推送失败，请手动处理（提交已保存在本地）")


def do_run():
    if already_ran_today():
        log("今日已运行，跳过")
        return
    ran = run_daily()
    if ran:
        mark_ran()
        git_sync()


def next_run_time():
    now = datetime.datetime.now()
    t = now.replace(hour=RUN_HOUR, minute=RUN_MIN, second=0, microsecond=0)
    if t <= now:
        t += datetime.timedelta(days=1)
    while t.weekday() >= 5:  # 跳过周末，落到下周一
        t += datetime.timedelta(days=1)
    return t


def write_pid():
    try:
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def main():
    write_pid()
    log(f"auto_runner 启动 (pid={os.getpid()})  计划每日 {RUN_HOUR:02d}:{RUN_MIN:02d} 运行")
    # 唤醒补跑：今天还没跑且已过计划时刻
    if datetime.datetime.now().hour >= RUN_HOUR and not already_ran_today():
        log("检测到今天尚未运行且已过计划时刻，立即补跑")
        do_run()
    try:
        while True:
            t = next_run_time()
            secs = (t - datetime.datetime.now()).total_seconds()
            log(f"下次计划运行: {t:%Y-%m-%d %H:%M:%S}，睡眠 {secs:.0f}s")
            time.sleep(secs)
            do_run()
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        try:
            if os.path.exists(PIDFILE) and open(PIDFILE).read().strip() == str(os.getpid()):
                os.remove(PIDFILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()
