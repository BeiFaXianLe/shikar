#!/usr/bin/env bash
# 设置 GitHub token，供 push_backup.py 使用（异地备份推送）
#
# 用法（在服务器上执行）：
#   bash deploy/set-gh-token.sh <your_token>
#
# 说明：
# - token 写到 /opt/shikar/.secrets/gh-token，权限 0600
# - .secrets/ 已在 .gitignore 中，不会进仓库
# - refresh.sh 会从该文件读取并 export GH_TOKEN
# - 建议 token 只授予 shikar-data-backup 一个仓库的 Contents: Read and write 权限

set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
  echo "用法: bash deploy/set-gh-token.sh <your_github_token>" >&2
  exit 1
fi

TOKEN="$1"
SECRETS_DIR=".secrets"
TOKEN_FILE="$SECRETS_DIR/gh-token"

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

printf '%s' "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

echo "已写入 $TOKEN_FILE (0600)"

# 立即验证 token 能否访问私有备份仓库
echo "验证 token…"
if curl -sf -H "Authorization: Bearer $TOKEN" \
     "https://api.github.com/repos/BeiFaXianLe/shikar-data-backup" \
     -o /dev/null; then
  echo "✓ token 可访问 BeiFaXianLe/shikar-data-backup"
else
  echo "✗ token 无法访问该仓库，请检查权限（需要 Contents: Read and write）" >&2
  exit 1
fi
