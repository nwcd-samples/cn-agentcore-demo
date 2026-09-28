"""自建的"承运商物流查询"网页。

这个页面是 Browser 能力的演示靶子。它刻意做成**需要填表提交**的形式,
而不是 GET /track?no=xxx 就能拿到结果 —— 否则一个 HTTP 请求就够了,
根本用不上浏览器,Browser 这个能力也就没什么可演示的。

流程:
  GET  /        渲染查询表单(一个 input + 一个 submit)
  POST /track   校验运单号,渲染物流时间线
  GET  /health  给部署脚本做连通性检查

数据来自业务表里的 SHIPMENT# 条目(由 scripts/seed_business.py 写入),
所以页面上的物流状态和订单的 promised_at 是一致的 ——
Agent 从订单看到"超期 77 小时",从这个页面能看到"卡在中转中心 3 天",
两边对得上,演示才可信。

零第三方依赖:只用标准库 + Lambda 自带 boto3。HTML 手写,不引模板引擎。

【安全说明:这个端点是刻意公开的】
  它扮演的是"第三方承运商的公开查询页",本来就该匿名可访问 ——
  给它加鉴权反而让演示失真(真实场景里 Agent 也是匿名访问快递官网)。
  为此做了这些约束:
    * 数据全是 seed 脚本写入的合成数据,没有任何真实个人信息
    * 运单号必须匹配严格正则,查不到就是查不到,不返回任何提示性信息
    * HTTP API 层配了限流(见 infra/30-logistics-web.yaml)
    * 所有回显到 HTML 的值都经过 html.escape,防 XSS
  如果要改成非公开,给 HTTP API 挂个 Lambda authorizer 即可,
  但那样 Browser 演示就需要额外处理登录态。
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BUSINESS_TABLE = os.environ["BUSINESS_TABLE"]

_ddb = boto3.client(
    "dynamodb", config=Config(retries={"max_attempts": 3, "mode": "standard"})
)

# 运单号:2-4 位承运商字母前缀 + 8-20 位数字
_SHIPMENT_NO_RE = re.compile(r"^[A-Z]{2,4}[0-9]{8,20}$")

# 北京时间,页面上显示给人看
_CST = timezone(timedelta(hours=8))

_PAGE_TITLE = "宁夏速运 · 物流查询"

_STYLE = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f4f6f8; color: #1a1a1a; }
  header { background: #0b5fa5; color: #fff; padding: 18px 24px; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  main { max-width: 720px; margin: 28px auto; padding: 0 16px; }
  .card { background: #fff; border-radius: 8px; padding: 24px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  label { display: block; margin-bottom: 8px; font-size: 14px; color: #555; }
  input[type=text] { width: 100%; padding: 12px; font-size: 16px;
                     border: 1px solid #ccd3da; border-radius: 6px; }
  button { margin-top: 16px; padding: 12px 28px; font-size: 16px; border: 0;
           border-radius: 6px; background: #0b5fa5; color: #fff; cursor: pointer; }
  button:hover { background: #094b83; }
  .meta { display: grid; grid-template-columns: 96px 1fr; gap: 8px 16px;
          font-size: 14px; margin: 0 0 20px; }
  .meta dt { color: #777; }
  .meta dd { margin: 0; font-weight: 500; }
  .status { display: inline-block; padding: 4px 12px; border-radius: 12px;
            font-size: 13px; font-weight: 600; }
  .status.normal { background: #e3f3e6; color: #1d7a33; }
  .status.warn { background: #fdf0dd; color: #a35c00; }
  .status.alert { background: #fdE3E3; color: #a32020; }
  ol.timeline { list-style: none; margin: 0; padding: 0; border-left: 2px solid #dde3e8; }
  ol.timeline li { position: relative; padding: 0 0 20px 22px; }
  ol.timeline li::before { content: ""; position: absolute; left: -7px; top: 4px;
        width: 12px; height: 12px; border-radius: 50%; background: #c3ccd4; }
  ol.timeline li:first-child::before { background: #0b5fa5; }
  .tl-time { font-size: 13px; color: #888; }
  .tl-note { font-size: 15px; margin-top: 2px; }
  .tl-loc { font-size: 13px; color: #666; margin-top: 2px; }
  .empty { padding: 32px 0; text-align: center; color: #888; }
  .back { display: inline-block; margin-top: 20px; color: #0b5fa5;
          text-decoration: none; font-size: 14px; }
  footer { text-align: center; color: #999; font-size: 12px; padding: 24px 0; }
"""


def _layout(body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_PAGE_TITLE}</title>
<style>{_STYLE}</style>
</head>
<body>
<header><h1>宁夏速运</h1></header>
<main>{body}</main>
<footer>本页为 AgentCore 演示用的合成数据,不对应任何真实运单。</footer>
</body>
</html>"""


def _html_response(body: str, status: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            # 页面只有内联样式,没有外部脚本,直接把 script 全禁掉
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
        "body": _layout(body),
    }


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def render_form(error: str = "", prefill: str = "") -> str:
    """查询表单。

    元素 id 刻意固定:Agent 端的 Browser 工具靠 #shipment-no 和 #query-btn
    定位,改名会让工具失效(有测试断言两边一致)。
    """
    alert = f'<p class="status alert">{html.escape(error)}</p>' if error else ""
    return f"""
<div class="card">
  <h2 style="margin-top:0;font-size:18px;">运单查询</h2>
  {alert}
  <form method="POST" action="/track">
    <label for="shipment-no">请输入运单号</label>
    <input type="text" id="shipment-no" name="shipment_no" autocomplete="off"
           placeholder="例如 SF7758291046" value="{html.escape(prefill)}">
    <button type="submit" id="query-btn">查询</button>
  </form>
</div>
"""


def _status_class(status: str) -> str:
    return {
        "delivered": "normal",
        "in_transit": "normal",
        "delayed": "warn",
        "exception": "alert",
    }.get(status, "normal")


_STATUS_LABEL = {
    "delivered": "已签收",
    "in_transit": "运输中",
    "delayed": "延误",
    "exception": "异常",
    "pending": "待揽收",
}


def render_result(shipment: dict[str, Any]) -> str:
    """物流时间线。

    这里刻意把"异常原因"和"停留时长"写成人类可读的文字而不是结构化字段 ——
    Agent 必须真的读页面内容才能拿到,这才是 Browser 能力的演示点。
    """
    status = shipment.get("status", "in_transit")
    events = shipment.get("events", [])

    rows = []
    # 最新的在最上面
    for event in sorted(events, key=lambda e: e["at"], reverse=True):
        when = datetime.fromtimestamp(event["at"], _CST).strftime("%Y-%m-%d %H:%M")
        rows.append(
            f"""    <li>
      <div class="tl-time">{html.escape(when)}</div>
      <div class="tl-note">{html.escape(event.get("note", ""))}</div>
      <div class="tl-loc">{html.escape(event.get("location", ""))}</div>
    </li>"""
        )
    timeline = (
        "\n".join(rows) if rows else '    <li><div class="tl-note">暂无物流轨迹</div></li>'
    )

    stalled = ""
    if shipment.get("stalled_hours"):
        hours = shipment["stalled_hours"]
        stalled = (
            f'<p class="status alert">包裹已在中转环节停留 {hours} 小时,'
            f"疑似中转异常,建议联系客服处理</p>"
        )

    return f"""
<div class="card">
  <h2 style="margin-top:0;font-size:18px;">运单 {html.escape(shipment["shipment_no"])}</h2>
  <p><span class="status {_status_class(status)}">
     {html.escape(_STATUS_LABEL.get(status, status))}</span></p>
  {stalled}
  <dl class="meta">
    <dt>承运商</dt><dd>{html.escape(shipment.get("carrier", "-"))}</dd>
    <dt>关联订单</dt><dd>{html.escape(shipment.get("order_id", "-"))}</dd>
    <dt>当前位置</dt><dd>{html.escape(shipment.get("current_location", "-"))}</dd>
  </dl>
  <h3 style="font-size:15px;color:#555;">物流轨迹</h3>
  <ol class="timeline">
{timeline}
  </ol>
  <a class="back" href="/">&larr; 查询另一个运单</a>
</div>
"""


def render_not_found(shipment_no: str) -> str:
    return f"""
<div class="card">
  <h2 style="margin-top:0;font-size:18px;">查询无结果</h2>
  <p class="empty">没有查询到运单 {html.escape(shipment_no)} 的信息。<br>
     请核对运单号后重试。</p>
  <a class="back" href="/">&larr; 返回</a>
</div>
"""


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


def _from_attr(value: dict[str, Any]) -> Any:
    if "S" in value:
        return value["S"]
    if "N" in value:
        return int(value["N"])
    if "L" in value:
        return [_from_attr(v) for v in value["L"]]
    if "M" in value:
        return {k: _from_attr(v) for k, v in value["M"].items()}
    if "BOOL" in value:
        return value["BOOL"]
    return None


def load_shipment(shipment_no: str) -> dict[str, Any] | None:
    resp = _ddb.get_item(
        TableName=BUSINESS_TABLE,
        Key={"PK": {"S": f"SHIPMENT#{shipment_no}"}, "SK": {"S": "META"}},
    )
    item = resp.get("Item")
    if not item:
        return None
    data = {k: _from_attr(v) for k, v in item.items()}
    for key in ("PK", "SK", "GSI1PK", "GSI1SK"):
        data.pop(key, None)
    return data


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------


def _parse_form_body(event: dict[str, Any]) -> dict[str, str]:
    import base64
    import urllib.parse

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def normalize_shipment_no(raw: str) -> str:
    """去空格并转大写。用户/浏览器填进来的值总是脏的。"""
    return re.sub(r"\s+", "", raw or "").upper()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def handle_index() -> dict[str, Any]:
    return _html_response(render_form())


def handle_track(event: dict[str, Any]) -> dict[str, Any]:
    form = _parse_form_body(event)
    shipment_no = normalize_shipment_no(form.get("shipment_no", ""))

    if not shipment_no:
        return _html_response(render_form(error="请填写运单号"), status=400)
    if not _SHIPMENT_NO_RE.match(shipment_no):
        return _html_response(
            render_form(error="运单号格式不正确,应为字母前缀加数字", prefill=shipment_no),
            status=400,
        )

    try:
        shipment = load_shipment(shipment_no)
    except ClientError:
        LOG.exception("查询运单失败")
        return _html_response(
            render_form(error="系统繁忙,请稍后重试", prefill=shipment_no), status=503
        )

    if shipment is None:
        # 查不到就是查不到,不透露"这个号段不存在"之类的信息
        return _html_response(render_not_found(shipment_no), status=404)
    return _html_response(render_result(shipment))


def handle_health() -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": '{"status":"ok","service":"logistics-web"}',
    }


ROUTES = {
    ("GET", "/"): lambda event: handle_index(),
    ("GET", "/index.html"): lambda event: handle_index(),
    ("POST", "/track"): handle_track,
    ("GET", "/health"): lambda event: handle_health(),
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    http = (event.get("requestContext") or {}).get("http") or {}
    method = str(http.get("method", "GET")).upper()
    path = http.get("path", event.get("rawPath", "/"))

    handler = ROUTES.get((method, path))
    if handler is None:
        return _html_response(
            render_form(error=f"页面不存在:{method} {path}"), status=404
        )

    started = time.time()
    try:
        response = handler(event)
    except Exception:  # noqa: BLE001
        LOG.exception("处理 %s %s 失败", method, path)
        return _html_response(render_form(error="服务异常,请稍后重试"), status=500)
    LOG.info("%s %s -> %d (%.0fms)", method, path, response["statusCode"],
             (time.time() - started) * 1000)
    return response
