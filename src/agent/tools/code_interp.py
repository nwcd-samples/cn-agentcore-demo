"""AgentCore Code Interpreter 工具。

沙箱里跑代码,主要用途是算钱 —— 赔付金额这类计算交给模型心算必然出错,
而 get_refund_policy 刻意只返回规则不返回金额,就是为了逼它走这条路。

三个工具:
  run_python        在沙箱里执行 Python,返回 stdout/stderr/exit_code
  publish_file      把沙箱产出的文件传到 S3,返回预签名下载链接
  install_packages  按白名单装 Python 包

会话生命周期:
  沙箱**惰性启动** —— 模型不调工具就不开会话,省钱也省冷启动时间。
  会话通过 ExitStack 注册清理,保证一轮对话结束一定关掉。

已对着 boto3 服务模型核对的响应结构:
  InvokeCodeInterpreter -> stream(事件流)
    result.structuredContent  {stdout, stderr, exitCode, executionTime, taskStatus}
    result.content[]          {type: text|image|resource|resource_link, text, ...}
    result.isError            bool
    result.<xxxException>     {message}   <- 异常也在流里,不是抛出来的
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import re
import time
from typing import Any

from strands import tool

from agent import obs
from agent.config import Settings

LOG = logging.getLogger(__name__)

# 输出截断阈值。沙箱里一个 print 循环就能刷出几 MB,
# 原样塞回模型上下文既烧 token 又可能超限。
_MAX_OUTPUT_CHARS = 8000
_MAX_CODE_CHARS = 20000

# 允许模型安装的包。SDK 自己会校验包名格式,但那只防注入,
# 不防"模型从 PyPI 拉一个任意包进沙箱"。沙箱有出网能力,
# 所以这里再加一层语义白名单,把供应链面收住。
_ALLOWED_PACKAGES = {
    "pandas",
    "numpy",
    "matplotlib",
    "scipy",
    "openpyxl",
    "python-dateutil",
    "tabulate",
}

# 沙箱内文件名:相对路径,不许穿越
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,120}$")

# 传到 S3 时按 MIME 类型给个合理的 Content-Type
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}


class SandboxError(RuntimeError):
    """沙箱调用失败。"""


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[输出过长,已截断,原始长度 {len(text)} 字符]"


def parse_invoke_result(response: dict[str, Any]) -> dict[str, Any]:
    """把 InvokeCodeInterpreter 的事件流摊平成一个好读的字典。

    异常是作为流里的一个字段回来的(不是抛出来),所以必须显式检查,
    否则会把一次失败当成"空输出"静默咽掉。
    """
    out: dict[str, Any] = {
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "execution_time_ms": None,
        "is_error": False,
        "texts": [],
        "resources": [],
    }

    exception_keys = (
        "accessDeniedException",
        "conflictException",
        "internalServerException",
        "resourceNotFoundException",
        "serviceQuotaExceededException",
        "throttlingException",
        "validationException",
    )

    for event in response.get("stream") or []:
        for key in exception_keys:
            if key in event:
                message = (event[key] or {}).get("message", "(没有给出原因)")
                raise SandboxError(f"{key}: {message}")

        result = event.get("result")
        if not result:
            continue

        if result.get("isError"):
            out["is_error"] = True

        structured = result.get("structuredContent") or {}
        if "stdout" in structured:
            out["stdout"] += structured["stdout"] or ""
        if "stderr" in structured:
            out["stderr"] += structured["stderr"] or ""
        if structured.get("exitCode") is not None:
            out["exit_code"] = structured["exitCode"]
        if structured.get("executionTime") is not None:
            # API 给的是秒(double),换成毫秒整数更好读
            out["execution_time_ms"] = int(float(structured["executionTime"]) * 1000)

        for item in result.get("content") or []:
            item_type = item.get("type")
            if item_type == "text" and item.get("text"):
                out["texts"].append(item["text"])
            elif item_type in ("resource", "resource_link", "image"):
                out["resources"].append(
                    {
                        "type": item_type,
                        "name": item.get("name"),
                        "uri": item.get("uri"),
                        "mime_type": item.get("mimeType"),
                    }
                )

    out["stdout"] = _truncate(out["stdout"])
    out["stderr"] = _truncate(out["stderr"])
    return out


def format_execution(parsed: dict[str, Any]) -> str:
    """转成给模型看的紧凑文本。

    刻意不返回 JSON:模型对"STDOUT: ..." 这种带标签的纯文本理解得更稳,
    也省掉一层转义带来的 token 开销。
    """
    lines: list[str] = []
    if parsed["stdout"]:
        lines.append(f"STDOUT:\n{parsed['stdout']}")
    if parsed["stderr"]:
        lines.append(f"STDERR:\n{parsed['stderr']}")
    for text in parsed["texts"]:
        lines.append(text)

    exit_code = parsed["exit_code"]
    if parsed["is_error"] or (exit_code not in (None, 0)):
        lines.append(f"执行失败,exit_code={exit_code}。请修正代码后重试。")
    elif not lines:
        lines.append("代码执行成功,但没有任何输出。记得用 print() 把结果打出来。")

    if parsed["resources"]:
        names = [r.get("name") or r.get("uri") for r in parsed["resources"]]
        lines.append(f"沙箱里产生的文件:{names}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 惰性会话
# ---------------------------------------------------------------------------


class LazySandbox:
    """按需启动 Code Interpreter 会话。

    模型不一定会用到沙箱(比如只是查个订单状态),所以不在组装阶段就开会话 ——
    那会白付一次冷启动的时间和费用。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    @property
    def started(self) -> bool:
        return self._client is not None

    def client(self) -> Any:
        if self._client is None:
            from bedrock_agentcore.tools import CodeInterpreter

            LOG.info(
                "启动 Code Interpreter 会话,identifier=%s",
                self._settings.code_interpreter_id,
            )
            # 冷启动是这条链路最慢的一环,单独埋点方便看耗时分布
            with obs.span(
                "sandbox.start", identifier=self._settings.code_interpreter_id
            ):
                client = CodeInterpreter(self._settings.region)
                client.start(identifier=self._settings.code_interpreter_id)
            self._client = client
        return self._client

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.stop()
            LOG.info("Code Interpreter 会话已关闭")
        except Exception:
            # 清理失败不该影响对话结果,会话本身也有超时兜底
            LOG.exception("关闭 Code Interpreter 会话失败(会话会自行超时)")
        finally:
            self._client = None


# ---------------------------------------------------------------------------
# S3 发布
# ---------------------------------------------------------------------------


def _validate_sandbox_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        raise ValueError("路径不能为空")
    if path.startswith("/"):
        raise ValueError(f"只接受相对路径,收到绝对路径 {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"路径不允许包含 ..,收到 {path!r}")
    if not _SAFE_PATH_RE.match(path):
        raise ValueError(f"路径含不允许的字符:{path!r}")
    return path


def _content_type_for(path: str) -> str:
    _, _, ext = path.rpartition(".")
    return _CONTENT_TYPES.get(f".{ext.lower()}", "application/octet-stream")


# ---------------------------------------------------------------------------
# 工具构造
# ---------------------------------------------------------------------------


def build_code_tools(
    settings: Settings,
    stack: contextlib.ExitStack,
    *,
    session_id: str = "",
    actor_id: str = "",
) -> list:
    """构造 Code Interpreter 相关工具。

    Args:
        stack: 调用方的 ExitStack,用来注册沙箱会话的清理。
        session_id / actor_id: 只用于给 S3 对象分区,不暴露成工具参数 ——
            否则模型可以指定任意路径覆盖别人的产物。
    """
    sandbox = LazySandbox(settings)
    stack.callback(sandbox.close)

    # S3 key 前缀由闭包固定。Runtime 角色的 S3 权限也只开到 outputs/*,
    # 两层一起保证模型写不出这个范围。
    key_prefix = posixpath.join(
        "outputs",
        actor_id or "anonymous",
        session_id or "no-session",
    )

    @tool
    def run_python(code: str, reset: bool = False) -> str:
        """在隔离沙箱里执行 Python 代码,返回 stdout 和 stderr。

        用它做一切计算 —— 赔付金额、统计、日期差。不要自己心算。

        要点:
        - 结果必须用 print() 输出,否则看不到返回值。
        - 变量在多次调用之间保留,可以分步来。
        - 金额一律用整数分计算,避免浮点误差。
        - 画图:用 matplotlib 存成 PNG(如 plt.savefig('chart.png')),
          再调 publish_file 拿下载链接。
        - 沙箱内已有 pandas / numpy / matplotlib;缺别的用 install_packages。

        Args:
            code: 要执行的 Python 代码。
            reset: 传 True 会清空之前所有变量,从干净环境开始。
        """
        if not code or not code.strip():
            return "代码不能为空。"
        if len(code) > _MAX_CODE_CHARS:
            return f"代码过长({len(code)} 字符),上限 {_MAX_CODE_CHARS}。请拆成多次执行。"

        started = time.monotonic()
        try:
            response = sandbox.client().execute_code(
                code=code, language="python", clear_context=reset
            )
            parsed = parse_invoke_result(response)
        except SandboxError as exc:
            LOG.warning("沙箱执行被拒:%s", exc)
            return f"沙箱执行失败:{exc}"
        except Exception as exc:  # noqa: BLE001
            LOG.exception("沙箱执行异常")
            return f"沙箱不可用:{type(exc).__name__}。请告知用户稍后重试。"

        LOG.info(
            "run_python 完成 exit=%s 耗时=%dms 本地耗时=%dms",
            parsed["exit_code"],
            parsed["execution_time_ms"] or 0,
            int((time.monotonic() - started) * 1000),
        )
        return format_execution(parsed)

    @tool
    def publish_file(sandbox_path: str) -> str:
        """把沙箱里的文件传到 S3,返回一个 1 小时有效的下载链接。

        典型用法:先用 run_python 把图表存成 chart.png,再调这个工具拿链接给用户。

        Args:
            sandbox_path: 沙箱内的相对路径,例如 chart.png 或 out/report.csv。
        """
        if not settings.artifact_bucket:
            return "没有配置产物存储桶(ARTIFACT_BUCKET),无法生成下载链接。"

        try:
            path = _validate_sandbox_path(sandbox_path)
        except ValueError as exc:
            return f"路径不合法:{exc}"

        try:
            content = sandbox.client().download_file(path)
        except FileNotFoundError:
            return f"沙箱里没有 {path}。先确认代码真的把文件写出来了。"
        except SandboxError as exc:
            return f"读取沙箱文件失败:{exc}"
        except Exception as exc:  # noqa: BLE001
            LOG.exception("读取沙箱文件失败")
            return f"读取沙箱文件失败:{type(exc).__name__}"

        payload = content.encode("utf-8") if isinstance(content, str) else content
        key = posixpath.join(key_prefix, posixpath.basename(path))

        try:
            import boto3

            s3 = boto3.client("s3", region_name=settings.region)
            s3.put_object(
                Bucket=settings.artifact_bucket,
                Key=key,
                Body=payload,
                ContentType=_content_type_for(path),
                ServerSideEncryption="AES256",
            )
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.artifact_bucket, "Key": key},
                ExpiresIn=3600,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("上传产物失败")
            return f"上传失败:{type(exc).__name__}。请告知用户稍后重试。"

        LOG.info("已发布产物 s3://%s/%s (%d 字节)", settings.artifact_bucket, key, len(payload))
        return (
            f"已上传 {path}({len(payload)} 字节)。"
            f"下载链接(1 小时内有效):\n{url}"
        )

    @tool
    def install_packages(packages: list[str]) -> str:
        """在沙箱里安装 Python 包。

        只允许安装以下包:pandas、numpy、matplotlib、scipy、openpyxl、
        python-dateutil、tabulate。其他包会被拒绝。

        多数情况下不需要调用它 —— pandas / numpy / matplotlib 已经预装。

        Args:
            packages: 包名列表,例如 ["scipy"]。
        """
        if not packages:
            return "包名列表不能为空。"

        rejected = [p for p in packages if p.split("==")[0].split(">")[0].strip()
                    not in _ALLOWED_PACKAGES]
        if rejected:
            return (
                f"这些包不在白名单里,已拒绝:{rejected}。"
                f"可安装的只有:{sorted(_ALLOWED_PACKAGES)}"
            )

        try:
            response = sandbox.client().install_packages(packages)
            parsed = parse_invoke_result(response)
        except SandboxError as exc:
            return f"安装失败:{exc}"
        except Exception as exc:  # noqa: BLE001
            LOG.exception("安装包失败")
            return f"安装失败:{type(exc).__name__}"

        if parsed["is_error"] or parsed["exit_code"] not in (None, 0):
            return f"安装失败:\n{_truncate(parsed['stderr'] or parsed['stdout'], 2000)}"
        return f"已安装 {packages}。"

    return [run_python, publish_file, install_packages]


__all__ = [
    "build_code_tools",
    "parse_invoke_result",
    "format_execution",
    "LazySandbox",
    "SandboxError",
]
