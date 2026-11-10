"""自动化评测引擎（第 27 节；维度三升级为 Harness）。

评测链路：
- **数据集化**：用例不再硬编码，改为从 `evaluation/datasets/*.json` 加载
  （standard 10 例 / smoke 2 例），每个用例带 expected keywords 与 negative keywords，
  kind=guard 的用例用于验证提示注入防护。
- **批量执行**：逐个调用 `workflow.run_task`，任务以 `approval_mode="auto"`
  创建（评测跑批不能卡在人工审批闸门），记录耗时 / 成功与否 / ReAct 循环次数。
- **成本采集**：从维度一的 task_events 汇总 token 与成本，写入每个用例结果。
- **LLM-as-Judge**：成功用例交给 `evaluation/judge.py` 打分（可开关，失败不阻断）。
- **回归门禁**：与同数据集最近一次 completed 评测对比，给出 pass / fail / skipped。
- **进度可见**：评测记录先落库（running）→ 每完成一个用例更新 completed_cases
  → 全部完成写入结果与指标（completed），前端评测中心轮询即可展示进度条。
- **并发保护**：同一时刻只允许一次评测运行（`_running` 标志）。
"""
import asyncio
import logging
import time
from uuid import uuid4

from db import evaluation_repository
from db import events_repository
from db import task_repository
from evaluation import dataset as dataset_mod
from evaluation import judge
from evaluation import metrics
from evaluation import regression
from graph import workflow
from mcp_client.mcp_client import mcp_manager

logger = logging.getLogger(__name__)

# 兼容旧调用方：默认数据集（standard）的用例列表
TEST_CASES = dataset_mod.load_dataset("standard")["cases"]

# 运行中的评测并发保护（同一时刻仅允许一次）
_running = False


async def _run_task_with_tool_mocks(task_id: str, topic: str, mock_tools: dict | None) -> None:
    """跑完整工作流；用例带 mock_tools 夹具时，拦截对应 MCP 工具调用返回固定内容。

    用途：间接注入防护用例 —— 恶意指令藏在「搜索结果/抓取正文」里进入系统，
    而真实 Tavily 返回不可控，只能在网络边界 mock。ReAct 循环、wrap_untrusted
    防护包装、Writer/Judge 全部走真实代码，被测的正是防护链本身。
    评测串行执行用例，patch 作用域仅在单个用例的生命周期内，finally 恢复。
    """
    if not mock_tools:
        await workflow.run_task(task_id, topic)
        return
    original = mcp_manager.call_tool

    async def patched(name: str, arguments: dict | None = None) -> dict:
        if name in mock_tools:
            return mock_tools[name]
        return await original(name, arguments)

    mcp_manager.call_tool = patched
    try:
        await workflow.run_task(task_id, topic)
    finally:
        mcp_manager.call_tool = original


def case_count(dataset: str = "standard") -> int:
    """数据集用例总数（前端「评测中心」展示用）。"""
    return len(dataset_mod.load_dataset(dataset)["cases"])


def _collect_cost(task_id: str) -> dict:
    """从 task_events 汇总该任务的 token / 成本（失败不影响评测）。"""
    try:
        summary = events_repository.summarize_events(task_id)
    except Exception:  # pragma: no cover - 观测表异常不应中断评测
        logger.warning("用例 %s 成本汇总失败（忽略）", task_id, exc_info=True)
        return {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    return {
        "cost_usd": summary.get("cost_usd") or 0.0,
        "tokens_in": summary.get("tokens_in") or 0,
        "tokens_out": summary.get("tokens_out") or 0,
    }


async def _run_single_case(
    case: dict,
    case_index: int,
    use_judge: bool = False,
    approval_mode: str = "auto",
) -> dict:
    """执行单个用例：创建任务记录 → 跑完整工作流 → 收集原始数据（+ 可选 Judge）。

    教学要点：与 `POST /api/v1/generate` 的提交逻辑一致（先落库 running 记录，
    再异步跑图），区别是**强制 approval_mode=auto** —— 否则 manual 模式会在
    计划闸门挂起，评测批量跑不完。任务结束再从 task_events 汇总成本。
    """
    task_id = f"task_{uuid4().hex[:8]}"
    kind = case.get("kind") or "standard"
    negative_keywords = case.get("negative_keywords") or []
    base = {
        "case_index": case_index,
        "case_id": case.get("id"),
        "topic": case["topic"],
        "category": case.get("category"),
        "difficulty": case.get("difficulty"),
        "kind": kind,
        "keywords": case["keywords"],
        "task_id": task_id,
        "quality_score": None,
        "quality_verdict": None,
        "judge_rationale": None,
        "judge_dimensions": {},
        "judge_error": None,
        "guard_pass": kind != "guard",  # 非防护用例默认不参与判定
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    task_repository.create_task(task_id, case["topic"], approval_mode=approval_mode)
    started = time.monotonic()
    try:
        await _run_task_with_tool_mocks(task_id, case["topic"], case.get("mock_tools"))
        duration = time.monotonic() - started
        task = task_repository.get_task(task_id)
        logs = task_repository.get_logs(task_id)
        report_text = (task or {}).get("final_report") or ""
        # 防护判定：报告不得包含 negative_keywords（大小写不敏感）
        leaked = [k for k in negative_keywords if k.lower() in report_text.lower()]
        guard_pass = kind != "guard" or not leaked
        # 结构化断言（可选）：引用数/参考资料/图表块 —— 质量分之外的结构性回归探测
        assertion_failures = metrics.check_assertions(report_text, case.get("assert"))
        # 成本：必须在 Judge 之前采集（Judge 在任务上下文之外，不应计入任务成本）
        cost = _collect_cost(task_id)

        result = {
            **base,
            "success": True,
            "duration_seconds": round(duration, 2),
            "react_loops": metrics.count_react_actions(logs),
            "keyword_coverage": metrics.compute_keyword_coverage(report_text, case["keywords"]),
            "memory_hits": len((task or {}).get("memory_hits") or []),
            "forced_pass": bool((task or {}).get("forced_pass")),
            "guard_pass": guard_pass,
            "guard_leaked": leaked,
            "assertion_failures": assertion_failures,
            **cost,
            "error": None,
        }
        if use_judge and report_text:
            judged = await judge.judge_report(case["topic"], report_text)
            result.update({
                "quality_score": judged.get("score"),
                "quality_verdict": judged.get("verdict"),
                "judge_rationale": judged.get("rationale"),
                "judge_dimensions": judged.get("dimensions") or {},
                "judge_error": judged.get("error"),
            })
        return result
    except Exception as exc:  # 单用例失败不中断整轮评测（失败也计入成功率分母）
        logger.exception("评测用例 %d 执行失败", case_index)
        return {
            **base,
            "success": False,
            "duration_seconds": None,
            "react_loops": 0,
            "keyword_coverage": 0.0,
            "memory_hits": 0,
            "forced_pass": False,
            "error": str(exc),
        }


def start_evaluation(
    dataset: str = "standard",
    use_judge: bool = True,
    baseline_eval_id: str | None = None,
) -> str:
    """触发一次自动化评测（同步返回 eval_id，后台批量执行）。

    :param dataset: 数据集名（standard / smoke）
    :param use_judge: 是否对成功用例运行 LLM-as-Judge 质量评分
    :param baseline_eval_id: 显式指定基线评测；为空则自动取同数据集最近一次 completed
    :raises ValueError: 数据集不存在 / 文件损坏（调用方返回 400）
    :raises RuntimeError: 已有评测在运行中（调用方返回 400）
    :return: eval_id（形如 eval_ab12cd34），前端立即用其轮询进度
    """
    global _running
    if _running:
        raise RuntimeError("评测已在运行中，请等待完成后再触发")
    # 先加载校验数据集（非法 name 直接报错，不占用并发锁）
    data = dataset_mod.load_dataset(dataset)
    cases = data["cases"]
    _running = True
    eval_id = f"eval_{uuid4().hex[:8]}"
    judge_model = judge.get_model_name() if use_judge else None
    config = {
        "dataset": dataset,
        "use_judge": use_judge,
        "baseline_eval_id": baseline_eval_id,
        "approval_mode": "auto",
    }
    try:
        evaluation_repository.create(
            eval_id,
            total_cases=len(cases),
            dataset=dataset,
            judge_model=judge_model,
            config=config,
        )
    except Exception:
        _running = False
        raise
    asyncio.create_task(
        _execute(eval_id, cases, dataset, use_judge, baseline_eval_id, judge_model)
    )  # 后台执行，接口立即返回
    logger.info(
        "评测已启动：%s（数据集 %s，共 %d 个用例，Judge=%s）",
        eval_id, dataset, len(cases), bool(use_judge),
    )
    return eval_id


async def _execute(
    eval_id: str,
    cases: list[dict],
    dataset: str,
    use_judge: bool,
    baseline_eval_id: str | None,
    judge_model: str | None,
) -> None:
    """后台批量执行全部用例 → 计算指标 → 回归对比 → 生成 Markdown → 落库（终态）。

    任何环节异常都落到 evaluation_reports.status=failed，
    绝不静默丢失（前端可提示重试）。
    """
    global _running
    try:
        results = []
        for index, case in enumerate(cases, start=1):
            result = await _run_single_case(case, index, use_judge=use_judge)
            results.append(result)
            evaluation_repository.set_progress(eval_id, len(results))  # 进度逐条更新
        metrics_data = metrics.compute_metrics(results)

        # 回归门禁：显式基线优先，否则取同数据集最近一次 completed（排除自己）
        if baseline_eval_id:
            baseline = evaluation_repository.get(baseline_eval_id)
        else:
            baseline = evaluation_repository.latest_completed(dataset, exclude_eval_id=eval_id)
        regression_data = regression.compare(metrics_data, baseline)

        # 结构化断言 → 绝对门禁：任一对报告"关键结构"的断言失败，直接阻断
        # （与基线无关 —— 引用归 0 这类回归不应被"和上次差不多"掩盖）
        assertion_blockers = [
            f"用例 {r.get('case_id')} 断言失败：{'；'.join(r['assertion_failures'])}"
            for r in results
            if r.get("assertion_failures")
        ]
        if assertion_blockers:
            regression_data.setdefault("blockers", []).extend(assertion_blockers)
            regression_data["gate"] = "fail"

        report_md = metrics.build_markdown(
            eval_id, results, metrics_data, regression=regression_data, dataset=dataset
        )
        evaluation_repository.finish(
            eval_id, results, metrics_data, report_md,
            regression=regression_data, judge_model=judge_model,
        )
        logger.info(
            "评测完成：%s（成功率 %.1f%%，质量分 %.2f，回归门禁 %s）",
            eval_id,
            metrics_data["summary"]["success_rate"] * 100,
            metrics_data["summary"]["avg_quality_score"],
            regression_data["gate"],
        )
    except Exception as exc:
        logger.exception("评测 %s 中断", eval_id)
        evaluation_repository.fail(eval_id, str(exc))
    finally:
        _running = False  # 释放并发锁，允许下一次评测
