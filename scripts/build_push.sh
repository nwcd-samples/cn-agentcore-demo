#!/usr/bin/env bash
#
# 构建 arm64 镜像并推到 ECR。
#
#   ./scripts/build_push.sh            # 构建 + 推送,打 latest 和 git sha 两个标签
#   ./scripts/build_push.sh --local    # 只本地构建,不推
#
# AgentCore Runtime 只接受 linux/arm64。在 Apple Silicon 上是原生构建;
# 在 x86 机器上 buildx 会走 QEMU 模拟,慢但能用。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a && source .env && set +a
fi

PROJECT="${PROJECT:-agentcore-cn}"
AWS_REGION="${AWS_REGION:-cn-northwest-1}"
FOUNDATION_STACK="${PROJECT}-foundation"
PLATFORM="linux/arm64"

AWS=(aws --region "$AWS_REGION")
[[ -n "${AWS_PROFILE:-}" ]] && AWS+=(--profile "$AWS_PROFILE")

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "缺少 docker"
docker buildx version >/dev/null 2>&1 || die "缺少 docker buildx"

# 标签用 git sha,方便把运行中的镜像追溯回代码
if git rev-parse --git-dir >/dev/null 2>&1; then
  TAG="$(git rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    TAG="${TAG}-dirty"
    warn "工作区有未提交改动,镜像标签加了 -dirty 后缀"
  fi
else
  TAG="$(date +%Y%m%d%H%M%S)"
fi

LOCAL_ONLY=0
[[ "${1:-}" == "--local" ]] && LOCAL_ONLY=1

host_arch="$(uname -m)"
if [[ "$host_arch" != "arm64" && "$host_arch" != "aarch64" ]]; then
  warn "本机是 ${host_arch},arm64 构建会走 QEMU 模拟,可能很慢"
fi

if [[ "$LOCAL_ONLY" == "1" ]]; then
  log "本地构建 ${PLATFORM} 镜像(不推送)"
  docker buildx build --platform "$PLATFORM" -t "${PROJECT}/agent:${TAG}" --load .
  log "完成:${PROJECT}/agent:${TAG}"
  exit 0
fi

# ---- 推 ECR ----

identity="$("${AWS[@]}" sts get-caller-identity --output json)" \
  || die "凭证不可用(AWS_PROFILE=${AWS_PROFILE:-<未设置>})"
account="$(printf '%s' "$identity" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])')"
partition="$(printf '%s' "$identity" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"].split(":")[1])')"
[[ "$partition" == "aws-cn" ]] || die "当前凭证在分区 ${partition},不是 aws-cn"

repo_uri="$("${AWS[@]}" cloudformation describe-stacks \
  --stack-name "$FOUNDATION_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AgentRepositoryUri'].OutputValue" \
  --output text 2>/dev/null)"
[[ -n "$repo_uri" && "$repo_uri" != "None" ]] \
  || die "取不到 ECR 仓库地址,先跑 ./scripts/deploy.sh"

# 中国区 ECR 的域名后缀是 .amazonaws.com.cn
registry="${account}.dkr.ecr.${AWS_REGION}.amazonaws.com.cn"
log "登录 ECR ${registry}"
"${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$registry"

log "构建并推送 ${PLATFORM} 镜像,标签 ${TAG} 和 latest"
# --provenance=false:AgentCore 拉镜像时不需要 attestation manifest,
# 带上反而会让某些镜像解析路径变复杂
docker buildx build \
  --platform "$PLATFORM" \
  --provenance=false \
  -t "${repo_uri}:${TAG}" \
  -t "${repo_uri}:latest" \
  --push .

log "已推送:"
printf '  %s:%s\n  %s:latest\n\n' "$repo_uri" "$TAG" "$repo_uri"
log "下一步建 Runtime:"
printf '  python scripts/create_runtime.py --image %s:%s\n\n' "$repo_uri" "$TAG"
