"""维度一（可观测性）测试：tracing / events_repository / pricing / 观测 API。"""
import asyncio

import pytest

from db import events_repository, task_repository
from db.database import init_db
from observability import pricing, tracing


@pytest.fixture
def repo_db(tmp_path):
    """隔离 SQLite 库文件（临时目录）。"""
    init_db(tmp_path / "test.db")
    yield
    task_repository.get_connection().close()


# ---------------------------------------------------------------- pricing


def test_pricing_known_model():
    cost = pricing.estimate_cost_usd("deepseek-chat", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.14 + 0.28, abs=1e-6)


def test_pricing_unknown_model_falls_back_and_zero_tokens():
    cost = pricing.estimate_cost_usd("some-unknown-model", 1_000_000, 0)
    assert cost == pytest.approx(0.15, abs=1e-6)   # default 输入价
    assert pricing.estimate_cost_usd("deepseek-chat", None, None) == 0.0


# ---------------------------------------------------------------- events_repository


def _span(task_id="t1", span_id="s1", **over):
    base = {
        "task_id": task_id, "span_id": span_id, "parent_span_id": None,
        "span_type": "llm", "name": "deepseek-chat", "status": "ok",
        "start_ts": 1000.0, "end_ts": 1001.5, "duration_ms": 1500.0,
        "model": "deepseek-chat", "tokens_in": 100, "tokens_out": 50,
        "cost_usd": 0.000028, "detail": {"messages": 2},
    }
    base.update(over)
    return base


def test_insert_and_list_events(repo_db):
    events_repository.insert_event(_span())
    events_repository.insert_event(_span(span_id="s2", span_type="tool", name="web_search",
                                         tool_name="web_search", args_hash="abc123",
                                         model=None, tokens_in=None, tokens_out=None, cost_usd=None))
    events = events_repository.list_events("t1")
    assert len(events) == 2
    assert events[0]["span_id"] == "s1"
    assert events[0]["detail"] == {"messages": 2}     # detail JSON 反序列化
    assert events[1]["tool_name"] == "web_search"


def test_summarize_events(repo_db):
    events_repository.insert_event(_span(span_id="n1", span_type="node", name="planner",
                                         model=None, tokens_in=None, tokens_out=None, cost_usd=None))
    events_repository.insert_event(_span(span_id="l1", parent_span_id="n1", start_ts=1000.2))
    events_repository.insert_event(_span(span_id="l2", parent_span_id="n1", start_ts=1000.5,
                                         tokens_in=200, tokens_out=100, cost_usd=0.00005,
                                         end_ts=1002.0))
    events_repository.insert_event(_span(span_id="tl1", span_type="tool", name="web_search",
                                         tool_name="web_search", retry_count=1,
                                         model=None, tokens_in=None, tokens_out=None, cost_usd=None))
    events_repository.insert_event(_span(span_id="e1", span_type="tool", name="fetch",
                                         tool_name="fetch", status="error", error_class="TimeoutError",
                                         model=None, tokens_in=None, tokens_out=None, cost_usd=None))
    s = events_repository.summarize_events("t1")
    assert s["node_count"] == 1
    assert s["llm_calls"] == 2
    assert s["tool_calls"] == 2
    assert s["tokens_in"] == 300 and s["tokens_out"] == 150
    assert s["cost_usd"] == pytest.approx(0.000078, abs=1e-6)
    assert s["error_count"] == 1
    assert s["models"]["deepseek-chat"]["calls"] == 2
    assert s["tools"]["web_search"]["retries"] == 1
    assert s["tools"]["fetch"]["errors"] == 1
    assert s["total_duration_ms"] == pytest.approx(2000.0, abs=1.0)


def test_delete_task_cascades_events(repo_db):
    task_repository.create_task("t1", "主题")
    events_repository.insert_event(_span())
    assert task_repository.delete_task("t1") is True
    assert events_repository.list_events("t1") == []


def test_mark_resumed(repo_db):
    task_repository.create_task("t1", "主题")
    task_repository.mark_resumed("t1")
    task_repository.mark_resumed("t1")
    task = task_repository.get_task("t1")
    assert task["resume_count"] == 2
    assert task["last_resume_at"]


# ---------------------------------------------------------------- tracing


def test_tracing_noop_without_task(repo_db):
    """未绑定任务（如单测直调 llm.chat）时：零副作用、不落库。"""
    span = tracing.start_span("llm", "m")
    assert span["_noop"] is True
    assert tracing.finish_span(span) is None
    assert events_repository.list_events("t1") == []


def test_tracing_span_lifecycle_and_parenting(repo_db):
    tracing.bind_task("t1")
    parent = tracing.start_span("node", "planner")
    token = tracing.set_current_span(parent)
    child = tracing.start_span("llm", "deepseek-chat", model="deepseek-chat")
    tracing.finish_span(child, tokens_in=10, tokens_out=5, cost_usd=0.000001)
    tracing.reset_current_span(token)
    tracing.finish_span(parent)

    events = events_repository.list_events("t1")
    assert len(events) == 2
    by_id = {e["span_id"]: e for e in events}
    assert by_id[parent["span_id"]]["parent_span_id"] is None
    assert by_id[child["span_id"]]["parent_span_id"] == parent["span_id"]
    assert by_id[child["span_id"]]["duration_ms"] >= 0
    assert by_id[child["span_id"]]["tokens_in"] == 10


def test_tracing_error_span(repo_db):
    tracing.bind_task("t2")
    span = tracing.start_span("tool", "web_search", tool_name="web_search",
                              args_hash=tracing.args_hash({"query": "q"}))
    tracing.finish_span(span, status="error", error_class="TimeoutError", retry_count=2)
    events = events_repository.list_events("t2")
    assert events[0]["status"] == "error"
    assert events[0]["error_class"] == "TimeoutError"
    assert events[0]["retry_count"] == 2
    assert events[0]["args_hash"] == tracing.args_hash({"query": "q"})


def test_event_bus_publish_subscribe():
    bus = tracing.EventBus()
    q = bus.subscribe("t1")
    bus.publish("t1", {"kind": "span_start", "span": {"span_id": "s1"}})
    assert q.get_nowait()["kind"] == "span_start"
    bus.unsubscribe("t1", q)
    bus.publish("t1", {"kind": "x"})          # 退订后不再收到
    assert q.empty()


def test_event_bus_full_queue_drops_oldest():
    bus = tracing.EventBus()
    q = bus.subscribe("t1", maxsize=2)
    for i in range(3):
        bus.publish("t1", {"n": i})
    assert q.get_nowait()["n"] == 1           # 最旧的 n=0 被丢弃
    assert q.get_nowait()["n"] == 2


# ---------------------------------------------------------------- 观测 API


def test_events_api(repo_db):
    from fastapi.testclient import TestClient
    from main import app

    task_repository.create_task("t_api", "主题")
    events_repository.insert_event(_span(task_id="t_api"))
    client = TestClient(app)
    resp = client.get("/api/v1/tasks/t_api/events")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 1
    resp = client.get("/api/v1/tasks/t_api/events/summary")
    assert resp.status_code == 200
    assert resp.json()["llm_calls"] == 1
    assert client.get("/api/v1/tasks/nope/events").status_code == 404


def test_checkpoint_api_no_snapshot(repo_db):
    from fastapi.testclient import TestClient
    from main import app

    task_repository.create_task("t_ck", "主题")
    client = TestClient(app)
    resp = client.get("/api/v1/tasks/t_ck/checkpoint")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_checkpoint"] is False
    assert body["checkpoint"] is None
    assert body["resume_count"] == 0
    assert client.get("/api/v1/tasks/nope/checkpoint").status_code == 404


def test_resume_marks_and_system_log(repo_db, monkeypatch):
    """恢复接口：resume_count+1、写 System 日志、任务回到 running。"""
    from fastapi.testclient import TestClient
    from main import app

    task_repository.create_task("t_rs", "主题")
    task_repository.update_task("t_rs", status="paused")

    async def fake_run(task_id, topic, resume=False):   # 不真跑图
        task_repository.update_task(task_id, status="completed")
    monkeypatch.setattr("graph.workflow.run_task", fake_run)

    client = TestClient(app)
    resp = client.post("/api/v1/tasks/t_rs/resume")
    assert resp.status_code == 200
    task = task_repository.get_task("t_rs")
    assert task["resume_count"] == 1
    assert task["last_resume_at"]
    logs = task_repository.get_logs("t_rs")
    assert any(l["log_type"] == "System" and "恢复执行" in l["content"] for l in logs)


def test_sse_stream_replay_and_done(repo_db):
    """SSE：先回放库中 span，任务终态时收到 done 事件。"""
    from fastapi.testclient import TestClient
    from main import app

    task_repository.create_task("t_sse", "主题")
    task_repository.update_task("t_sse", status="completed")
    events_repository.insert_event(_span(task_id="t_sse"))

    client = TestClient(app)
    chunks: list[str] = []
    with client.stream("GET", "/api/v1/tasks/t_sse/events/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for chunk in resp.iter_text():
            chunks.append(chunk)
            if '"kind": "done"' in chunk.replace("\\", ""):
                break
    payload = "".join(chunks)
    assert "span_end" in payload               # 回放的库中 span
    assert "deepseek-chat" in payload
