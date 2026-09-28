# 依赖锁定说明

`requirements.txt` 的版本不是随手写的,有几个硬约束互相咬合,改任何一个都可能解不开:

| 包 | 锁定版本 | 为什么 |
| --- | --- | --- |
| `bedrock-agentcore` | 1.23.1 | 提供 `BedrockAgentCoreApp`(自带 `/invocations`、`/ping`、SSE 流式、`@app.async_task` 忙态上报)以及 Code Interpreter / Browser / Identity 客户端。用它就不需要 FastAPI。 |
| `strands-agents[openai,otel]` | 1.57.1 | `openai` extra 提供 `OpenAIModel`(DeepSeek 是 OpenAI 兼容接口);`otel` extra 提供 OTLP/HTTP exporter。 |
| `openai` | 2.9.0 | `strands-agents[openai]` 约束 `openai<3.0.0`,而 PyPI 上最新已是 3.x,**必须显式锁 2.x**,否则装出来直接不兼容。 |
| `mcp` | 1.23.0 | `bedrock-agentcore[strands-agents]` 要 `mcp<2.0.0`,`strands-agents` 要 `mcp<2.2`,交集取 1.x 最新。 |
| `playwright` | 1.63.0 | 只用 `connect_over_cdp` 连 AgentCore Browser,**不执行 `playwright install`**,镜像里不含浏览器内核,省几百 MB。 |
| `aws-opentelemetry-distro` | 0.20.0 | 提供 `opentelemetry-instrument` 入口与 AWS X-Ray 传播器。 |

## 验证记录

```
$ pip install --dry-run -r requirements.txt   # 共解析 121 个包,无冲突
bedrock-agentcore==1.23.1
strands-agents==1.57.1
openai==2.9.0
mcp==1.23.0
pydantic==2.13.5        # 受 bedrock-agentcore 的 pydantic<2.41.3 约束
starlette==1.7.0
uvicorn==0.54.0
opentelemetry-api==1.44.0
opentelemetry-sdk==1.44.0
boto3==1.43.103
```

上面是在本机 Python 3.14 上解析的结果。容器里是 Python 3.13,构建时会再解析一次;
`scripts/build_push.sh` 构建失败时优先看是不是这里的约束被破坏了。

## Lambda 侧依赖

IdP Lambda 刻意做到**零第三方依赖**:只用标准库(`hashlib` / `hmac` / `base64` / `json`)
加 Lambda 自带的 `boto3`。JWT 是手工拼的,签名交给 KMS,所以不需要 `PyJWT` 或
`cryptography`,也就不需要打 Lambda Layer。
