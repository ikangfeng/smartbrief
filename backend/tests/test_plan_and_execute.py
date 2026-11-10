"""Planner / Scheduler / ReAct 执行器测试（第 24 节）。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import planner, react_researcher, react_writer, scheduler


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- Planner


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeResp:
    def __init__(self, content):
        self.choices = [MagicMock(message=FakeMessage(content))]


def test_parse_plan_valid_json():
    content = '[{"id": 1, "agent": "researcher", "task": "搜索市场规模", "depends_on": []}]'
    steps = planner.parse_plan(content)
    assert len(steps) == 1
    assert steps[0]["id"] == 1
    assert steps[0]["agent"] == "researcher"


def test_parse_plan_with_code_fence():
    content = '```json\n[{"id": 1, "agent": "writer", "task": "撰写", "depends_on": []}]\n```'
    steps = planner.parse_plan(content)
    assert steps[0]["agent"] == "writer"


def test_parse_plan_invalid_returns_empty():
    assert planner.parse_plan("不是 JSON") == []
    assert planner.parse_plan("") == []


def test_parse_plan_filters_invalid_steps():
    content = json_dumps_list(
        [
            {"id": 1, "agent": "researcher", "task": "ok", "depends_on": []},
            {"id": 2, "agent": "hacker", "task": "非法", "depends_on": []},
            {"id": 3, "agent": "writer", "task": "", "depends_on": []},
        ]
    )
    steps = planner.parse_plan(content)
    assert [s["id"] for s in steps] == [1]


def json_dumps_list(data):
    import json

    return json.dumps(data)


def test_fallback_plan_shape():
    steps = planner.fallback_plan("手机")
    assert len(steps) == 3
    assert [s["agent"] for s in steps] == ["researcher", "researcher", "writer"]


def test_planner_node_uses_llm():
    content = json_dumps_list(
        [
            {"id": 1, "agent": "researcher", "task": "市场规模", "depends_on": []},
            {"id": 2, "agent": "writer", "task": "写摘要", "depends_on": [1]},
        ]
    )
    state = {"task_id": "t1", "topic": "智能手表", "react_logs": []}
    with patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(content))):
        result = run(planner.planner_node(state))
    assert len(result["plan"]) == 2
    assert result["current_step_index"] == 0
    assert any("已生成执行计划" in log["content"] for log in result["react_logs"])


def test_planner_node_falls_back_on_llm_error():
    state = {"task_id": "t1", "topic": "手机", "react_logs": []}
    with patch.object(planner, "chat", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = run(planner.planner_node(state))
    assert len(result["plan"]) == 3  # 降级计划
    assert any("降级计划" in log["content"] for log in result["react_logs"])


# ---------------------------------------------------------------- Scheduler


class FakeStep:
    pass


def test_ready_steps_dependency():
    plan = [
        {"id": 1, "agent": "researcher", "task": "a", "depends_on": []},
        {"id": 2, "agent": "researcher", "task": "b", "depends_on": [1]},
        {"id": 3, "agent": "writer", "task": "c", "depends_on": [1, 2]},
    ]
    assert [s["id"] for s in scheduler._ready_steps(plan, [])] == [1]
    assert [s["id"] for s in scheduler._ready_steps(plan, [1])] == [2]
    assert [s["id"] for s in scheduler._ready_steps(plan, [1, 2])] == [3]
    assert scheduler._ready_steps(plan, [1, 2, 3]) == []


def test_scheduler_node_runs_all_steps():
    plan = [
        {"id": 1, "agent": "researcher", "task": "市场规模", "depends_on": []},
        {"id": 2, "agent": "writer", "task": "写摘要", "depends_on": [1]},
    ]
    state = {
        "task_id": "t1",
        "plan": plan,
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
        "draft_content": "",
    }
    with (
        patch.object(
            scheduler, "react_researcher_run",
            new=AsyncMock(return_value={"observations": [{"query": "q", "result": "数据"}]}),
        ) as researcher,
        patch.object(
            scheduler, "react_writer_run",
            new=AsyncMock(return_value="## 摘要\n内容"),
        ) as writer,
    ):
        result = run(scheduler.scheduler_node(state))
    assert result["completed_steps"] == [1, 2]
    assert result["research_data"]["step_1"] == {"observations": [{"query": "q", "result": "数据"}]}
    assert result["draft_content"] == "## 摘要\n内容"
    researcher.assert_awaited_once()
    writer.assert_awaited_once()
    assert result["current_step_index"] == 2


def test_scheduler_node_empty_plan():
    state = {
        "task_id": "t1",
        "plan": [],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
        "draft_content": "",
    }
    result = run(scheduler.scheduler_node(state))
    assert result["completed_steps"] == []
    assert result["draft_content"] == "（无 writer 步骤生成内容）"


# ---------------------------------------------------------------- ReAct Researcher


def test_react_researcher_done_first():
    logs = []
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(return_value=FakeResp('{"action": "done"}'))),
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock()) as call,
    ):
        result = run(react_researcher.react_researcher_run("搜索手机", 1, logs))
    assert result == {"observations": []}
    call.assert_not_awaited()
    assert any("信息已足够" in log["content"] for log in logs)


def test_react_researcher_search_once():
    logs = []
    resp1 = FakeResp('{"action": "web_search", "query": "手机 2025 市场"}')
    resp2 = FakeResp('{"action": "done"}')
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(side_effect=[resp1, resp2])),
        patch.object(
            react_researcher.mcp_manager, "call_tool",
            new=AsyncMock(return_value={"title": "市场报告", "summary": "100 亿"}),
        ) as call,
    ):
        result = run(react_researcher.react_researcher_run("搜索手机", 1, logs))
    assert len(result["observations"]) == 1
    assert result["observations"][0]["query"] == "手机 2025 市场"
    call.assert_awaited_once_with("web_search", {"query": "手机 2025 市场"})
    types = [log["type"] for log in logs]
    assert "Thought" in types and "Action" in types and "Observation" in types


def test_react_researcher_search_failure():
    logs = []
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(return_value=FakeResp('{"action": "web_search", "query": "q"}'))),
        patch.object(
            react_researcher.mcp_manager, "call_tool",
            new=AsyncMock(side_effect=RuntimeError("网络错误")),
        ),
    ):
        result = run(react_researcher.react_researcher_run("搜索 q", 1, logs))
    assert result["observations"] == []
    assert any("web_search 调用失败" in log["content"] for log in logs)


def test_react_researcher_max_steps():
    logs = []
    always_search = FakeResp('{"action": "web_search", "query": "q"}')
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(return_value=always_search)),
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock(return_value={"x": 1})),
    ):
        result = run(react_researcher.react_researcher_run("循环搜索", 1, logs))
    assert len(result["observations"]) == react_researcher.MAX_REACT_STEPS


def test_react_researcher_llm_error_falls_back_to_search():
    logs = []
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(side_effect=RuntimeError("LLM 挂了"))),
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock(return_value={"r": 1})),
    ):
        result = run(react_researcher.react_researcher_run("降级搜索", 1, logs))
    assert len(result["observations"]) == 1
    assert any("降级为直接搜索" in log["content"] for log in logs)


def test_react_researcher_fetches_first_source():
    """搜索返回有效链接时：结构化 sources + 自动抓取首个来源正文（挂 content 字段）。"""
    logs = []
    search_result = {
        "source": "bing",
        "query": "q",
        "results": [
            {"title": "T1", "url": "https://a.com/1", "snippet": "s1"},
            {"title": "T2", "url": "https://b.com/2", "snippet": "s2"},
        ],
    }

    async def fake_call(name, arguments=None):
        if name == "web_search":
            return search_result
        if name == "fetch":
            return {"text": "正文数字 123 亿"}
        raise AssertionError(f"未预期的工具调用：{name}")

    resp1 = FakeResp('{"action": "web_search", "query": "q"}')
    resp2 = FakeResp('{"action": "done"}')
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(side_effect=[resp1, resp2])),
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock(side_effect=fake_call)),
    ):
        result = run(react_researcher.react_researcher_run("研究 q", 1, logs))
    assert len(result["sources"]) == 2
    assert result["sources"][0]["content"] == "正文数字 123 亿"
    assert "content" not in result["sources"][1]
    assert any(log["type"] == "Action" and "fetch" in log["content"] for log in logs)


def test_react_researcher_fetch_falls_back_to_next_source():
    """首个来源被 robots.txt 拦截时：自动换下一个来源再试，正文挂到抓取成功的来源上。"""
    logs = []
    search_result = {
        "source": "bing",
        "query": "q",
        "results": [
            {"title": "T1", "url": "https://a.com/1", "snippet": "s1"},
            {"title": "T2", "url": "https://b.com/2", "snippet": "s2"},
        ],
    }

    async def fake_call(name, arguments=None):
        if name == "web_search":
            return search_result
        if name == "fetch":
            if arguments["url"] == "https://a.com/1":
                raise RuntimeError("robots.txt 禁止抓取")   # _run_fetch 内部捕获降级为 ""
            return {"text": "正文 42 亿"}
        raise AssertionError(f"未预期的工具调用：{name}")

    resp1 = FakeResp('{"action": "web_search", "query": "q"}')
    resp2 = FakeResp('{"action": "done"}')
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(side_effect=[resp1, resp2])),
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock(side_effect=fake_call)),
    ):
        result = run(react_researcher.react_researcher_run("研究 q", 1, logs))
    assert "content" not in result["sources"][0]          # 第一家被挡，无正文
    assert result["sources"][1]["content"] == "正文 42 亿"  # 降级抓到第二家
    fetches = [l for l in logs if l["type"] == "Action" and "fetch" in l["content"]]
    assert len(fetches) == 2                              # 两次抓取尝试都有日志


def test_react_researcher_skips_fetch_for_mock_results():
    """降级模拟结果（source=mock，example.com 链接）不抓正文，只保留来源列表。"""
    logs = []
    mock_result = {
        "source": "mock",
        "query": "q",
        "results": [{"title": "m", "url": "https://example.com/1", "snippet": "x"}],
    }
    resp1 = FakeResp('{"action": "web_search", "query": "q"}')
    resp2 = FakeResp('{"action": "done"}')
    with (
        patch.object(react_researcher, "chat", new=AsyncMock(side_effect=[resp1, resp2])),
        patch.object(
            react_researcher.mcp_manager, "call_tool", new=AsyncMock(return_value=mock_result)
        ) as call,
    ):
        result = run(react_researcher.react_researcher_run("研究 q", 1, logs))
    call.assert_awaited_once_with("web_search", {"query": "q"})   # 只有搜索，没有 fetch
    assert result["sources"][0]["url"] == "https://example.com/1"
    assert "content" not in result["sources"][0]


def test_scheduler_appends_references_section():
    """研究数据带来源时：草稿末尾自动追加「参考资料」章节（编号 + 锚点 + 链接）。"""
    plan = [
        {"id": 1, "agent": "researcher", "task": "调研", "depends_on": []},
        {"id": 2, "agent": "writer", "task": "撰写", "depends_on": [1]},
    ]
    state = {
        "task_id": "t1",
        "plan": plan,
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
        "draft_content": "",
    }
    with (
        patch.object(
            scheduler, "react_researcher_run",
            new=AsyncMock(return_value={
                "observations": [],
                "sources": [{"title": "T", "url": "https://a.com", "snippet": "s", "content": ""}],
            }),
        ),
        patch.object(scheduler, "react_writer_run", new=AsyncMock(return_value="## 正文\n数据 [1]")),
    ):
        result = run(scheduler.scheduler_node(state))
    assert "## 参考资料" in result["draft_content"]
    assert 'id="ref-1"' in result["draft_content"]
    assert "[T](https://a.com)" in result["draft_content"]


# ---------------------------------------------------------------- ReAct Writer


def test_react_writer_generates_section():
    logs = []
    research_data = {"step_1": {"observations": [{"query": "q", "result": "100亿"}]}}
    with patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 摘要\n内容"))):
        content = run(react_writer.react_writer_run("写摘要", 2, logs, research_data))
    assert content == "## 摘要\n内容"
    types = [log["type"] for log in logs]
    # 日志序列：若干 Thought（RAG/技能挂载等可选）→ Action(llm_gen) → Observation
    assert types.count("Action") == 1 and types[-1] == "Observation"
    assert any("llm_gen" in log["content"] for log in logs)
    assert any("RAG 检索" in log["content"] for log in logs)


def test_react_writer_empty_research():
    logs = []
    with patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("内容"))):
        content = run(react_writer.react_writer_run("写摘要", 2, logs, {}))
    assert content == "内容"


def test_react_writer_llm_error():
    logs = []
    with patch.object(react_writer, "chat", new=AsyncMock(side_effect=RuntimeError("boom"))):
        content = run(react_writer.react_writer_run("写摘要", 2, logs, {}))
    assert "撰写失败" in content
    assert any("llm_gen 调用失败" in log["content"] for log in logs)


def test_react_writer_summarize_research():
    research = {"step_1": {"observations": [{"query": "q", "result": {"a": 1}}]}}
    summary = react_writer._summarize_research(research)
    assert "q" in summary
    assert "（暂无研究数据）" not in summary
    assert "（暂无研究数据）" in react_writer._summarize_research({})


def test_collect_sources_numbers_by_step_order_and_dedupes():
    """来源编号按步骤号升序分配、按 URL 去重（与文末参考资料编号一致）。"""
    research = {
        "step_2": {"sources": [{"title": "B", "url": "https://b.com", "snippet": "", "content": "c2"}]},
        "step_1": {"sources": [
            {"title": "A", "url": "https://a.com", "snippet": "s", "content": "c1"},
            {"title": "A-dup", "url": "https://a.com", "snippet": "s", "content": ""},
        ]},
    }
    sources = react_writer.collect_sources(research)
    assert [s["id"] for s in sources] == [1, 2]
    assert sources[0]["title"] == "A"
    assert sources[1]["title"] == "B"
    assert react_writer.collect_sources({}) == []


def test_summarize_research_includes_numbered_source_list():
    """研究摘要附「可引用的来源列表」（编号 + URL + 正文摘录），供行内引用 [n]。"""
    research = {
        "step_1": {
            "observations": [{"query": "q", "result": "r"}],
            "sources": [{"title": "T", "url": "https://a.com", "snippet": "s", "content": "正文摘录 123 亿"}],
        }
    }
    summary = react_writer._summarize_research(research)
    assert "[1] T | https://a.com" in summary
    assert "正文摘录 123 亿" in summary
