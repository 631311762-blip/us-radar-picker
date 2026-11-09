#!/bin/bash
# 幂等自启 stock-v3 自动调度器
# - 若已在运行则跳过，不重复拉起
# - 若未运行（含沙箱休眠后进程丢失）则后台拉起，并自动补跑当天（若未跑且已过计划时刻）
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDF="$DIR/.auto_runner.pid"

if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  echo "auto_runner 已在运行 (pid $(cat "$PIDF"))，无需重启"
  exit 0
fi

cd "$DIR" || exit 1
# 清理失效 pid 文件
rm -f "$PIDF"

nohup python3 auto_runner.py > auto_runner.out 2>&1 &
echo $! > "$PIDF"
echo "已启动 auto_runner (pid $!)"
