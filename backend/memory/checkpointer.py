"""短期记忆（第 26 节）：LangGraph Checkpointer 封装（backend/checkpoints.db）。

职责边界（与第 24 节业务库分工，见 CLAUDE.md）：
- `backend/data.db`        由 task_repository 显式写入**任务业务数据**；
- `backend/checkpoints.db` 由 LangGraph 框架在每次图节点执行后**自动写入图运行
  时状态快照**，task_id 同时作为 checkpoint 的 thread_id，二者一一对应。

注意（langgraph-checkpoint-sqlite >= 3 的 API）：`from_conn_string()` 返回的不是
Saver 实例而是连接生命周期管理器 —— 因此：
- 执行工作流（要跨节点持久写快照）必须用 `async with open_checkpointer() as (cp, kind):`
  把 compile + invoke 包在连接生命周期内（AsyncSqliteSaver）；
- 轻量只读查询（has_checkpoint / list_snapshots）走同步 `SqliteSaver` 短暂连接。

降级（保证缺依赖环境也能跑）：
1. AsyncSqliteSaver（langgraph-checkpoint-sqlite，跨进程持久化 —— 后端重启后仍可续跑）
2. MemorySaver（langgraph 自带，仅进程内；以单例复用，同一进程内仍可 resume）
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
CHECKPOINT_DB = BACKEND_DIR / "checkpoints.db"     # 默认：backend/checkpoints.db

# 降级路径的进程内 MemorySaver 单例（供 open_checkpointer 与只读查询共用，
# 保证同一进程内"写快照 → 检测/恢复"使用同一实例）
_memory_saver: Any = None


def _get_memory_saver() -> Any:
    global _memory_saver
    if _memory_saver is None:
        from langgraph.checkpoint.memory import MemorySaver

        _memory_saver = MemorySaver()
    return _memory_saver


@asynccontextmanager
async def open_checkpointer(
    db_path: Optional[str] = None,
) -> AsyncIterator[Tuple[Any, str]]:
    """在连接生命周期内打开持久化 Checkpointer。

    新版 langgraph-checkpoint-sqlite 要求 `async with from_conn_string(...)`
    进入连接生命周期：期间图每次节点执行后自动向 SQLite 落快照，退出即关闭连接。
    sqlite 依赖/文件不可用则降级为进程内 MemorySaver（kind="memory"）。

    :param db_path: 覆盖默认 backend/checkpoints.db（测试隔离用）
    :yield: (checkpointer, kind)，kind ∈ {"sqlite", "memory"}
    """
    path = Path(db_path or CHECKPOINT_DB)
    cp: Any = None
    kind = "memory"
    opener: Any = None
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        # 显式打开连接（真正连库发生在 opener.__aenter__，返回 saver 实例）；
        # 失败即降级。注意：降级分支绝不能包住 yield —— body 抛出的异常若被
        # except 吞掉并再次 yield，asynccontextmanager 会报
        # "generator didn't stop after athrow()"。
        opener = AsyncSqliteSaver.from_conn_string(str(path))
        cp = await opener.__aenter__()
        kind = "sqlite"
    except Exception as exc:  # pragma: no cover - 依赖缺失/文件异常降级路径
        logger.warning("SQLite Checkpointer 不可用（%s），降级为进程内 MemorySaver", exc)
        cp = _get_memory_saver()
        kind = "memory"
    try:
        yield cp, kind
    finally:
        # saver 实例无 __aexit__，连接生命周期归属 opener（async generator）
        if opener is not None:
            await opener.__aexit__(None, None, None)


def _latest_on(cp: Any, task_id: str):
    """取某任务（thread_id）的最新快照（async/同步 Saver 方法兼容）。"""
    config = {"configurable": {"thread_id": task_id}}
    aget = getattr(cp, "aget_tuple", None)
    if aget is not None:
        return aget(config)          # AsyncSqliteSaver / 新版 MemorySaver → await
    return cp.get_tuple(config)      # 旧版同步 MemorySaver


def has_checkpoint(task_id: str, db_path: Optional[str] = None) -> bool:
    """（同步只读）指定任务（thread_id）是否存在可续跑的快照。"""
    path = Path(db_path or CHECKPOINT_DB)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(path)) as cp:
            return cp.get_tuple({"configurable": {"thread_id": task_id}}) is not None
    except Exception as exc:  # pragma: no cover - sqlite 打开失败降级 memory
        logger.warning("SQLite 查询任务 %s 快照失败（%s），退回进程内 MemorySaver", task_id, exc)
        return _get_memory_saver().get_tuple(
            {"configurable": {"thread_id": task_id}}
        ) is not None


async def async_has_checkpoint(cp: Any, task_id: str) -> bool:
    """在已打开的 Checkpointer（连接内）上查询是否有可续跑快照。"""
    try:
        latest = await _latest_on(cp, task_id)
        return latest is not None
    except Exception as exc:  # pragma: no cover - 依赖异常路径
        logger.warning("查询任务 %s 快照失败：%s", task_id, exc)
        return False


def list_snapshots(task_id: str, db_path: Optional[str] = None) -> List[Dict]:
    """（同步只读）列出某任务的 checkpoint 快照（供前端展示断点状态）。"""
    path = Path(db_path or CHECKPOINT_DB)
    snapshots: List[Dict] = []
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(path)) as cp:
            configs = cp.list({"configurable": {"thread_id": task_id}})
            # CheckpointTuple 形如 (config, checkpoint, metadata, parent_config, pending_writes)，
            # 部分实现字段数不同，故用 *rest 兼容展开
            for config, _checkpoint, meta, *_rest in configs:
                cfg = config.get("configurable", {})
                snapshots.append({
                    "checkpoint_id": cfg.get("checkpoint_id"),
                    "created_at": (meta or {}).get("created_at"),
                    "step": (meta or {}).get("step", 0),
                })
    except Exception as exc:  # pragma: no cover - sqlite 打开失败降级 memory
        logger.warning("SQLite 读取任务 %s 快照失败（%s），退回进程内 MemorySaver", task_id, exc)
        try:
            cp = _get_memory_saver()
            for config, _checkpoint, meta, *_rest in cp.list(
                {"configurable": {"thread_id": task_id}}
            ):
                cfg = config.get("configurable", {})
                snapshots.append({
                    "checkpoint_id": cfg.get("checkpoint_id"),
                    "created_at": (meta or {}).get("created_at"),
                    "step": (meta or {}).get("step", 0),
                })
        except Exception:  # pragma: no cover
            pass
    return snapshots


def reset_checkpointer() -> None:
    """清空进程内 MemorySaver 单例（测试隔离用：换临时库文件前调用）。"""
    global _memory_saver
    _memory_saver = None


def latest_checkpoint_info(task_id: str, db_path: Optional[str] = None) -> Optional[Dict]:
    """（维度一·断点信息卡）读取最新快照的元数据 + 状态摘要（同步只读）。

    从最新 CheckpointTuple 中提取：
    - checkpoint_id / created_at / step（metadata：第几个 super-step）
    - state 摘要：plan 步骤数、completed_steps 进度、A2A iteration、草稿字数
      （checkpoint["channel_values"] 即最近一次节点写入后的完整 AgentState）

    :return: 信息 dict；无快照时返回 None
    """
    path = Path(db_path or CHECKPOINT_DB)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(path)) as cp:
            tup = cp.get_tuple({"configurable": {"thread_id": task_id}})
            return _extract_info(tup)
    except Exception as exc:  # pragma: no cover - sqlite 打开失败降级 memory
        logger.warning("SQLite 读取任务 %s 最新快照失败（%s），退回进程内 MemorySaver", task_id, exc)
        try:
            tup = _get_memory_saver().get_tuple({"configurable": {"thread_id": task_id}})
            return _extract_info(tup)
        except Exception:  # pragma: no cover
            return None


def _extract_info(tup) -> Optional[Dict]:
    """从 CheckpointTuple 提取断点信息（tuple 为 None 时返回 None）。"""
    if tup is None:
        return None
    config, checkpoint, meta = tup[0], tup[1], (tup[2] or {})
    cfg = config.get("configurable", {})
    values = (checkpoint or {}).get("channel_values", {}) or {}
    plan = values.get("plan") or []
    completed = values.get("completed_steps") or []
    return {
        "checkpoint_id": cfg.get("checkpoint_id"),
        # 快照时间在 checkpoint["ts"]（ISO 字符串），不在 metadata 里
        "created_at": (checkpoint or {}).get("ts") or meta.get("created_at"),
        "step": meta.get("step", 0),                     # 已执行的 super-step 数
        "plan_steps": len(plan),                          # 计划总步骤数
        "completed_steps": len(completed),                # 已完成步骤数
        "completed_step_ids": list(completed),            # 已完成步骤 id
        "iteration": values.get("iteration", 0),          # A2A 返工轮次
        "draft_chars": len(values.get("draft_content") or ""),  # 当前草稿字数
    }
