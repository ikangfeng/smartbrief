"""报告 → Skill 闭环测试：沉淀（distill）+ 复用（Writer 挂载）。

- 沉淀：LLM 提炼输出解析、技能名清洗、注册与持久化
- 复用：Writer 撰写时自动挂载已启用的 writing 类技能并写日志
"""
import asyncio
from types import SimpleNamespace

import pytest

from skills import distill as distill_mod
from skills import registry as reg_mod
from skills.skills_manager import SkillsManager
from agents import react_writer


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """隔离注册表与技能池目录；distill 模块的单例 manager 也替换为隔离实例。"""
    monkeypatch.setattr(reg_mod, "registry", reg_mod.SkillRegistry())
    monkeypatch.setattr(
        "skills.skills_manager.SKILL_CONFIG_PATH", tmp_path / "skill_config.json"
    )
    mgr = SkillsManager(pool_dir=tmp_path / "skills_pool")
    mgr.init()
    monkeypatch.setattr(distill_mod, "skill_manager", mgr)
    return mgr


def _wrap(text: str):
    async def fake_chat(messages, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    return fake_chat


# ---------------------------------------------------------------- distill


def test_parse_distill_result_plain_json():
    data = distill_mod._parse_distill_result('{"name": "a", "body": "正文"}')
    assert data["body"] == "正文"


def test_parse_distill_result_in_fence():
    data = distill_mod._parse_distill_result('```json\n{"name": "a", "body": "正文"}\n```')
    assert data["name"] == "a"


def test_parse_distill_result_missing_body():
    with pytest.raises(ValueError):
        distill_mod._parse_distill_result('{"name": "a"}')


def test_safe_skill_name_cleaning(manager):
    assert distill_mod._safe_skill_name("储能市场分析 Report!!").startswith("report")
    name = distill_mod._safe_skill_name("Market Analysis")
    assert name == "market-analysis"


def test_safe_skill_name_collision(manager):
    manager.create_skill("market-analysis", "旧", "旧", "writing", skill_md="旧正文")
    name = distill_mod._safe_skill_name("Market Analysis")
    assert name.startswith("market-analysis-")  # 撞名追加随机后缀


def test_distill_skill_from_task(manager, monkeypatch):
    monkeypatch.setattr(distill_mod, "chat", _wrap(
        '{"name": "storage-market", "display_name": "储能市场分析", '
        '"description": "便携储能类市场调研报告", "body": "## 适用场景\\n..."}'
    ))
    task = {"task_id": "t1", "topic": "储能市场", "final_report": "# 报告正文"}
    skill = asyncio.run(distill_mod.distill_skill_from_task(task))
    assert skill["name"] == "storage-market"
    assert skill["category"] == "writing"
    # 已持久化到技能池
    assert (manager.pool_dir / "storage-market" / "skill.md").exists()


def test_distill_skill_bad_llm_output(manager, monkeypatch):
    monkeypatch.setattr(distill_mod, "chat", _wrap("无法提炼"))
    with pytest.raises(ValueError):
        asyncio.run(distill_mod.distill_skill_from_task(
            {"task_id": "t2", "topic": "x", "final_report": "y"}
        ))


# ---------------------------------------------------------------- Writer 挂载


def test_mount_writing_skills_filters(manager):
    manager.create_skill("w1", "写作技能A", "d", "writing", skill_md="技能正文A")
    manager.create_skill("w2", "写作但禁用", "d", "writing", skill_md="技能正文B")
    manager.set_enabled("w2", False)
    manager.create_skill("g1", "通用类", "d", "other", skill_md="不该被挂载")
    mounted = react_writer._mount_writing_skills()
    assert [s["name"] for s in mounted] == ["w1"]  # 仅启用 + writing 类 + 非内置


def test_writer_injects_mounted_skill(manager, monkeypatch):
    manager.create_skill("w1", "储能写作法", "d", "writing", skill_md="## 章节框架\n先市场后竞争")
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[1]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="章节"))])

    monkeypatch.setattr(react_writer, "chat", fake_chat)
    logs = []
    asyncio.run(react_writer.react_writer_run("写市场概述", 1, logs, {}))
    assert "储能写作法" in captured["prompt"]          # 技能正文进了 prompt
    assert "先市场后竞争" in captured["prompt"]
    assert any("已挂载写作技能" in log["content"] for log in logs)  # 日志可见


# ---------------------------------------------------------------- 单篇评分接口


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离配置路径与 SQLite 库文件，启动应用（不污染真实 data.db）。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "api_test.db")
    from db.database import init_db

    init_db(tmp_path / "api_test.db")
    from main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def test_score_endpoint(client, monkeypatch):
    from db import task_repository
    from evaluation import judge

    task_repository.create_task("task_score_1", "储能市场")
    task_repository.update_task("task_score_1", status="completed", final_report="# 报告")

    async def fake_judge(topic, report):
        return {"score": 4.0, "dimensions": {"structure": 4}, "verdict": "pass",
                "rationale": "结构清晰", "error": None}

    monkeypatch.setattr(judge, "judge_report", fake_judge)
    resp = client.post("/api/v1/tasks/task_score_1/score")
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] == 4.0 and data["judge_model"]


def test_score_endpoint_unfinished_task(client):
    from db import task_repository

    task_repository.create_task("task_score_2", "未完成主题")
    resp = client.post("/api/v1/tasks/task_score_2/score")
    assert resp.status_code == 400
