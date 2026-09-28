#!/usr/bin/env bash
#
# 演示脚本:按顺序点亮中国区可用的全部 AgentCore 能力。
#
#   ./scripts/demo.sh              # 完整演示,每步之间等回车
#   ./scripts/demo.sh --auto       # 不等回车,一路跑完(录屏用)
#   ./scripts/demo.sh --quick      # 只跑自检和主链路
#   ./scripts/demo.sh --list       # 只列出会演示什么,不实际调用
#
# 前置:部署已完成,.env 里有 AGENT_RUNTIME_ARN 和 IdP 凭证。
# 没有的话先看 docs/DEPLOY.md。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- 配置 ----------------------------------------------------------------

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a && source .env && set +a
fi

PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python

AUTO=0
QUICK=0
LIST_ONLY=0
case "${1:-}" in
  --auto)  AUTO=1 ;;
  --quick) QUICK=1 ;;
  --list)  LIST_ONLY=1 ;;
  "")      ;;
  *)       echo "未知参数:$1(可用:--auto --quick --list)" >&2; exit 1 ;;
esac

BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
BLUE=$'\033[1;34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[1;31m'

step_no=0

banner() {
  step_no=$((step_no + 1))
  printf '\n%s━━━ %d. %s ━━━%s\n' "$BLUE" "$step_no" "$1" "$RESET"
  [[ -n "${2:-}" ]] && printf '%s%s%s\n' "$DIM" "$2" "$RESET"
  echo
}

note() { printf '%s%s%s\n' "$DIM" "$1" "$RESET"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%s[!]%s %s\n' "$YELLOW" "$RESET" "$1" >&2; }
die()  { printf '%s[x]%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }

pause() {
  [[ "$AUTO" == "1" ]] && { sleep 2; return; }
  printf '\n%s按回车继续…%s' "$DIM" "$RESET"
  read -r _ || true
  echo
}

# ---- 前置检查 ------------------------------------------------------------

if [[ "$LIST_ONLY" == "1" ]]; then
  cat <<'EOF'
这个脚本会依次演示:

  1. 自建 OIDC IdP        拿 JWT(中国区没有 Cognito,IdP 是自建的)
  2. 全能力自检           一次点亮 8 项,失败项不影响后续
  3. 主业务链路           查订单 → 读物流页 → 沙箱算钱 → 开工单 → 记偏好
  4. 流式返回             SSE 逐 token,工具调用实时可见
  5. 异步长任务           立刻返回 taskId,/ping 期间是 HealthyBusy
  6. 会话隔离             同一用户两个 session 互不串话
  7. 跨会话长期记忆       新 session 仍记得偏好
  8. 版本与端点灰度       DEFAULT 跟最新、stable 钉在指定版本

对应的 AgentCore 能力:
  Runtime / Gateway / Identity / Code Interpreter / Browser / Observability
  + MemoryLite(中国区没有 Memory,这块是自建的)
EOF
  exit 0
fi

command -v aws >/dev/null || die "缺少 aws cli"
[[ -n "${AGENT_RUNTIME_ARN:-}" ]] || die "缺少 AGENT_RUNTIME_ARN,先跑 ./scripts/deploy.sh --runtime"
[[ -n "${DEMO_CLIENT_SECRET:-}" ]] || die "缺少 DEMO_CLIENT_SECRET,先跑 ./scripts/deploy.sh --seed"

AWS=(aws --region "${AWS_REGION:-cn-northwest-1}")
[[ -n "${AWS_PROFILE:-}" ]] && AWS+=(--profile "$AWS_PROFILE")

"${AWS[@]}" sts get-caller-identity >/dev/null 2>&1 \
  || die "AWS 凭证不可用。SSO 的话跑:aws sso login --profile ${AWS_PROFILE:-<profile>}"

RUNTIME_ID="${AGENT_RUNTIME_ARN##*/}"

# ---- 1. 自建 IdP ---------------------------------------------------------

banner "自建 OIDC IdP:换一个 JWT" \
  "中国区 Cognito 在宁夏不可用,而 Gateway 禁止匿名入向 —— 所以 IdP 是自建的:
KMS 签 RS256 + Lambda + DynamoDB,零第三方依赖。"

ISSUER="$("${AWS[@]}" cloudformation describe-stacks \
  --stack-name "${PROJECT:-agentcore-cn}-auth-idp" \
  --query "Stacks[0].Outputs[?OutputKey=='IssuerUrl'].OutputValue" --output text)"
note "issuer = $ISSUER"

note "GET /.well-known/openid-configuration"
curl -fsS --max-time 20 "$ISSUER/.well-known/openid-configuration" \
  | "$PY" -c 'import json, sys
d = json.load(sys.stdin)
print("  grant_types :", d["grant_types_supported"])
print("  签名算法     :", d["id_token_signing_alg_values_supported"])' 

note "GET /.well-known/jwks.json(公钥从 KMS 派生,私钥不出 KMS)"
curl -fsS --max-time 20 "$ISSUER/.well-known/jwks.json" \
  | "$PY" -c 'import json, sys
k = json.load(sys.stdin)["keys"][0]
print("  kid={}  kty={}  alg={}".format(k["kid"], k["kty"], k["alg"]))' 

AGENT_JWT_TOKEN="$(curl -fsS --max-time 25 -u "$DEMO_CLIENT_ID:$DEMO_CLIENT_SECRET" \
  -d "grant_type=password&username=$DEMO_USERNAME&password=$DEMO_PASSWORD" \
  "$ISSUER/oauth2/token" \
  | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')"
export AGENT_JWT_TOKEN
[[ -n "$AGENT_JWT_TOKEN" ]] || die "拿不到 token"

"$PY" - <<'EOF'
import base64, json, os
p = os.environ["AGENT_JWT_TOKEN"].split(".")[1]
c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
print(f"  actor_id = {c.get('actor_id')}   scope = {c.get('scope')}")
EOF
ok "JWT 已签发($(printf '%s' "$AGENT_JWT_TOKEN" | wc -c | tr -d ' ') 字符)"

note "错密码会被拒(顺手验一下 IdP 真的在校验):"
curl -s --max-time 20 -u "$DEMO_CLIENT_ID:$DEMO_CLIENT_SECRET" \
  -d "grant_type=password&username=$DEMO_USERNAME&password=definitely-wrong" \
  "$ISSUER/oauth2/token" \
  | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
print("  ->", d.get("error"), "|", d.get("error_description"))' 
pause

# ---- 2. 自检 -------------------------------------------------------------

banner "全能力自检" \
  "依次探测 8 项。一项失败不影响后面 —— 自检的意义是一次看全景,
而不是在第一个错误处停下。「没配置」和「坏了」是两种状态。"

"$PY" scripts/invoke.py --selftest --new-session --timeout 150 2>&1 \
  | grep -vE 'X-Amz' | sed 's|报告(1 小时内有效).*|报告已上传 S3|'
pause

# ---- 3. 主链路 -----------------------------------------------------------

banner "主业务链路:一句话串起五个能力" \
  "订单接口只说「超期了」,卡在哪一环只有承运商网页上写着 ——
所以必须用 Browser 真去读页面。赔付金额交给 Code Interpreter 按分算,
不让模型心算。"

note "提问:ORD-1024 一直没收到,帮我查清楚卡在哪、该赔多少,然后开工单。"
echo
"$PY" scripts/invoke.py --new-session --timeout 180 \
  --prompt "ORD-1024 一直没收到。帮我查清楚卡在哪、该赔多少钱,然后开个工单。" 2>&1 | tail -30
pause

if [[ "$QUICK" == "1" ]]; then
  printf '\n%s--quick 模式结束。完整演示去掉 --quick。%s\n' "$DIM" "$RESET"
  exit 0
fi

# ---- 4. 流式 -------------------------------------------------------------

banner "流式返回(SSE)" \
  "返回异步生成器,框架自动转 SSE。工具调用实时可见。"

"$PY" scripts/invoke.py --stream --new-session --timeout 150 \
  --prompt "ORD-1026 超期了吗?一句话回答。" 2>&1 | tail -8
pause

# ---- 5. 异步长任务 -------------------------------------------------------

banner "异步长任务" \
  "立刻返回 taskId,后台继续跑。期间 /ping 返回 HealthyBusy,
Runtime 据此知道这个实例还在干活,不会回收它。"

"$PY" scripts/invoke.py --async --wait --new-session --timeout 180 \
  --prompt "把 CUST-002 的订单查一遍,汇总超期情况。" 2>&1 | tail -16
pause

# ---- 6. 会话隔离 ---------------------------------------------------------

banner "会话隔离" \
  "中国区没有 AgentCore Memory,短期记忆是自建的(DynamoDB + TTL)。
同一个用户的两个 sessionId 必须互不串话。"

note "会话 A:告诉它一个只有 A 知道的编号"
"$PY" scripts/invoke.py --new-session --timeout 120 \
  --prompt "这次排查的内部编号是 CASE-7788,记在心里,后面我会问你。先回复「已记下 CASE-7788」。" 2>&1 | tail -4
echo
note "会话 B(全新 session):问它这个编号"
"$PY" scripts/invoke.py --new-session --timeout 120 \
  --prompt "这次排查的内部编号是多少?如果你不知道就直接说不知道。" 2>&1 | tail -4
note "预期:B 不知道 —— 编号只存在 A 的短期记忆里,没进长期记忆。"
pause

# ---- 7. 跨会话长期记忆 ---------------------------------------------------

banner "跨会话长期记忆" \
  "长期记忆按 actor_id 存,不带 TTL,所以换会话仍在。
actor_id 从入向 JWT 解出来 —— 这也是数据隔离的依据。"

note "全新会话,问它记得什么偏好:"
"$PY" scripts/invoke.py --new-session --timeout 120 \
  --prompt "你还记得我的联系偏好吗?直接说,不用调工具。" 2>&1 | tail -6

note "直接看 DynamoDB 里存在哪个分区下(actor_id 从 JWT 解出,不是拼的):"
ACTOR_ID="$("$PY" - <<'EOF'
import base64, json, os
p = os.environ["AGENT_JWT_TOKEN"].split(".")[1]
c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
print(c.get("actor_id") or c.get("sub") or "anonymous")
EOF
)"
note "分区键 = ACTOR#${ACTOR_ID}"
"${AWS[@]}" dynamodb query --table-name "${PROJECT:-agentcore-cn}-memory" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :p)" \
  --expression-attribute-values "{\":pk\":{\"S\":\"ACTOR#${ACTOR_ID}\"},\":p\":{\"S\":\"FACT#\"}}" \
  --query 'Items[].[SK.S,value.S]' --output text 2>&1 | sed 's/^/  /'

note "换个 actor 看隔离(应该是空的):"
n="$("${AWS[@]}" dynamodb query --table-name "${PROJECT:-agentcore-cn}-memory" \
  --key-condition-expression "PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"ACTOR#anonymous"}}' \
  --query 'Count' --output text 2>/dev/null || echo "?")"
note "  ACTOR#anonymous 条目数 = $n"
pause

# ---- 8. 版本与灰度 -------------------------------------------------------

banner "版本与端点灰度" \
  "每次 UpdateAgentRuntime 产生一个新版本。DEFAULT 端点跟最新,
命名端点(stable)可以钉在任意版本 —— 出问题把它指回上一版即可回退。"

note "当前所有版本:"
"${AWS[@]}" bedrock-agentcore-control list-agent-runtime-versions \
  --agent-runtime-id "$RUNTIME_ID" --max-results 5 \
  --query 'agentRuntimes[].[agentRuntimeVersion,status]' --output text 2>/dev/null \
  | head -5 | sed 's/^/  版本 /' || true

echo
note "两个端点分别指向:"
for ep in DEFAULT stable; do
  v="$("${AWS[@]}" bedrock-agentcore-control get-agent-runtime-endpoint \
    --agent-runtime-id "$RUNTIME_ID" --endpoint-name "$ep" \
    --query '[targetVersion,liveVersion,status]' --output text 2>/dev/null || echo "-")"
  printf '  %-8s targetVersion/liveVersion/status = %s\n' "$ep" "$v"
done

echo
note "指定端点调用(--qualifier stable):"
"$PY" scripts/invoke.py --qualifier stable --new-session --timeout 120 \
  --prompt "只回复:stable 端点正常" 2>&1 | tail -3
note "回退用法:python scripts/create_runtime.py --promote <版本号>"

# ---- 收尾 ---------------------------------------------------------------

cat <<EOF

${BLUE}━━━ 演示结束 ━━━${RESET}

${BOLD}这套 demo 覆盖的中国区可用能力${RESET}
  Runtime           容器 / 流式 / 异步长任务 / 会话隔离 / 版本与端点
  Gateway           5 个业务工具,CUSTOM_JWT 入向
  Identity          出向 DeepSeek API Key + 自建 IdP 的 OAuth2
  Code Interpreter  沙箱算赔付、出图
  Browser           Playwright over CDP 填表抓页面
  Observability     自定义 span + trace id 贯穿

${BOLD}中国区不可用、由本项目补位的${RESET}
  Memory      -> MemoryLite(DynamoDB,STM 带 TTL + 会话摘要 + LTM)
  Cognito     -> 自建 OIDC IdP(KMS 签 RS256,私钥不出 KMS)
  Bedrock 模型 -> DeepSeek 官方 API(OpenAI 兼容)

${DIM}观测:CloudWatch 日志组 /aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT
每次调用的响应体里都带 traceId,可直接拿它去 CloudWatch 定位。
踩过的坑和设计取舍见 docs/DEPLOY.md。${RESET}
EOF
