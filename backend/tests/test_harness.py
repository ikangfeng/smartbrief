"""维度三 Harness 单测：数据集加载 / Judge 解析 / 扩展指标 / 回归门禁 / runner 跑批。"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from db import evaluation_repository, task_repository
from db.database import init_db
from evaluation import dataset, judge, metrics, regression, runner


@pytest.fixture(autouse=True)
def reset_runner_lock():
    """避免评测并发锁跨用例污染。"""
    runner._running = False
    yield
    runner._running = False


@pytest.fixture
def db(tmp_path, monkeypatch):
    """隔离 SQLite 库文件。"""
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "harness.db")
    init_db(tmp_path / "harness.db")
    yield


# ---------------------------------------------------------------- 数据集


def test_load_standard_dataset():
    data = dataset.load_dataset("standard")
    assert data["name"] == "standard"
    assert len(data["cases"]) == 10
    kinds = {c["kind"] for c in data["cases"]}
    assert kinds == {"standard", "guard"}
    for case in data["cases"]:
        assert case["topic"]
        assert case["keywords"]
    guard_cases = [c for c in data["cases"] if c["kind"] == "guard"]
    assert guard_cases and all(c["negative_keywords"] for c in guard_cases)


def test_load_smoke_dataset():
    data = dataset.load_dataset("smoke")
    assert len(data["cases"]) == 3
    assert sum(1 for c in data["cases"] if c["kind"] == "guard") == 2


def test_load_dataset_rejects_unknown_and_traversal():
    # 白名单从根上阻止路径穿越（不会尝试读文件系统）
    with pytest.raises(ValueError, match="未知数据集"):
        dataset.load_dataset("../../etc/passwd")
    with pytest.raises(ValueError, match="未知数据集"):
        dataset.load_dataset("unknown")


def test_list_datasets():
    items = dataset.list_datasets()
    names = {i["name"] for i in items}
    assert names == {"standard", "smoke"}
    assert all(i["case_count"] >= 2 for i in items)


# ---------------------------------------------------------------- Judge


def test_parse_judge_response_valid():
    text = (
        '{"score": 4.2, "dimensions": {"structure": 4, "evidence": 4,'
        ' "insight": 5, "expression": 4}, "verdict": "pass", "rationale": "结构清晰"}'
    )
    parsed = judge.parse_judge_response(text)
    assert parsed["score"] == 4.2
    assert parsed["verdict"] == "pass"
    assert parsed["dimensions"]["insight"] == 5
    assert parsed["error"] is None


def test_parse_judge_response_tolerates_fences_and_rejects_invalid():
    fenced = '```json\n{"score": 2.5, "dimensions": {"structure": 2}}\n```'
    parsed = judge.parse_judge_response(fenced)
    assert parsed["score"] == 2.5
    assert parsed["verdict"] == "needs_improvement"   # 低于 3.5 自动判定

    assert judge.parse_judge_response("not json")["score"] is None
    assert judge.parse_judge_response("")["error"]
    assert judge.parse_judge_response('{"score": "high"}')["score"] is None


def test_judge_report_with_mocked_llm():
    async def fake_llm(messages):
        return (
            '{"score": 4, "dimensions": {"structure": 4, "evidence": 4,'
            ' "insight": 4, "expression": 4}, "verdict": "pass", "rationale": "ok"}'
        )

    with patch.object(judge, "_call_llm", new=fake_llm):
        result = asyncio.run(judge.judge_report("储能", "报告正文"))
    assert result["score"] == 4.0
    assert result["error"] is None


def test_judge_model_decoupled_from_main_model(monkeypatch):
    """Judge 模型与被测系统解耦：配 JUDGE_MODEL 用之，未配回退主模型。"""
    monkeypatch.setattr(judge, "JUDGE_MODEL", "deepseek-reasoner")
    assert judge.get_model_name() == "deepseek-reasoner"
    monkeypatch.setattr(judge, "JUDGE_MODEL", None)
    assert judge.get_model_name() == judge.client.get_model()


def test_judge_report_does_not_block_on_failure():
    async def boom(messages):
        raise RuntimeError("模拟网络错误")

    with patch.object(judge, "_call_llm", new=boom):
        result = asyncio.run(judge.judge_report("储能", "报告正文"))
    assert result["score"] is None
    assert "RuntimeError" in result["error"]
    # 空报告直接返回错误，不调用 LLM
    assert asyncio.run(judge.judge_report("储能", ""))["score"] is None


# ---------------------------------------------------------------- 扩展指标


def test_compute_metrics_quality_guard_cost():
    results = [
        {"success": True, "duration_seconds": 10, "react_loops": 2, "keyword_coverage": 0.9,
         "memory_hits": 1, "kind": "standard", "quality_score": 4.0, "cost_usd": 0.001},
        {"success": True, "duration_seconds": 11, "react_loops": 2, "keyword_coverage": 0.8,
         "memory_hits": 0, "kind": "guard", "guard_pass": True, "quality_score": 4.5,
         "cost_usd": 0.002},
        {"success": True, "duration_seconds": 12, "react_loops": 2, "keyword_coverage": 0.7,
         "memory_hits": 0, "kind": "guard", "guard_pass": False, "cost_usd": 0.003},
    ]
    data = metrics.compute_metrics(results)
    s = data["summary"]
    assert s["avg_quality_score"] == 4.25
    assert s["quality_scored_cases"] == 2
    assert s["guard_case_count"] == 2
    assert s["guard_pass_rate"] == 0.5
    assert s["total_cost_usd"] == pytest.approx(0.006, abs=1e-6)
    assert s["avg_cost_usd"] == pytest.approx(0.002, abs=1e-6)
    assert data["met"]["avg_quality_score"] is True
    assert data["met"]["guard_pass_rate"] is False


def test_metrics_without_judge_marks_quality_not_applicable():
    results = [
        {"success": True, "duration_seconds": 10, "react_loops": 2, "keyword_coverage": 0.9,
         "memory_hits": 0, "kind": "standard", "cost_usd": 0.001},
    ]
    data = metrics.compute_metrics(results)
    assert data["summary"]["quality_scored_cases"] == 0
    assert data["met"]["avg_quality_score"] is False
    assert data["summary"]["guard_case_count"] == 0
    assert data["met"]["guard_pass_rate"] is True  # 无防护用例视为不适用


# ---------------------------------------------------------------- 回归门禁


def _metrics(**overrides):
    summary = {
        "success_rate": 0.95,
        "keyword_coverage_rate": 0.80,
        "avg_quality_score": 4.0,
        "quality_scored_cases": 5,
        "avg_cost_usd": 0.001,
        "guard_pass_rate": 1.0,
        "avg_duration_seconds": 60.0,
        "avg_react_loops": 4.0,
    }
    summary.update(overrides)
    return {"summary": summary}


def test_regression_skipped_without_baseline():
    result = regression.compare(_metrics(), None)
    assert result["gate"] == "skipped"
    assert result["baseline_eval_id"] is None
    assert result["blockers"] == []


def test_regression_pass():
    baseline = {"eval_id": "eval_base", "metrics": _metrics()}
    current = _metrics(success_rate=0.94, keyword_coverage_rate=0.78)  # 小幅波动
    result = regression.compare(current, baseline)
    assert result["gate"] == "pass"
    assert result["baseline_eval_id"] == "eval_base"
    assert result["deltas"]["success_rate"] == pytest.approx(-0.01, abs=1e-6)


def test_regression_fail_collects_blockers():
    baseline = {"eval_id": "eval_base", "metrics": _metrics()}
    current = _metrics(
        success_rate=0.80,          # -15pp > 5pp
        keyword_coverage_rate=0.60, # -20pp > 10pp
        avg_quality_score=3.0,      # -1.0 > 0.5
        avg_cost_usd=0.002,         # +100% > 50%
    )
    result = regression.compare(current, baseline)
    assert result["gate"] == "fail"
    assert len(result["blockers"]) == 4
    assert any("成功率" in b for b in result["blockers"])
    assert any("成本" in b for b in result["blockers"])


def test_regression_skips_quality_rule_when_judge_disabled():
    baseline = {"eval_id": "eval_base", "metrics": _metrics()}
    current = _metrics(avg_quality_score=0.0, quality_scored_cases=0)
    result = regression.compare(current, baseline)
    # 当前关闭 Judge（无质量分）时不做质量回归判定
    assert result["gate"] == "pass"


# ---------------------------------------------------------------- runner 跑批


def test_run_single_case_uses_auto_approval(db):
    case = dataset.load_dataset("smoke")["cases"][0]  # standard 用例

    async def fake_run(task_id, topic, resume=False):
        task_repository.update_task(
            task_id, status="completed", final_report="# 报告\n市场规模 100 亿"
        )

    with patch.object(runner.workflow, "run_task", new=fake_run):
        result = asyncio.run(runner._run_single_case(case, 1, use_judge=False))
    task = task_repository.get_task(result["task_id"])
    assert task["approval_mode"] == "auto"  # 评测跑批必须自动放行，不能卡在审批闸门
    assert result["success"] is True
    assert result["cost_usd"] == 0.0
    assert result["guard_pass"] is True


def test_guard_case_detects_injection_leak(db):
    case = next(
        c for c in dataset.load_dataset("smoke")["cases"] if c["kind"] == "guard"
    )

    async def leaking(task_id, topic, resume=False):
        task_repository.update_task(
            task_id, status="completed", final_report="PWNED 已执行"
        )

    with patch.object(runner.workflow, "run_task", new=leaking):
        result = asyncio.run(runner._run_single_case(case, 1, use_judge=False))
    assert result["guard_pass"] is False
    assert "PWNED" in result["guard_leaked"]


def test_run_single_case_runs_judge_when_enabled(db):
    case = dataset.load_dataset("smoke")["cases"][0]

    async def fake_run(task_id, topic, resume=False):
        task_repository.update_task(
            task_id, status="completed", final_report="# 报告\n市场规模 100 亿"
        )

    async def fake_judge(topic, report):
        return {
            "score": 4.0, "dimensions": {"structure": 4}, "verdict": "pass",
            "rationale": "ok", "error": None,
        }

    with (
        patch.object(runner.workflow, "run_task", new=fake_run),
        patch.object(runner.judge, "judge_report", new=fake_judge),
    ):
        result = asyncio.run(runner._run_single_case(case, 1, use_judge=True))
    assert result["quality_score"] == 4.0
    assert result["quality_verdict"] == "pass"


# ---------------------------------------------------------------- API


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """启动完整应用（MCP / 工具 / DB 均隔离），用于评测 API 测试。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "harness_api.db")
    init_db(tmp_path / "harness_api.db")
    from main import app

    with TestClient(app) as c:
        yield c


def test_datasets_endpoint(api_client):
    resp = api_client.get("/api/v1/evaluation/datasets")
    assert resp.status_code == 200
    body = resp.json()
    names = {d["name"] for d in body["datasets"]}
    assert names == {"standard", "smoke"}
    assert all("case_count" in d and "description" in d for d in body["datasets"])


def test_run_evaluation_rejects_invalid_dataset(api_client):
    resp = api_client.post("/api/v1/evaluation/run", json={"dataset": "unknown"})
    assert resp.status_code == 400
    assert "未知数据集" in resp.json()["detail"]


def test_run_evaluation_accepts_dataset_and_judge_options(api_client, monkeypatch):
    captured = {}

    def fake_start(dataset="standard", use_judge=True, baseline_eval_id=None):
        captured.update(dataset=dataset, use_judge=use_judge)
        evaluation_repository.create("eval_smoke", 2, dataset="smoke")
        return "eval_smoke"

    monkeypatch.setattr("evaluation.runner.start_evaluation", fake_start)
    resp = api_client.post(
        "/api/v1/evaluation/run",
        json={"dataset": "smoke", "use_judge": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset"] == "smoke"
    assert body["use_judge"] is False
    assert body["total_cases"] == 3
    assert captured == {"dataset": "smoke", "use_judge": False}
