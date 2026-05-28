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

git fetch --quiet --tags origin
PREV_TAG=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | awk -v current="v${VER}" '$0 != current { print; exit }')
PREV_TAG=${PREV_TAG:-none}

if git rev-parse -q --verify "refs/tags/v${VER}" >/dev/null; then
  echo "tag v${VER} 已存在，请先更新 VERSION"
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/v${VER}" >/dev/null 2>&1; then
  echo "远端 tag v${VER} 已存在，请先更新 VERSION"
  exit 1
fi

echo "=== 版本: ${PREV_TAG} -> v${VER} ==="
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
  --build-arg "DINGDONG_VERSION=${VER}" \
  -t hexsean/dingdong:latest \
  -t hexsean/dingdong:${VER} \
  --push .

remote_digest() {
  local image="$1"
  local digest=""
  for _ in {1..12}; do
    digest=$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$image" 2>/dev/null || true)
    if [[ "$digest" == sha256:* ]]; then
      echo "$digest"
      return
    fi
    echo "等待镜像同步: ${image}" >&2
    sleep 5
  done
  echo "无法读取镜像: ${image}" >&2
  exit 1
}

verify_latest_tag() {
  local version_digest="$1"
  local latest_digest=""
  for _ in {1..12}; do
    latest_digest=$(remote_digest "hexsean/dingdong:latest")
    if [[ "$latest_digest" == "$version_digest" ]]; then
      return
    fi
    echo "等待 latest 标签同步" >&2
    sleep 5
  done
  echo "latest 标签不匹配，期望 ${version_digest}，实际 ${latest_digest:-unknown}"
  exit 1
}

watchtower_digest() {
  local tag="$1"
  local token=""
  local headers=""
  local digest=""

  token=$(curl -fsSL "https://auth.docker.io/token?service=registry.docker.io&scope=repository%3Ahexsean%2Fdingdong%3Apull" 2>/dev/null \
    | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  [[ -n "$token" ]] || return 1

  headers=$(curl -fsSI \
    -H "Authorization: Bearer ${token}" \
    -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
    -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
    -H "Accept: application/vnd.docker.distribution.manifest.v1+json" \
    -H "Accept: application/vnd.oci.image.index.v1+json" \
    "https://index.docker.io/v2/hexsean/dingdong/manifests/${tag}" 2>/dev/null || true)

  digest=$(printf "%s\n" "$headers" | awk 'tolower($1) == "docker-content-digest:" { gsub("\r", "", $2); print $2; exit }')
  [[ "$digest" == sha256:* ]] || return 1
  echo "$digest"
}

wait_update_channel() {
  local expected_digest="$1"
  local digest=""
  local stable=0

  for _ in {1..30}; do
    digest=$(watchtower_digest "latest" || true)
    if [[ "$digest" == "$expected_digest" ]]; then
      stable=$((stable + 1))
      if [[ "$stable" -ge 2 ]]; then
        return
      fi
    else
      stable=0
    fi
    echo "等待更新通道同步" >&2
    sleep 10
  done

  echo "更新通道未同步，期望 ${expected_digest}，实际 ${digest:-unknown}" >&2
  exit 1
}

# 确认远端 latest 已指向本次版本，再发布 GitHub 版本。
VERSION_DIGEST=$(remote_digest "hexsean/dingdong:${VER}")
verify_latest_tag "$VERSION_DIGEST"
wait_update_channel "$VERSION_DIGEST"

# 只有镜像推送成功后，才发布 GitHub 版本，避免用户收到更新提醒时镜像还没准备好。
git tag -a "v${VER}" -m "$MSG"
if ! git push --atomic origin "HEAD:main" "refs/tags/v${VER}"; then
  git tag -d "v${VER}" >/dev/null 2>&1 || true
  echo "GitHub 推送失败；镜像可能已经推送成功。修复后可重新运行 ./deploy.sh。"
  exit 1
fi

if docker pull "hexsean/dingdong:${VER}" >/dev/null; then
  docker tag "hexsean/dingdong:${VER}" hexsean/dingdong:latest
fi

echo "✓ ${PREV_TAG} -> v${VER} done"
