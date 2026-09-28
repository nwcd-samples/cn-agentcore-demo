# AgentCore Runtime 要求 linux/arm64 容器,监听 0.0.0.0:8080,
# 暴露 POST /invocations 与 GET /ping。
#
# 构建:见 scripts/build_push.sh,平台由 buildx 的 --platform 指定,
# 不在 FROM 里硬编码(硬编码会触发 buildx 的
# FromPlatformFlagConstDisallowed 告警,且让镜像无法多平台复用)。
#
# 基础镜像用 public.ecr.aws 而不是 Docker Hub:
# 实测 Docker Hub 的 auth.docker.io 在这个网络环境下被拦截,
# 而 public.ecr.aws 可达 —— 中国区构建本来也该优先用它。
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 依赖层单独缓存,改代码不用重装依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/agent /app/src/agent

# 非 root 运行
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# opentelemetry-instrument 负责把 trace 送到 AgentCore Observability。
# Runtime 会注入 OTEL_* 环境变量,不要在镜像里硬编码 endpoint。
CMD ["opentelemetry-instrument", "python", "-m", "agent.main"]
