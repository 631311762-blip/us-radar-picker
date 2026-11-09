#!/usr/bin/env bash
#
# install_autosync.sh — 启用「提交即推送」自动同步
# =====================================================
# 用法:
#   GH_TOKEN=ghp_xxx bash scripts/install_autosync.sh
#   或
#   bash scripts/install_autosync.sh ghp_xxx
#
# 作用:
#   1. 将 origin 的 URL 写入「带 token」的地址（避免每次提交都输入凭据）
#   2. 安装 .git/hooks/post-commit 钩子：每次 git commit 后自动推送到当前分支
#
# 说明:
#   - Git 钩子不被仓库跟踪，因此每个克隆副本需各自运行一次本脚本
#   - 推送失败不会中断提交，仅打印提示，可稍后手动 git push
#   - 仓库已通过 .github/workflows/low_shadow.yml 在收盘后定时运行模型并回写状态，
#     本脚本负责「本地代码改动」也能自动同步上云
set -euo pipefail

REPO="631311762-blip/us-radar-picker"
TOKEN="${GH_TOKEN:-${1:-}}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -n "$TOKEN" ]; then
  git remote set-url origin "https://${TOKEN}@github.com/${REPO}.git"
  echo "[ok] 已配置 origin（含 token）"
else
  echo "[warn] 未提供 GH_TOKEN，跳过 remote 配置（请确保 origin 已可推送）"
fi

HOOK=".git/hooks/post-commit"
cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
# 自动同步：提交后推送当前分支到 origin
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -n "$branch" ]; then
  git push --quiet origin "$branch" \
    || echo "[post-commit] 自动推送失败，请手动: git push origin $branch"
fi
HOOK_EOF
chmod +x "$HOOK"
echo "[ok] 已安装 post-commit 钩子 -> $HOOK"
echo "完成。此后在本仓库执行 git commit 即自动推送当前分支到 GitHub。"
