"""大模型封装：OpenAI 兼容接口。

配置从 `backend/.env` 读取（示例见同目录 .env.example）：
- OPENAI_BASE_URL：接口地址（默认 https://api.openai.com/v1）
- OPENAI_API_KEY：API Key（必填）
- OPENAI_MODEL：模型名（默认 gpt-4o-mini）

教学要点：`load_dotenv()` 默认从**当前工作目录**找 .env，
后端以 backend/ 为启动目录运行（uvicorn main:app），
因此实际加载的就是 backend/.env。
"""
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

# 加载 backend/.env（load_dotenv 默认读当前工作目录的 .env，无副作用可重复调用）
load_dotenv()

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """获取（懒加载）AsyncOpenAI 客户端。"""
    global _client
    if _client is None:
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY，请在 backend/.env 中填写")
        _client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    return _client


def get_model() -> str:
    """当前使用的模型名。"""
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


async def chat(messages: list, tools: list | None = None, tool_choice: str | None = None,
               model: str | None = None):
    """调用 Chat Completions。

    维度一（可观测性）：包一层 llm span —— 捕获 model / prompt / completion
    tokens（resp.usage）并按价目表估算成本；任务上下文中执行时自动落入
    task_events（Flight Recorder 成本卡数据源）。非任务上下文（如单测）为零副作用。

    :param messages: OpenAI 格式的消息列表
    :param tools: OpenAI 格式的工具 Schema 列表（Function Calling）
    :param tool_choice: 可选 "auto" / "none"
    :param model: 覆盖默认模型（OPENAI_MODEL）—— 如 Judge 用更强的评审模型，
        与被测系统解耦（避免「自己评自己」的同源偏差）
    :return: ChatCompletion 响应对象（含 content / tool_calls）
    """
    from observability import pricing, tracing

    client = get_client()
    model = model or get_model()
    kwargs = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    span = tracing.start_span(
        "llm", model, model=model,
        detail={"messages": len(messages), "with_tools": bool(tools)},
    )
    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        tracing.finish_span(span, status="error", error_class=type(exc).__name__)
        raise
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None)
    tokens_out = getattr(usage, "completion_tokens", None)
    tracing.finish_span(
        span,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=pricing.estimate_cost_usd(model, tokens_in, tokens_out),
    )
    return resp
