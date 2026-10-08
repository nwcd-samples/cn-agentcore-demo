#!/usr/bin/env bash
#
# P0 部署:基础设施 + 自建 OIDC IdP。
#
#   ./scripts/deploy.sh            # 建栈 / 更新栈 + 推代码 + 回填 issuer + seed
#   ./scripts/deploy.sh --code     # 只重推两个 Lambda 的代码
#   ./scripts/deploy.sh --verify   # 只跑验证
#   ./scripts/deploy.sh --quick    # 建/轮换 Quick S2S 客户端并更新 Gateway allowlist
#
# 幂等,可重复执行。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- 配置 ----------------------------------------------------------------

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a && source .env && set +a
fi

PROJECT="${PROJECT:-agentcore-cn}"
AWS_REGION="${AWS_REGION:-cn-northwest-1}"
FOUNDATION_STACK="${PROJECT}-foundation"
IDP_STACK="${PROJECT}-auth-idp"
TOOLS_STACK="${PROJECT}-business-tools"
LOGISTICS_STACK="${PROJECT}-logistics-web"

AWS=(aws --region "$AWS_REGION")
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS+=(--profile "$AWS_PROFILE")
fi

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

stack_output() {
  local stack="$1" key="$2"
  "${AWS[@]}" cloudformation describe-stacks \
    --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text 2>/dev/null
}

# ---- 前置检查 ------------------------------------------------------------

preflight() {
  command -v aws >/dev/null || die "缺少 aws cli"
  command -v python3 >/dev/null || die "缺少 python3"

  local identity
  if ! identity="$("${AWS[@]}" sts get-caller-identity --output json 2>&1)"; then
    die "凭证不可用,先确认 AWS_PROFILE=${AWS_PROFILE:-<未设置>} 是否有效:
$identity"
  fi
  local account partition
  account="$(printf '%s' "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')"
  partition="$(printf '%s' "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"].split(":")[1])')"
  [[ "$partition" == "aws-cn" ]] || die "当前凭证在分区 ${partition},不是 aws-cn。本项目只针对中国区。"
  log "账号 ${account} / 区域 ${AWS_REGION} / 分区 ${partition}"
}

# ---- 建栈 ----------------------------------------------------------------

deploy_stacks() {
  log "校验模板"
  if [[ -x .venv/bin/cfn-lint ]]; then
    .venv/bin/cfn-lint infra/*.yaml --region "$AWS_REGION"
  else
    warn "未装 cfn-lint,跳过离线校验"
  fi

  log "部署 ${FOUNDATION_STACK}"
  "${AWS[@]}" cloudformation deploy \
    --template-file infra/00-foundation.yaml \
    --stack-name "$FOUNDATION_STACK" \
    --parameter-overrides "Project=${PROJECT}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset

  log "部署 ${IDP_STACK}"
  "${AWS[@]}" cloudformation deploy \
    --template-file infra/10-auth-idp.yaml \
    --stack-name "$IDP_STACK" \
    --parameter-overrides "Project=${PROJECT}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset

  log "部署 ${TOOLS_STACK}"
  "${AWS[@]}" cloudformation deploy \
    --template-file infra/20-business-tools.yaml \
    --stack-name "$TOOLS_STACK" \
    --parameter-overrides "Project=${PROJECT}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset

  log "部署 ${LOGISTICS_STACK}(Browser 的演示靶子,端点刻意公开)"
  "${AWS[@]}" cloudformation deploy \
    --template-file infra/30-logistics-web.yaml \
    --stack-name "$LOGISTICS_STACK" \
    --parameter-overrides "Project=${PROJECT}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset
}

# ---- 推 Lambda 代码 ------------------------------------------------------

# 两个 Lambda 都是零第三方依赖,zip 里只有一个 *_handler.py,不需要 Layer。
# 文件名刻意不同名(idp_handler / tools_handler),避免 import 时互相覆盖。
push_lambda_code() {
  local stack="$1" output_key="$2" source_dir="$3" fn_name zip_dir
  fn_name="$(stack_output "$stack" "$output_key")"
  [[ -n "$fn_name" && "$fn_name" != "None" ]] || die "取不到函数名(${stack}/${output_key})"

  zip_dir="$(mktemp -d)"
  log "打包并更新 ${fn_name}"
  # 每个 Lambda 目录下只有一个 *_handler.py,整目录打包
  (cd "$source_dir" && zip -q -r "${zip_dir}/fn.zip" . -i '*.py')

  "${AWS[@]}" lambda update-function-code \
    --function-name "$fn_name" \
    --zip-file "fileb://${zip_dir}/fn.zip" \
    --publish \
    --output text --query 'Version' >/dev/null
  "${AWS[@]}" lambda wait function-updated --function-name "$fn_name"
  rm -rf "$zip_dir"
}

push_all_code() {
  push_lambda_code "$IDP_STACK" IdpFunctionName src/lambdas/idp
  push_lambda_code "$TOOLS_STACK" ToolsFunctionName src/lambdas/tools
  push_lambda_code "$LOGISTICS_STACK" LogisticsFunctionName src/lambdas/logistics
}

# ---- 回填 issuer --------------------------------------------------------

backfill_issuer() {
  local fn_name issuer current env_json
  fn_name="$(stack_output "$IDP_STACK" IdpFunctionName)"
  issuer="$(stack_output "$IDP_STACK" IssuerUrl)"
  [[ -n "$issuer" && "$issuer" != "None" ]] || die "取不到 IssuerUrl"

  current="$("${AWS[@]}" lambda get-function-configuration \
    --function-name "$fn_name" \
    --query 'Environment.Variables.ISSUER' --output text)"

  if [[ "$current" == "$issuer" ]]; then
    log "ISSUER 已是 ${issuer},跳过"
    return
  fi

  # issuer 必须与 discovery 文档和 token 的 iss 完全一致,
  # 否则 AgentCore 的 CUSTOM_JWT 校验会静默失败(一律 403)。
  #
  # 用 JSON 而不是 Variables={k=v,...} 的 shorthand:后者遇到含逗号或
  # 等号的值会被拆错,ARN 和 URL 都有这个风险。
  log "回填 ISSUER=${issuer}"
  env_json="$(
    "${AWS[@]}" lambda get-function-configuration \
      --function-name "$fn_name" \
      --query 'Environment.Variables' --output json |
    python3 -c '
import json, sys
env = json.load(sys.stdin) or {}
env["ISSUER"] = sys.argv[1]
json.dump({"Variables": env}, sys.stdout)
' "$issuer"
  )"

  "${AWS[@]}" lambda update-function-configuration \
    --function-name "$fn_name" \
    --environment "$env_json" \
    --output text --query 'LastModified' >/dev/null
  "${AWS[@]}" lambda wait function-updated --function-name "$fn_name"

  # 环境变量一改容器就重启,公钥缓存会重建,这里不需要额外处理
}

# ---- seed ---------------------------------------------------------------

seed() {
  local py=python3
  [[ -x .venv/bin/python ]] && py=.venv/bin/python

  log "写入演示用户与 OAuth 客户端"
  AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/seed_auth.py

  log "写入演示订单与工单"
  AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/seed_business.py
}

# ---- Amazon Quick 团队级 Remote MCP -------------------------------------

quick() {
  local py=python3
  [[ -x .venv/bin/python ]] && py=.venv/bin/python

  log "创建/轮换 Amazon Quick 独立 Service-to-Service 客户端"
  AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/seed_auth.py --quick-only

  log "把 Quick client_id 加入 Gateway CUSTOM_JWT allowedClients"
  AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/create_gateway.py

  local token_endpoint gateway_url quick_id
  token_endpoint="$(stack_output "$IDP_STACK" TokenEndpoint)"
  gateway_url="${GATEWAY_URL:-}"
  quick_id="${QUICK_CLIENT_ID:-${PROJECT}-quick}"
  cat <<EOF

$(log "在 Amazon Quick → Create for your team → MCP 中填写:")
  MCP server endpoint: ${gateway_url:-<使用 create_gateway.py 输出的 GATEWAY_URL>}
  Authentication:      Service-to-Service
  Client ID:           ${quick_id}
  Client Secret:       使用 .env 中的 QUICK_CLIENT_SECRET（首次生成值见上方）
  Token URL:           ${token_endpoint}
  Scope:               gateway:invoke tools:read tools:write
EOF
}

# ---- Gateway ------------------------------------------------------------

gateway() {
  local py=python3
  [[ -x .venv/bin/python ]] && py=.venv/bin/python
  log "创建/更新 Gateway 并挂上业务工具 target"
  AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/create_gateway.py
}

# ---- Identity 出向凭证 ---------------------------------------------------

identity() {
  local py=python3
  [[ -x .venv/bin/python ]] && py=.venv/bin/python

  if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    warn "DEEPSEEK_API_KEY 未设置,跳过 Identity 配置。"
    warn "填好 .env 里的 DEEPSEEK_API_KEY 后单独跑:./scripts/deploy.sh --identity"
    return 0
  fi

  log "配置 Identity 出向凭证(DeepSeek API Key + 自建 IdP 的 OAuth2)"
  # GATEWAY_CLIENT_SECRET 由 seed_auth.py 生成后打印,需要手工填回 .env
  if [[ -z "${GATEWAY_CLIENT_SECRET:-}" ]]; then
    warn "GATEWAY_CLIENT_SECRET 未设置,只配 DeepSeek API Key。"
    warn "机器客户端的 secret 在 seed 步骤打印过,填进 .env 后重跑 --identity"
    AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/setup_identity.py --skip-oauth
  else
    AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/setup_identity.py
  fi
}

# ---- 验证 ---------------------------------------------------------------

verify() {
  local issuer discovery
  issuer="$(stack_output "$IDP_STACK" IssuerUrl)"
  discovery="$(stack_output "$IDP_STACK" DiscoveryUrl)"
  [[ -n "$issuer" && "$issuer" != "None" ]] || die "取不到 IssuerUrl"

  log "GET ${discovery}"
  local doc
  doc="$(curl -fsS --max-time 15 "$discovery")" || die "discovery 拉不通"
  printf '%s' "$doc" | python3 -m json.tool

  local doc_issuer
  doc_issuer="$(printf '%s' "$doc" | python3 -c 'import json,sys; print(json.load(sys.stdin)["issuer"])')"
  [[ "$doc_issuer" == "$issuer" ]] \
    || die "discovery 里的 issuer(${doc_issuer})与预期(${issuer})不一致,CUSTOM_JWT 一定会失败"
  log "issuer 一致 ✓"

  log "GET ${issuer}/.well-known/jwks.json"
  curl -fsS --max-time 15 "${issuer}/.well-known/jwks.json" | python3 -m json.tool \
    || die "JWKS 拉不通"

  local logistics
  logistics="$(stack_output "$LOGISTICS_STACK" LogisticsUrl)"
  if [[ -n "$logistics" && "$logistics" != "None" ]]; then
    log "GET ${logistics}/health"
    curl -fsS --max-time 15 "${logistics}/health" \
      || warn "物流页健康检查失败,Browser 演示会用不了"
    echo
    # 表单元素的 id 是 Browser 工具的定位依据,顺手确认页面真渲染出来了
    if curl -fsS --max-time 15 "${logistics}/" | grep -q 'id="shipment-no"'; then
      log "物流查询表单渲染正常 ✓"
    else
      warn "物流页没有渲染出 #shipment-no,Browser 工具会定位失败"
    fi
  fi

  cat <<EOF

$(log "P0/P1 完成。Runtime 需要的环境变量:")

  PROJECT=${PROJECT}
  AWS_REGION=${AWS_REGION}
  ARTIFACT_BUCKET=$(stack_output "$FOUNDATION_STACK" ArtifactBucketName)
  BUSINESS_TABLE=$(stack_output "$FOUNDATION_STACK" BusinessTableName)
  MEMORY_TABLE=$(stack_output "$FOUNDATION_STACK" MemoryTableName)
  DEEPSEEK_API_KEY_PROVIDER=${DEEPSEEK_API_KEY_PROVIDER:-${PROJECT}-deepseek}
  GATEWAY_OAUTH_PROVIDER=${GATEWAY_OAUTH_PROVIDER:-${PROJECT}-gateway-oauth}
  LOGISTICS_URL=$(stack_output "$LOGISTICS_STACK" LogisticsUrl)
  GATEWAY_URL=<由 scripts/create_gateway.py 输出>

$(log "建 Runtime / Gateway 的 CUSTOM_JWT 要用:")

  discoveryUrl    ${discovery}
  allowedAudience ${PROJECT}

部署完 Runtime 后跑一次全能力自检:
  {"mode": "selftest"}   # 依次探测 8 项并产出报告,一项失败不影响其余

拿一个 token 试试:
  curl -s -u '<client_id>:<client_secret>' \\
    -d 'grant_type=password&username=${DEMO_USERNAME:-demo-user}&password=<password>' \\
    ${issuer}/oauth2/token | python3 -m json.tool
EOF
}

# ---- 镜像与 Runtime ------------------------------------------------------

runtime() {
  local py=python3
  [[ -x .venv/bin/python ]] && py=.venv/bin/python

  log "构建并推送 arm64 镜像"
  ./scripts/build_push.sh

  log "创建/更新 Runtime"
  # GATEWAY_URL 由 create_gateway.py 输出,需要填进 .env 后重跑这一步,
  # 否则 Agent 起来时会跳过业务工具(降级路径,不会崩)
  AWS_REGION="$AWS_REGION" PROJECT="$PROJECT" "$py" scripts/create_runtime.py
}

# ---- 入口 ---------------------------------------------------------------

main() {
  case "${1:-all}" in
    --code)
      preflight; push_all_code ;;
    --verify)
      preflight; verify ;;
    --seed)
      preflight; seed ;;
    --quick)
      preflight; quick ;;
    --gateway)
      preflight; gateway ;;
    --identity)
      preflight; identity ;;
    --runtime)
      preflight; runtime ;;
    all)
      preflight
      deploy_stacks
      push_all_code
      backfill_issuer
      seed
      verify
      identity
      gateway ;;
    *)
      die "未知参数:$1(可用:--code --verify --seed --quick --gateway --identity --runtime)" ;;
  esac
}

main "$@"
