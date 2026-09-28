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

## 中国区已知的 SDK 缺陷(本项目已规避)

这三处都是 SDK 把 AWS 域名硬编码成 `.amazonaws.com`、没处理 `aws-cn` 分区需要的
`.cn` 后缀。全部用 DNS 查询和容器日志实测确认过。

| 位置 | 拼出来的错误域名 | 规避方式 |
| --- | --- | --- |
| `bedrock_agentcore._utils.endpoints.get_data_plane_endpoint()`(Browser 的 CDP 地址) | `bedrock-agentcore.<region>.amazonaws.com` | `src/agent/tools/browser.py` 的 `_fix_china_ws_url()`,幂等,SDK 修好后自动变空操作 |
| `aws-opentelemetry-distro` 的 logs exporter | `logs.<region>.amazonaws.com` | 关掉 OTLP 日志导出,容器 stdout 本来就进 CloudWatch |
| `aws-opentelemetry-distro` 的 trace exporter | `xray.<region>.amazonaws.com` | 关掉。试过显式指向正确域名,换来 `403 Forbidden` —— X-Ray 的 OTLP 端点要 SigV4,手工设 endpoint 会绕过 distro 自己的签名逻辑 |

后两个的后果比"少一条 trace"严重得多:exporter 解析失败后在后台反复重试,
把请求线程拖住。

另外有一个**和中国区无关、但同样只在真机上才暴露**的坑,记在这里因为它花的
排查时间最长:

**绝不要跨事件循环 await Playwright 对象。** 它们绑定在创建时的那个循环上。
早先 `LazyBrowser.close()` 是注册给 `ExitStack` 的同步回调,在 async 函数里
unwind 时它会另开线程跑新循环去 await 清理 —— 直接挂死,不抛异常、不打日志、
不超时。症状是 8 个自检步骤 6.6 秒全跑完,然后整次调用静默挂到客户端读超时。

因为挂死不产生日志,连着六轮都定位错了地方(日志最后一行只说明"到这里还活着",
不说明下一步是什么)。现在的做法:同步路径只停 AgentCore 会话(那是真正占沙箱、
要计费的),Playwright 交给进程回收;异步调用方用 `AsyncExitStack` +
`push_async_callback(aclose)`,让清理在拥有对象的那个循环里做。
有 AST 测试断言 `close()` 里不许出现 `asyncio.run` / `new_event_loop` /
`ThreadPoolExecutor`。

## 已在真实账号里确认可用的项

下面这些在 cn-northwest-1(账号实测)已确认,不需要再验:

| 项 | 证据 |
| --- | --- |
| `bedrock-agentcore-control` / `bedrock-agentcore` 在本区可用 | `list-gateways` / `list-code-interpreter-sessions` 正常响应 |
| KMS `RSA_2048` + `SIGN_VERIFY` | 建密钥成功,支持 6 种签名算法;`10-auth-idp.yaml` 正常建栈 |
| 纯 stdlib DER 解析器对真实 KMS 公钥正确 | 与 `openssl` 独立解析的 modulus 逐位一致,2048 bit,e=65537 |
| 真实 KMS 签的 JWT 能被 JWKS 公钥验过 | 篡改 payload / `alg:none` / 换密钥签 三类攻击均被正确拒绝 |
| `aws.codeinterpreter.v1` | `status: READY`,沙箱执行 51-64 ms |
| `aws.browser.v1` | `status: READY`,CDP 连接成功并抓到页面 366 字符 |
| AgentCore 能拉取 `execute-api` 上的 discovery 文档 | Gateway 24 秒进 `READY`,`CUSTOM_JWT` 被接受 |
| 自建 IdP 三种 grant | `password` / `client_credentials` / `refresh_token` 均能签出 token;错密码正确拒绝 |
| Gateway 全链路 | 自建 IdP 签 token → 验 `CUSTOM_JWT` → 剥 `business___` 前缀 → Lambda 查 DynamoDB |
| Runtime 容器出网到 `api.deepseek.com` | 模型返回 `pong`,完整工具循环 13 秒 |
| Identity 出向取 DeepSeek key | 自检 Identity 一项通过,provider 名 `agentcore-cn-deepseek` |
| Browser 沙箱出网到 `execute-api.*.amazonaws.com.cn` | 抓到物流页 366 字符 |
| 按用户的数据隔离 | 配 `requestHeaderAllowlist` 后,LTM 正确写入 `ACTOR#actor-demo-user`,`ACTOR#anonymous` 为空 |
| 长期记忆跨会话 | 全新 session 仍能读出之前记住的两条偏好 |
| 完整业务链路 | 查订单 → Browser 查物流 → 沙箱算赔付 → 开工单 → 记偏好,25 秒,工单 summary 含具体异常原因 |
| arm64 镜像 / 版本与端点灰度 | 多次 `UpdateAgentRuntime` 产生版本 1→11,`stable` 端点可钉版本 |

### CUSTOM_JWT 入向必须显式放行 Authorization 头

AgentCore 验证完入向 JWT 后,**不会**把原始 `Authorization` 头透给容器。
容器实际只收到两个头:`baggage` 和 `workloadaccesstoken`。而那个 WAT 是
**不透明的**(实测 2895 字符、单段、不是 JWT),数据面也没有任何 API 能把它
反解回身份 —— `GetWorkloadAccessToken*` 全是"用身份换 token"的单向操作。

所以必须在 `CreateAgentRuntime` 里配:

```python
"requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]}
```

**不配的后果是静默的**:容器解不出 `actor_id`,一律回落 `anonymous`,
于是所有用户的长期记忆挤在 `ACTOR#anonymous` 一个分区里。不报错、不告警,
只是数据隔离根本不存在。这个坑我是在真机上查 DynamoDB 才发现的 ——
`FACT#notify_channel` 写在了 `ACTOR#anonymous` 而不是 `ACTOR#actor-demo-user`。

配上之后容器能正常解出 `actor_id` / `username` / `scope`。

### 版本切换有会话亲和性

`UpdateAgentRuntime` 之后 `liveVersion` 立刻变成新版本,但**已有会话仍在旧
容器实例上**。用同一个 `runtimeSessionId` 继续调会打到旧代码,表现成"改了没生效"。
验证新版本时换一个全新的 session id 强制冷启动。

### 部署时踩到的两个 IAM 陷阱(已修进模板)

`GetResourceApiKey` / `GetResourceOauth2Token` 的授权资源**不是 token vault**,
而是调用方的 **workload identity**:

```
arn:aws-cn:bedrock-agentcore:<region>:<acct>:workload-identity-directory/default/workload-identity/<runtime-name>
```

只授 `token-vault/*` 会让 Identity / 模型 / Gateway 三项同时 `AccessDenied`。

而且这两个 action **不返回解密后的明文** —— 容器还要自己去 Secrets Manager 读
AgentCore 托管的那条密文,所以还需要 `secretsmanager:GetSecretValue`,
资源收敛到 `secret:bedrock-agentcore-identity!*` 前缀。

## 仍需按自己环境确认的项

| 项 | 怎么验 | 失败的话 |
| --- | --- | --- |
| CloudWatch Transaction Search 已开启 | GenAI 看板里能看到 trace | 在 CloudWatch 控制台开启 |
| `bedrock-agentcore` 服务配额 | 建 Runtime / Gateway 不报 `ServiceQuotaExceeded` | 提工单加配额 |
| 组织级出网管控 | 自检的 Model / Browser 两项通过 | 给 `api.deepseek.com` 加白名单 |

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
