#!/bin/bash
set -e

MSG="${1:-update}"

echo "=== 变更文件 ==="
git status -s
echo ""

read -p "确认提交并推送? [y/N] " confirm
[[ "$confirm" =~ ^[yY]$ ]] || { echo "已取消"; exit 0; }

git add -A
git commit -m "$MSG"
git push
docker build -t hexsean/dingdong:latest .
docker push hexsean/dingdong:latest

echo "✓ done"
