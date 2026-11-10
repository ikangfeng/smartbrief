"""Span 追踪（维度一）：contextvars 上下文传播 + 进程内事件总线。

追踪模型（对齐 OpenTelemetry 的简化版）：
- 每个 span = 一条 task_events 记录：node（图节点）/ llm（模型调用）/ tool（工具调用）
- contextvars 传播：run_task 入口处 `bind_task(task_id)`，
  之后任意深处（agent → llm.chat / mcp.call_tool）都能拿到当前 task_id 与父 span，
  无需改动各函数签名 —— 这是"埋点不侵入业务"的关键手段。
- 事件总线（EventBus）：span 开始/结束时向订阅者广播，
  SSE 端点（GET /tasks/{id}/events/stream）据此实时推送前端 Flight Recorder。

容错红线：观测代码绝不能影响业务主流程 ——
persist / publish 全部 try/except 吞掉，宁可丢一条 span 也不能让任务失败。
"""
import asyncio
import contextvars
import hashlib
import json
import logging
import time
from uuid import uuid4

from db import events_repository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 上下文传播

_current_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_task_id", default=None
)
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_parent_span_id", default=None
)


def bind_task(task_id: str) -> contextvars.Token:
    """绑定当前异步上下文的任务 id（run_task 入口调用一次）。

    :return: contextvars.Token（任务结束可 reset；不 reset 也无碍，每次 run_task 都会重绑）
    """
    return _current_task_id.set(task_id)


def unbind_task() -> None:
    """解除当前异步上下文的任务绑定（维度三：评测 Judge 等任务外调用前使用）。

    `run_task` 结束后 contextvar 仍保留任务 id（未 reset），若不解除，
    Judge 的 LLM 调用会被记入被评测任务的 task_events，导致成本卡虚高。
    """
    _current_task_id.set(None)
    _current_span_id.set(None)


def get_current_task_id() -> str | None:
    return _current_task_id.get()


# ---------------------------------------------------------------- 事件总线（SSE 实时推送）


class EventBus:
    """进程内发布/订阅总线：task_id → 订阅队列集合。

    每个 SSE 连接订阅一个 asyncio.Queue；span 事件向该任务所有队列广播。
    队列满（慢消费者）时丢弃最旧事件而非阻塞生产者 —— 观测流量绝不能反压业务。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, task_id: str, maxsize: int = 500) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(task_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(task_id, None)

    def publish(self, task_id: str, event: dict) -> None:
        """向某任务的全部订阅者广播事件（非阻塞；队列满则丢最旧的一条）。"""
        for queue in list(self._subscribers.get(task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()          # 丢最旧，腾位置给最新
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


event_bus = EventBus()   # 进程级单例


# ---------------------------------------------------------------- Span 生命周期


def args_hash(arguments: dict | None) -> str:
    """工具参数指纹：sha256 前 12 位（可区分不同调用，又不把明文参数落库）。"""
    try:
        raw = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(arguments)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def start_span(span_type: str, name: str, **fields) -> dict:
    """开启一个 span：分配 id、记录开始时间、继承父 span、广播 start 事件。

    未绑定任务（如单测直接调 llm.chat）时返回 {"_noop": True} 占位，
    finish_span 识别后直接丢弃 —— 保证非任务上下文调用零副作用。

    :param span_type: node | llm | tool
    :param name: 展示名（节点名 / 模型名 / 工具名）
    :param fields: 预置字段（如 model / tool_name / args_hash）
    """
    task_id = _current_task_id.get()
    if not task_id:
        return {"_noop": True}
    span = {
        "task_id": task_id,
        "span_id": uuid4().hex[:8],
        "parent_span_id": _current_span_id.get(),
        "span_type": span_type,
        "name": name,
        "status": "ok",
        "start_ts": time.time(),
        **fields,
    }
    _publish_safe(task_id, {"kind": "span_start", "span": _public_span(span)})
    return span


def set_current_span(span: dict) -> contextvars.Token:
    """把 span 设为当前父 span（node span 用：其内部的 llm/tool span 都挂到它下面）。"""
    return _current_span_id.set(span.get("span_id"))


def reset_current_span(token: contextvars.Token) -> None:
    _current_span_id.reset(token)


def finish_span(span: dict, status: str | None = None, **fields) -> dict | None:
    """结束 span：补齐结束时间/耗时 → 落库 → 广播 end 事件。

    :param span: start_span 的返回值（noop 占位时直接返回 None）
    :param status: ok | error（不传则沿用 span 里的 status 字段或默认 ok）
    :param fields: 结束时才有的字段（tokens_in/out、cost_usd、error_class、retry_count…）
    """
    if span.get("_noop"):
        return None
    span.update(fields)
    if status:
        span["status"] = status
    span["end_ts"] = time.time()
    span["duration_ms"] = round((span["end_ts"] - span["start_ts"]) * 1000, 1)
    try:
        events_repository.insert_event(span)          # 落库（Flight Recorder 数据源）
    except Exception as exc:  # 观测失败绝不能影响业务
        logger.warning("span 落库失败（忽略）：%s", exc)
    _publish_safe(span["task_id"], {"kind": "span_end", "span": _public_span(span)})
    return span


def _public_span(span: dict) -> dict:
    """广播给前端的 span 快照（剔除内部字段）。"""
    return {k: v for k, v in span.items() if not k.startswith("_")}


def _publish_safe(task_id: str, event: dict) -> None:
    """广播容错：没有事件循环（如同步测试上下文）时静默跳过。"""
    try:
        event_bus.publish(task_id, event)
    except Exception:  # pragma: no cover - 防御性兜底
        pass
