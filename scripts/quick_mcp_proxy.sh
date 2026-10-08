#!/usr/bin/env bash
# Amazon Quick Desktop -> Local/stdio -> AgentCore Gateway 兼容代理。
#
# 用途:Quick Desktop Remote Connector 某些版本会在 Gateway 上持续返回 401。
# 本脚本每次启动时用 .env 中的 M2M 客户端换取短期 JWT,再通过固定版本的
# mcp-remote 将本地 stdio 转为远端 Streamable HTTP。
#
# Quick Desktop 配置:
#   类型:Local
#   Command:<本文件的绝对路径>
#   Arguments:留空
#   Environment variables:留空
#   Timeout:120
#
# stdout 专用于 MCP stdio 协议,所有诊断必须写 stderr。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "quick-mcp-proxy:找不到 $ENV_FILE" >&2
  exit 1
fi

# .env 是项目操作者维护的本地可信配置,且已被 gitignore。
# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

if [[ -z "${GATEWAY_URL:-}" ]]; then
  echo "quick-mcp-proxy:.env 缺少 GATEWAY_URL" >&2
  exit 1
fi

# 新部署优先使用 Quick 专用凭证；尚未创建时兼容回退到 Runtime M2M。
if [[ -n "${QUICK_CLIENT_ID:-}" && -n "${QUICK_CLIENT_SECRET:-}" ]]; then
  AUTH_CLIENT_ID="$QUICK_CLIENT_ID"
  AUTH_CLIENT_SECRET="$QUICK_CLIENT_SECRET"
elif [[ -n "${GATEWAY_CLIENT_ID:-}" && -n "${GATEWAY_CLIENT_SECRET:-}" ]]; then
  AUTH_CLIENT_ID="$GATEWAY_CLIENT_ID"
  AUTH_CLIENT_SECRET="$GATEWAY_CLIENT_SECRET"
else
  echo "quick-mcp-proxy:.env 缺少 Quick 或 Gateway M2M 客户端凭证" >&2
  exit 1
fi

case "$GATEWAY_URL" in
  https://*/mcp) ;;
  *) echo "quick-mcp-proxy:GATEWAY_URL 必须是 https://.../mcp" >&2; exit 1 ;;
esac

PY="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
  echo "quick-mcp-proxy:找不到 Python" >&2
  exit 1
fi
if ! command -v npx >/dev/null 2>&1; then
  echo "quick-mcp-proxy:找不到 npx,请安装 Node.js 22+" >&2
  exit 1
fi

TOKEN_ENDPOINT="${IDP_TOKEN_ENDPOINT:-}"
if [[ -z "$TOKEN_ENDPOINT" ]]; then
  GATEWAY_BASE="${GATEWAY_URL%/}"
  GATEWAY_BASE="${GATEWAY_BASE%/mcp}"
  RESOURCE_METADATA="$GATEWAY_BASE/.well-known/oauth-protected-resource"

  AUTH_SERVER="$(
    curl --fail --silent --show-error --max-time 20 "$RESOURCE_METADATA" |
      "$PY" -c 'import json,sys
p=json.load(sys.stdin); servers=p.get("authorization_servers") or []
if not servers: raise SystemExit("resource metadata 缺少 authorization_servers")
print(servers[0])'
  )"

  TOKEN_ENDPOINT="$(
    curl --fail --silent --show-error --max-time 20 \
      "$AUTH_SERVER/.well-known/oauth-authorization-server" |
      "$PY" -c 'import json,sys
p=json.load(sys.stdin); endpoint=p.get("token_endpoint")
if not endpoint: raise SystemExit("authorization metadata 缺少 token_endpoint")
print(endpoint)'
  )"
fi

ACCESS_TOKEN="$(
  curl --fail --silent --show-error --max-time 25 \
    -u "$AUTH_CLIENT_ID:$AUTH_CLIENT_SECRET" \
    -d 'grant_type=client_credentials' \
    --data-urlencode 'scope=gateway:invoke tools:read tools:write' \
    "$TOKEN_ENDPOINT" |
    "$PY" -c 'import json,sys
p=json.load(sys.stdin); token=p.get("access_token")
if not token: raise SystemExit("IdP 响应缺少 access_token")
print(token)'
)"

# 只通过环境变量传给 mcp-remote,避免 JWT 出现在进程参数或 Quick 配置里。
export AUTH_HEADER="Bearer $ACCESS_TOKEN"
unset ACCESS_TOKEN AUTH_CLIENT_SECRET GATEWAY_CLIENT_SECRET QUICK_CLIENT_SECRET DEMO_CLIENT_SECRET DEMO_PASSWORD DEEPSEEK_API_KEY

exec npx -y mcp-remote@0.14.3 "$GATEWAY_URL" \
  --header 'Authorization:${AUTH_HEADER}' \
  --silent
