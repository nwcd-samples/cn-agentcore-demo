# AgentCore 客户 Demo 演示指南

## 演示目标

不要把本 Demo 演成 AgentCore 功能清单，而要用一个完整的售后业务故事，让客户理解三件事：

1. 这不是只会聊天的机器人，而是能够完成真实业务动作的 Agent。
2. AgentCore 负责安全运行、工具调用、凭证管理、沙箱执行和可观测性。
3. 中国区缺失的能力由项目明确补位，没有把自建能力包装成 AgentCore 原生能力。

推荐演示时长为 **15 分钟**。技术客户可以扩展到 20～30 分钟。

---

## 一、推荐演示流程

### 1. 介绍业务问题（2 分钟）

推荐开场话术：

> 今天演示的是电商售后场景。客户问“订单为什么没到、能赔多少钱”，传统客服需要在订单系统、物流网站、赔付规则和工单系统之间来回切换。
>
> 这个 Agent 会自主完成查询、判断、计算和建单，但所有外部操作仍然经过受控工具和身份授权。

不要一开始就展开 Runtime、Gateway、Identity 等技术名词，否则非技术客户容易失去注意力。

### 2. 用简化架构图解释（2 分钟）

```text
客户
  │ JWT
  ▼
AgentCore Runtime
  │
  ├─ Gateway ── 订单 / 工单 / 赔付规则
  ├─ Browser ── 承运商物流网页
  ├─ Code Interpreter ── 精确计算赔付
  ├─ Identity ── DeepSeek Key / Gateway OAuth Token
  └─ Observability ── Trace / 日志 / 调用链
```

一句话解释：

> 模型负责理解和决策；AgentCore 负责让模型以可控、安全、可观测的方式使用企业系统。

演示时要明确区分：

- DeepSeek 是推理模型，不是 AgentCore。
- OIDC IdP 和 MemoryLite 是中国区能力缺失后的自建补位。
- Runtime、Gateway、Identity、Browser、Code Interpreter、Observability 是 AgentCore 原生能力。

---

## 二、演示前准备

### 1. 登录 AWS SSO

```bash
aws sso login --profile <你的 AWS_PROFILE>
```

### 2. 加载本地配置

```bash
set -a
source .env
set +a
```

不要在投影屏幕上打开 `.env`，也不要展示 JWT、API Key 或客户端密钥。

### 3. 私下运行全能力自检

```bash
python scripts/invoke.py --selftest
```

预期结果为 8/8 通过。全能力自检会写入 MemoryLite，并把报告上传到 S3，因此建议在客户到场前执行。

自检不适合作为业务客户演示的第一部分。技术客户有兴趣时，可以在业务故事结束后展示。

### 4. 获取演示用户 JWT

```bash
IDP_TOKEN_ENDPOINT=$(aws cloudformation describe-stacks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --stack-name "${PROJECT:-agentcore-cn}-auth-idp" \
  --query "Stacks[0].Outputs[?OutputKey=='TokenEndpoint'].OutputValue" \
  --output text)

export AGENT_JWT_TOKEN=$(curl -s \
  -u "$DEMO_CLIENT_ID:$DEMO_CLIENT_SECRET" \
  -d "grant_type=password&username=$DEMO_USERNAME&password=$DEMO_PASSWORD" \
  "$IDP_TOKEN_ENDPOINT" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

这一步体现的是 Runtime 入向 `CUSTOM_JWT` 鉴权。不要打印 `$AGENT_JWT_TOKEN`。

### 5. 演示环境检查

客户到场前确认：

- DeepSeek API 额度正常。
- Runtime、Gateway 和两个端点均为 `READY`。
- `ORD-1024` 演示数据存在。
- 物流页面 `/health` 返回正常。
- CloudWatch 控制台已经登录并打开到对应区域。
- 终端字体放大，关闭系统通知。
- 准备一份截图或录屏作为网络故障时的备用材料。

---

## 三、核心业务链路演示（约 6 分钟）

### 1. 发起客户请求

建议使用下面的 Prompt。它既覆盖完整业务链路，也明确授权了工单写操作：

```bash
python scripts/invoke.py --new-session --stream \
  --prompt "订单 ORD-1024 为什么还没到？请查询真实物流状态并按赔付政策计算金额。如果确认超时，直接为我创建物流延迟工单。以后此类进展请用短信通知我。"
```

使用 `--stream` 后，客户可以看到逐步返回的文本和工具调用状态，避免在 Browser、Identity 或沙箱启动期间误以为系统卡住。

### 2. 工具调用出现时的讲解话术

| 屏幕上的动作 | 给客户的解释 | AgentCore 的作用 |
| --- | --- | --- |
| `business___get_order` | 查询订单金额、承诺送达时间、运单号 | Gateway 管理企业工具 |
| `track_shipment` | 物流信息没有 API，Agent 像用户一样填写网页表单 | Browser 托管浏览器 |
| `business___get_refund_policy` | 获取企业赔付规则，不允许模型自己编规则 | Gateway |
| `run_python` | 按“分”计算，避免浮点误差和模型心算错误 | Code Interpreter |
| `business___create_ticket` | 真正创建售后工单 | Gateway 执行业务动作 |
| `remember` | 记录用户偏好的短信通知方式 | 自建 MemoryLite |

演示时重点强调：

> Agent 没有直接访问 DynamoDB，也没有把 DeepSeek Key 写进容器。订单、工单、凭证和沙箱都通过不同的受控能力访问。

### 3. 最终业务结果

最终结果应让客户清楚看到以下信息：

```text
物流状态：滞留在西安中转中心
异常原因：分拣设备故障
超期时间：以实时查询结果为准
预计赔付：194.85 元
工单编号：TKT-xxxx
通知偏好：短信
```

与只展示一段自然语言相比，这种结构化结果更容易让客户理解业务价值。

---

## 四、展示跨会话长期记忆（约 2 分钟）

业务链路完成后，开启全新会话：

```bash
python scripts/invoke.py --new-session \
  --prompt "我之前希望用什么方式接收售后进展？"
```

推荐讲解：

> 这是一个全新的 session，但还是同一个用户。短期对话已经隔离，长期偏好仍然保留。
>
> 中国区当前没有 AgentCore Memory，因此本 Demo 使用 DynamoDB 实现 MemoryLite。如果部署到支持 AgentCore Memory 的区域，可以替换为托管能力。

不要把 MemoryLite 描述成 AgentCore 原生 Memory。

---

## 五、展示可观测性和安全（约 3 分钟）

业务演示完成后再打开 CloudWatch，展示刚才调用产生的 trace。

建议展示：

- Runtime 调用；
- 模型调用；
- Gateway 工具调用；
- Browser 和 Code Interpreter 耗时；
- 每一步的成功或失败状态；
- session、actor 和 trace ID。

推荐讲解：

> Agent 的执行过程不是黑盒。我们可以看到调用了哪个工具、耗时多久、在哪一步失败，但不会把客户原话、订单内容或凭证明文写进 span。

身份部分可以展示：

```text
resolved_actor: actor-demo-user
is_anonymous: false
has_authorization: true
```

这说明 `Authorization` 已被 Runtime 验证并正确透传，长期记忆不会全部落入 `anonymous` 用户分区。

---

## 六、AgentCore 在业务链路中的作用

| 能力 | 在 Demo 中解决的问题 | 如果没有该能力 |
| --- | --- | --- |
| Runtime | 承载 Agent，支持同步、流式、异步、版本和端点 | 需要自己管理容器、协议、伸缩和发布 |
| Gateway | 把订单、工单和政策封装成受控 MCP 工具 | 模型可能直接接触内部数据库或散乱 API |
| Identity | 安全取得 DeepSeek Key 和 Gateway M2M Token | 凭证容易被写进镜像或环境变量 |
| Browser | 操作必须提交表单的物流网页 | 只能接入有正式 API 的系统 |
| Code Interpreter | 精确计算赔付并生成图表或报告 | 模型心算容易出错，执行环境难隔离 |
| Observability | 记录 agent、model、tool 调用链和耗时 | 线上问题难排查、难审计 |
| CUSTOM_JWT | 验证最终用户身份并解析 actor | 无法安全地做用户隔离和个性化 |
| 版本与端点 | `DEFAULT` 跟随最新版，`stable` 可固定版本 | 发布失败时难以快速回退 |

可以用一句话总结：

> DeepSeek 提供推理能力，AgentCore 提供企业级运行和治理能力。

---

## 七、面向不同客户选择演示深度

### 业务客户：10～15 分钟

只演示：

1. 售后问题；
2. 完整业务链路；
3. 新会话仍记得通知偏好；
4. 最终业务结果和业务价值。

不要展开 SDK、中国区域名修复、Playwright 事件循环等实现细节。

### 技术客户：20～30 分钟

业务链路后可以继续运行：

```bash
./scripts/demo.sh
```

该脚本会在每个环节暂停，适合边操作边解释。

无人值守完整演示：

```bash
./scripts/demo.sh --auto
```

只查看演示大纲：

```bash
./scripts/demo.sh --list
```

完整脚本覆盖：

1. 自建 OIDC IdP 获取 JWT；
2. 8 项全能力自检；
3. 主业务链路；
4. SSE 流式返回；
5. 异步任务和 `HealthyBusy`；
6. session 隔离；
7. 跨会话长期记忆；
8. Runtime 版本及 `stable` 端点灰度。

技术客户尤其应该看到 Gateway、Identity、Observability 和版本回滚，因为这些是 AgentCore 相对于“普通模型加 Lambda”的核心价值。

---

## 八、图形化最终用户界面

项目已经提供一个可直接运行的本地图形化演示台:

- 页面文件:`web/demo.html`
- 本地安全代理:`scripts/demo_web.py`
- 访问地址:`http://127.0.0.1:8765`
- 部署位置:演示人员的电脑,不部署到公网

后端会自动读取 `.env`,从 CloudFormation 发现自建 IdP,在服务端换取 JWT 并代理
AgentCore Runtime 的 SSE。AWS 凭证、IdP 密钥、用户密码和 JWT 都不会发送到页面。

启动方式:

```bash
aws sso login --profile <你的 AWS_PROFILE>   # SSO 仍有效时可跳过
.venv/bin/python scripts/demo_web.py
```

脚本默认自动打开浏览器。只启动服务、不打开浏览器:

```bash
.venv/bin/python scripts/demo_web.py --no-browser
```

更换本地端口:

```bash
.venv/bin/python scripts/demo_web.py --port 9000
```

该服务只监听 `127.0.0.1`,适合现场演示或会议屏幕共享。不要通过端口映射或反向代理
直接暴露公网;公网版本需要额外设计用户登录、会话授权、限流和托管后端。

页面顶部有四种演示模式:

1. **Demo 介绍**:默认客户首页,先说明 ORD-1024 业务主线、传统售后痛点、方案价值、
   AgentCore 架构与中国区能力边界,再通过 CTA 进入后续演示。
2. **一站式演示**:一句话触发完整售后链路,展示最终业务价值。
3. **分步骤演示**:依次点击 6 个步骤,用独立的同一会话展示 Gateway 查订单、
   Browser 查物流、读取政策、Code Interpreter 计算、工单查重/创建和记忆偏好。
   工单与记忆步骤执行前会弹出确认。
4. **业务资源查看**:查看业务表中的所有订单、运单和工单,可在执行前后对比数据变化。
   该视图只读且只返回业务字段,不会读取认证表、Memory 表或展示 DynamoDB 存储键。

推荐演示顺序是“Demo 介绍（建立业务与架构认知）→ 业务资源查看（介绍素材）→
分步骤演示（解释能力）→ 一站式演示（展示完整体验）”。

### 1. 左侧：对话区

显示用户问题和 Agent 最终回答。

### 2. 右侧：执行进度

只显示安全的工具状态，不显示模型内部思维链：

```text
✓ 已验证用户身份
✓ 已查询订单 ORD-1024
✓ 已访问承运商物流页面
✓ 已读取赔付政策
✓ 已在沙箱完成赔付计算
✓ 已创建工单 TKT-xxxx
✓ 已记录短信通知偏好
```

不要显示：

- Chain of thought；
- JWT；
- API Key；
- Workload Access Token；
- 未过滤的工具原始 JSON；
- 完整异常堆栈。

### 3. 底部：业务结果卡片

```text
订单：ORD-1024
状态：运输异常
赔付：¥194.85
工单：TKT-xxxx
下一步：短信通知处理进度
```

可以提供折叠的“技术详情”：

```text
Session ID
Trace ID
使用的 AgentCore 能力
总耗时
工具调用次数
```

### 4. 推荐 Web 调用架构

浏览器不应直接持有 AWS 凭证，推荐增加后端代理层：

```text
Web 前端
   │ SSE
   ▼
应用后端 / BFF
   │ 获取用户 JWT
   ▼
InvokeAgentRuntime
```

后端负责：

- 登录和 JWT 获取；
- 调用 AgentCore Runtime；
- 将 SSE 安全地转发给前端；
- 过滤工具事件和错误信息；
- 保存 session ID；
- 向前端返回 trace ID。

---

## 九、客户常见问题及回答

### 为什么用了 DeepSeek，还需要 AgentCore？

> DeepSeek 负责语言理解和推理，但不负责安全访问企业工具、凭证托管、沙箱执行、版本发布和调用链审计。AgentCore 补齐的是企业级运行与治理层。

### 为什么不直接让模型调用 Lambda？

> Gateway 提供统一工具协议、鉴权边界、工具描述和后续治理能力。模型只看到允许调用的工具，而不是直接拥有内部系统权限。

### Browser 和普通 HTTP 请求有什么区别？

> Browser 可以真实操作网页、填写表单和读取渲染后的结果，适合尚未提供 API 的遗留系统或第三方页面。

### Agent 会不会自己乱开工单？

> 工具描述明确标注了写操作，要求先确认订单并向用户说明。本 Demo 的 Prompt 也显式授权了创建工单。生产环境还可以增加人工审批和更严格的策略控制。

### 这是 AgentCore Memory 吗？

> 不是。中国区当前没有 AgentCore Memory，本项目用 DynamoDB 实现了 MemoryLite，并明确作为可替换的补位层。

### 数据是否会发送到外部？

> 模型推理会调用 DeepSeek 官方 API，这是本方案唯一外部服务依赖。生产部署前应根据企业的数据分类和合规要求决定发送范围、脱敏策略及模型选择。

---

## 十、演示前需要处理或说明的事项

当前部署适合作为 Demo，但正式向客户演示前建议处理：

1. 当前 Runtime 镜像的 ECR 扫描存在一个已有修复的 Critical Perl 漏洞 `CVE-2026-13221`，应更新基础镜像并重新构建。
2. 当前固定 `mcp==1.23.0`，建议兼容性验证后升级到修复会话绑定漏洞的 `1.27.2` 或更高兼容版本。
3. Runtime CloudWatch 日志组尚未设置保留期，建议设置 14～30 天。
4. `stable` 和 `DEFAULT` 当前都指向 v17。发布新版本时，建议让 `stable` 暂时固定在最后一个已验证版本。
5. 物流页公开、业务工具未按 actor 做订单数据隔离、自建 IdP 没有 MFA 和登录锁定，均属于 Demo 取舍，不应描述为生产完成态。

---

## 十一、结束总结话术

> DeepSeek 给了 Agent 推理能力，但仅有模型并不能安全接入企业业务。
>
> AgentCore 在这里提供了运行环境、身份和凭证管理、企业工具网关、浏览器与代码沙箱、流式和异步执行、版本灰度以及端到端可观测性。
>
> 因此，这不是一个“会回答问题的聊天机器人”，而是一个可以被管理、审计并逐步生产化的售后业务 Agent。
