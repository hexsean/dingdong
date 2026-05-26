#!/bin/bash
set -euo pipefail

VER=$(cat VERSION | tr -d '[:space:]')
MSG="${1:-v${VER}}"
BRANCH=$(git branch --show-current)

if [[ -z "$BRANCH" ]]; then
  echo "当前不在分支上，无法发布"
  exit 1
fi

if [[ "$BRANCH" != "main" ]]; then
  echo "发布必须在 main 分支执行，当前分支: ${BRANCH}"
  exit 1
fi

if [[ ! "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION 必须是 MAJOR.MINOR.PATCH，当前: ${VER}"
  exit 1
fi

if git rev-parse -q --verify "refs/tags/v${VER}" >/dev/null; then
  echo "tag v${VER} 已存在，请先更新 VERSION"
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/v${VER}" >/dev/null 2>&1; then
  echo "远端 tag v${VER} 已存在，请先更新 VERSION"
  exit 1
fi

echo "=== 版本: v${VER} ==="
echo "=== 变更文件 ==="
git status -s
echo ""

read -p "确认提交并推送? [y/N] " confirm
[[ "$confirm" =~ ^[yY]$ ]] || { echo "已取消"; exit 0; }

git add -A
if git diff --cached --quiet; then
  echo "没有新的文件变更，将发布当前 HEAD"
else
  git commit -m "$MSG"
fi

docker buildx build --platform linux/amd64,linux/arm64 \
  --pull \
  -t hexsean/dingdong:latest \
  -t hexsean/dingdong:${VER} \
  --push .

# 只有镜像推送成功后，才发布 GitHub 版本，避免用户收到更新提醒时镜像还没准备好。
git tag -a "v${VER}" -m "$MSG"
if ! git push --atomic origin "HEAD:main" "refs/tags/v${VER}"; then
  git tag -d "v${VER}" >/dev/null 2>&1 || true
  echo "GitHub 推送失败；镜像可能已经推送成功。修复后可重新运行 ./deploy.sh。"
  exit 1
fi

# buildx 跨平台构建不更新本地 tag，按版本号拉取确保本地同步
docker pull hexsean/dingdong:${VER}
docker tag hexsean/dingdong:${VER} hexsean/dingdong:latest

echo "✓ v${VER} done"
