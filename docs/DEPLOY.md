# 部署手册

目标区域 **cn-northwest-1**(宁夏)。全程约 20 分钟,其中等 Gateway 和 Runtime
就绪各占几分钟。

## 前置条件

| 要求 | 说明 |
| --- | --- |
| 中国区 AWS 账号 | 凭证分区必须是 `aws-cn`,脚本会强制校验 |
| Docker + buildx | Runtime 只接受 `linux/arm64` 镜像 |
| Python 3.13 | 本地跑脚本和测试 |
| DeepSeek API Key | 从 https://platform.deepseek.com 申请 |

IAM 权限:部署者需要 CloudFormation、IAM(建角色)、Lambda、DynamoDB、S3、ECR、
KMS、API Gateway、`bedrock-agentcore` 与 `bedrock-agentcore-control` 的完整权限。
demo 阶段给 `AdministratorAccess` 最省事;生产要按资源收紧。

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # 填 AWS_PROFILE 和 DEEPSEEK_API_KEY
```

先在本地跑一遍测试,确认环境没问题(不需要 AWS 凭证):

```bash
.venv/bin/python -m pytest tests/ -q          # 475 个用例
.venv/bin/cfn-lint infra/*.yaml --region cn-northwest-1
```

## 一步到位

```bash
./scripts/deploy.sh            # 基础设施 + IdP + 工具 + 物流页 + seed + Identity + Gateway
```

跑完它会打印 `GATEWAY_URL` 和机器客户端的 secret。**把这两个值填回 `.env`**,
然后建 Runtime:

```bash
./scripts/deploy.sh --identity   # 用刚拿到的 secret 补上 OAuth2 provider
./scripts/deploy.sh --runtime    # 构建镜像 + 建 Runtime
```

为什么要分两次:Gateway 的 URL 和 IdP 的客户端密钥都是部署过程中才产生的,
而 Runtime 需要它们作为环境变量。没填也能起来 —— Agent 会跳过对应的工具
(见下面「分阶段验证」)。

## 分步执行

每一步都幂等,可以重复跑。

```bash
./scripts/deploy.sh --code       # 只重推三个 Lambda 的代码
./scripts/deploy.sh --seed       # 只重灌演示数据
./scripts/deploy.sh --verify     # 只跑连通性检查
./scripts/deploy.sh --identity   # 只配 Identity 出向凭证
./scripts/deploy.sh --gateway    # 只建/更新 Gateway
./scripts/deploy.sh --runtime    # 只构建镜像 + 建 Runtime
```

栈的部署顺序(`deploy.sh` 已经按序处理):

```
00-foundation.yaml      DynamoDB / S3 / ECR / IAM 角色
10-auth-idp.yaml        KMS 签名密钥 + IdP Lambda + HTTP API
20-business-tools.yaml  业务工具 Lambda
30-logistics-web.yaml   物流查询页(Browser 的靶子)
```

## 调用

```bash
export AGENT_RUNTIME_ARN=<create_runtime.py 输出的 ARN>

# 同步
python scripts/invoke.py --prompt "ORD-1024 到哪了,该赔多少?"

# 流式
python scripts/invoke.py --stream --prompt "帮我算一下超时赔付"

# 异步长任务
python scripts/invoke.py --async --wait --prompt "把本月工单做个趋势图"

# 全能力自检
python scripts/invoke.py --selftest
```

Runtime 默认是 `CUSTOM_JWT` 入向,所以要先从自建 IdP 拿 token:

```bash
export AGENT_JWT_TOKEN=$(curl -s -u "$DEMO_CLIENT_ID:$DEMO_CLIENT_SECRET" \
  -d "grant_type=password&username=$DEMO_USERNAME&password=$DEMO_PASSWORD" \
  "$ISSUER/oauth2/token" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

python scripts/invoke.py --prompt "你好"
```

想改成 SigV4 入向:`python scripts/create_runtime.py --auth iam --image ...`。
这种模式下 `invoke.py` 会带上 `runtimeUserId` —— **不带的话容器里取不到
workload access token,Identity 出向会失败**,报错是
`Workload access token has not been set`,和调用参数看不出关联。

## 版本与灰度

每次 `--runtime` 都会产生一个新版本。`DEFAULT` 端点始终跟最新版本,
`stable` 端点由你手工指定:

```bash
python scripts/create_runtime.py --show            # 看所有版本和端点
python scripts/create_runtime.py --promote 3       # 让 stable 指向版本 3
python scripts/invoke.py --qualifier stable --prompt "..."
```

出问题就把 `stable` 指回上一个版本,流量立即回退。

## 分阶段验证

Agent 按可用性降级 —— 某个能力还没配好,它照样能起来,只是少几个工具。
所以可以边部署边验:

| 阶段 | 跑什么 | 期望 |
| --- | --- | --- |
| 基础设施 | `./scripts/deploy.sh --verify` | discovery 文档的 `issuer` 与预期一致;物流页渲染出 `#shipment-no` |
| Runtime 起来 | `invoke.py --prompt "你好"` | 能对话(此时还没有业务工具) |
| Identity 配好 | `invoke.py --selftest` | Identity 一行从 `skipped` 变 `ok` |
| Gateway 配好 | `invoke.py --prompt "ORD-1024 状态"` | 真的调 `business___get_order` |
| 全部配好 | `invoke.py --selftest` | 8 项全绿 |

`selftest` 的关键性质:**一项失败不影响后面的步骤**。所以任何时候跑它都能
拿到全景,不会在第一个错误处停下。「没配置」显示 `skipped`,「坏了」显示
`failed` —— 这两个要分清,否则分阶段部署时报告会全是红的。

## 必须在真实账号里验证的项

下面这些我在本地无法验证(无有效凭证),部署后请逐项确认:

| 项 | 怎么验 | 失败的话 |
| --- | --- | --- |
| KMS 非对称密钥(`RSA_2048` / `SIGN_VERIFY`)在宁夏可用 | `10-auth-idp.yaml` 能建成 | 改用 Secrets Manager 存私钥 + 给 Lambda 打 PyJWT layer |
| AgentCore 能拉取 `execute-api` 上的 discovery 文档 | Gateway 进 `READY` 且 token 能调通 | 用 `setup_identity.py --oauth-metadata` 改成显式端点;Gateway 侧则需换成公网可达的 issuer |
| `aws.codeinterpreter.v1` 系统 identifier 可用 | selftest 的 CodeInterpreter 一行为 `ok` | 用 `CodeInterpreter.create_code_interpreter` 建自定义的,改 `CODE_INTERPRETER_ID` |
| `aws.browser.v1` 系统 identifier 可用 | selftest 的 Browser 一行为 `ok` | 同上,改 `BROWSER_ID` |
| Browser 沙箱能出网访问 `execute-api.cn-northwest-1.amazonaws.com.cn` | `track_shipment` 能抓到页面 | 给 browser session 配 `proxy_configuration`,或把物流页换成沙箱可达的地址 |
| Runtime 容器能出网访问 `api.deepseek.com` | selftest 的 Model 一行为 `ok` | 确认 `networkMode=PUBLIC`;若组织有出网管控需加白名单 |
| CloudWatch Transaction Search 已开启 | GenAI 看板里能看到 trace | 在 CloudWatch 控制台开启;X-Ray 在中国区可用 |
| `bedrock-agentcore` 的服务配额 | 建 Runtime / Gateway 不报 `ServiceQuotaExceeded` | 提工单加配额 |

已在本地验证过的(不需要重复确认):

- arm64 镜像构建、容器启动、`/ping` 返回 `Healthy`
- 容器内 `selftest` 跑完 8 步、`opentelemetry-instrument` 装上了 provider
- 容器到 `api.deepseek.com` 的网络与请求格式(拿到真实 401,说明只是 key 假)
- 三个 Lambda 的 zip 内容与 CFN `Handler` 配置一一对应
- 所有 CloudFormation 模板过 `cfn-lint`
- Gateway / Identity / Runtime 的 boto3 参数形状(用 botocore `ParamValidator` 校验)

## 清理

```bash
# 先删非 CloudFormation 管理的资源
python scripts/create_runtime.py --show   # 记下 runtime id
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <id> --region cn-northwest-1
python scripts/create_gateway.py --show   # 记下 gateway id 和 target id
python scripts/setup_identity.py --delete

# 再删栈(倒序)
for s in logistics-web business-tools auth-idp foundation; do
  aws cloudformation delete-stack --stack-name agentcore-cn-$s --region cn-northwest-1
  aws cloudformation wait stack-delete-complete --stack-name agentcore-cn-$s --region cn-northwest-1
done
```

S3 桶和 ECR 仓库都配了 `DeletionPolicy: Delete` + `EmptyOnDelete`,会连内容一起删。
KMS 密钥是 `PendingWindowInDays: 7`,删栈后还有 7 天可恢复。

## 排障

**Gateway 一直 `CREATE_FAILED`** — `create_gateway.py` 会打印 `statusReasons`,
那是唯一有用的信息。最常见的原因是 discovery URL 拉不通,或 `searchType`
被误传(中国区不支持语义检索,代码里完全不传这个字段)。

**Gateway 通了但 Agent 说工具加载失败** — 看 IdP 的 Lambda 日志。大概率是
`invalid_scope`(m2m 客户端的 scope 不够)或 `allowedClients` 不匹配。
客户端 ID 和 scope 都集中在 `scripts/naming.py`,三个脚本共用,正常不会漂移。

**所有调用都 403** — discovery 文档里的 `issuer` 和 token 里的 `iss` 必须
一字不差。`deploy.sh --verify` 会比对这两个值。

**Agent 起来了但没有记忆** — 看 Runtime 执行角色有没有 `-memory` 表的
读写权限,以及 `MEMORY_TABLE` 环境变量有没有注入(`create_runtime.py --show`
能看到完整的环境变量)。

**日志在哪** — Runtime 在 `/aws/bedrock-agentcore/runtimes/<id>-<endpoint>`,
三个 Lambda 在 `/aws/lambda/agentcore-cn-{idp,tools,logistics}`。
同步调用的响应体里带 `traceId`,可以直接拿去 CloudWatch 定位。
