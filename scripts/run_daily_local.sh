#!/usr/bin/env bash
#
# run_daily_local.sh — 领湃模型「收盘后完整流程」本地/沙箱运行器（含自动同步）
# =====================================================
# 用法:
#   bash scripts/run_daily_local.sh            # 正常运行
#   bash scripts/run_daily_local.sh --no-gate  # 透传参数给 领湃模型_每日.py
#
# 流程:
#   1. git pull 拉取仓库最新代码/状态（后续他处改动自动生效）
#   2. 在 lingpei/ 下依次执行: _lixin_update.py -> 领湃模型_每日.py -> 领湃跟踪修正.py
#      （脚本内部非交易日/无信号/无到期样本会自动退出、不推送）
#   3. git add + commit 回写状态（若已装 post-commit 钩子则自动推送上云）
#
# 说明:
#   - STATE_DIR 默认与 GitHub Actions 保持一致（lingpei/state，相对 lingpei/ 即 lingpei/lingpei/state）
#   - PUSHPLUS_TOKEN 通过环境变量传入；GitHub Actions 由同名 Secret 提供
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 沙箱/代理环境 HTTPS 证书修复（存在才设置，不影响其他机器）
if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
  export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
fi

echo "[1/3] git pull 同步最新代码与状态..."
git pull --rebase --autostash origin main 2>/dev/null \
  || echo "[warn] git pull 失败，使用本地代码继续"

echo "[2/3] 运行领湃三步流程..."
cd lingpei
mkdir -p state
export STATE_DIR="${STATE_DIR:-lingpei/state}"
export PUSHPLUS_TOKEN="${PUSHPLUS_TOKEN:-}"

set -o pipefail
python3 _lixin_update.py \
  && python3 领湃模型_每日.py "$@" \
  && python3 领湃跟踪修正.py
rc=$?

echo "[3/3] 回写运行状态到仓库..."
cd "$ROOT"
git add lingpei scripts
git commit -m "lingpei state: $(date +%F %H:%M)" || echo "[info] 无状态变更，跳过提交"

if [ $rc -ne 0 ]; then
  echo "[error] 三步流程存在失败步骤（exit=$rc），请按上方错误信息排查"
fi
exit $rc
