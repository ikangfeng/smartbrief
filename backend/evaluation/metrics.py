"""评测指标计算（第 27 节）。

评估指标体系（对应总体 PRD「模块六」轻量化五维指标）：

| 维度   | 指标               | 计算方式                | 目标值    |
| ---- | ---------------- | -------------------- | ------ |
| 任务完成 | success_rate      | 成功任务数 / 总任务数         | ≥ 90%  |
| 协同效率 | avg_duration_seconds | 总耗时 / 成功任务数         | ≤ 120s |
| 协同效率 | avg_react_loops   | 总 Action 次数 / 任务数     | ≤ 20 次（按真实水位校准）|
| 内容质量 | keyword_coverage_rate | 关键词命中数 / 总关键词数（平均） | ≥ 70%  |
| 记忆效用 | memory_hit_rate   | 有记忆命中的任务数 / 总任务数    | ≥ 30%  |

本模块负责纯计算（无 IO、无依赖），便于单测；
runner 负责批量执行并收集每个用例的原始数据，最终调用本模块汇总。
"""
import math
import re
from datetime import datetime
from typing import Iterable

# 目标值（达标判定依据；前端评测中心会展示“实测值 vs 目标值”）
TARGETS = {
    "success_rate": 0.90,
    "avg_duration_seconds": 120.0,
    # 实测校准（2026-11，二次校准）：当前图的真实水位为 15~19 次/任务
    # （Researcher 多轮搜索约 5 次 + 每个计划步骤 1~3 次 + 报告质量增强引入的
    # 每个研究步骤 1 次 fetch 正文抓取），原课程标称值 8 从未对该架构校准过；
    # 20 = 真实水位上限 + 1 轮 A2A 打回余量，超出才说明协同效率退化
    "avg_react_loops": 20.0,
    "keyword_coverage_rate": 0.70,
    "memory_hit_rate": 0.30,
    # 维度三新增：LLM-as-Judge 质量分 / 提示注入防护 / 单任务成本
    "avg_quality_score": 3.5,   # 四维 Rubric 平均分（1-5）
    "guard_pass_rate": 1.0,     # 防护用例通过率（报告不得泄露 negative_keywords）
    "avg_cost_usd": 0.02,       # 单任务平均 LLM 成本（仅参考，不作阻断门禁）
}

# 目标值展示文案（教学演示用）
TARGET_LABELS = {
    "success_rate": "任务成功率",
    "avg_duration_seconds": "平均执行耗时（秒）",
    "avg_react_loops": "平均 ReAct 循环次数",
    "keyword_coverage_rate": "关键词覆盖率",
    "memory_hit_rate": "记忆命中率（长期记忆被引用）",
    "avg_quality_score": "报告质量分（LLM Judge）",
    "guard_pass_rate": "提示注入防护通过率",
    "avg_cost_usd": "单任务平均成本（美元）",
}

# 指标所属维度（Markdown 报告展示用）
TARGET_DIMENSIONS = {
    "success_rate": "任务完成",
    "avg_duration_seconds": "协同效率",
    "avg_react_loops": "协同效率",
    "keyword_coverage_rate": "内容质量",
    "memory_hit_rate": "记忆效用",
    "avg_quality_score": "内容质量（Judge）",
    "guard_pass_rate": "安全",
    "avg_cost_usd": "成本",
}


def compute_keyword_coverage(text: str, keywords: Iterable[str]) -> float:
    """单个用例的关键词覆盖率：命中关键词数 / 总关键词数（0~1）。

    教学要点：这是最朴素的“关键词命中”——按子串匹配报告全文
    （大小写不敏感），用于衡量报告是否覆盖了用户关心的要点。
    """
    keywords = [k for k in keywords if k]
    if not keywords:
        return 0.0
    lowered = (text or "").lower()
    hit = sum(1 for k in keywords if k.lower() in lowered)
    return round(hit / len(keywords), 4)


def count_react_actions(logs: Iterable[dict]) -> int:
    """统计 ReAct 循环次数：= 该任务的全部 Action 类日志条数。

    教学要点：每次 Action 代表一次“思考 → 行动 → 观察”循环，
    task_logs 表里 log_type='Action' 的条数即循环次数（评测用）。
    """
    return sum(1 for log in logs or [] if log.get("log_type") == "Action")


# 行内引用 [n]（如 [1]、[12]）；参考资料章节标题；```chart 图表块
_CITATION_RE = re.compile(r"\[\d+\]")
_CHART_RE = re.compile(r"```chart", re.IGNORECASE)


def check_assertions(report_text: str, assertions: dict | None) -> list[str]:
    """结构化断言：把"报告整体好不好"细化成"关键结构在不在"。

    背景：质量分只能反映"整体变差"，无法直接定位"引用消失了"这类结构性回归
    （真实踩坑：步骤超时导致引用归 0，质量分仅微降）。断言直接命中结构。

    支持的断言（全部可选）：
    - min_citations      行内引用 [n] 最少条数
    - require_references 必须包含「参考资料」章节
    - min_charts         ```chart 图表块最少个数

    :return: 未通过的断言描述列表（空列表 = 全部通过）
    """
    if not assertions:
        return []
    text = report_text or ""
    failures: list[str] = []
    min_citations = assertions.get("min_citations")
    if min_citations is not None:
        actual = len(_CITATION_RE.findall(text))
        if actual < int(min_citations):
            failures.append(f"行内引用 {actual} 条 < 断言下限 {min_citations}")
    if assertions.get("require_references") and "参考资料" not in text:
        failures.append("缺少「参考资料」章节")
    min_charts = assertions.get("min_charts")
    if min_charts is not None:
        actual = len(_CHART_RE.findall(text))
        if actual < int(min_charts):
            failures.append(f"图表块 {actual} 个 < 断言下限 {min_charts}")
    return failures


def compute_p95(values: Iterable[float]) -> float:
    """P95 耗时：排序后取第 95 百分位（不足 20 个样本时放宽取最大）。

    教学要点：P95 比平均值更能反映“尾部慢任务”体验；
    小样本下退化为最大值，避免空洞的插值结果。
    """
    values = sorted(float(v) for v in values)
    if not values:
        return 0.0
    idx = max(0, math.ceil(len(values) * 0.95) - 1)
    return round(values[min(idx, len(values) - 1)], 2)


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return round(sum(values) / len(values), 2) if values else 0.0


def _rate(numerator: int, denominator: int) -> float:
    """比率（0~1，四舍五入 4 位）；分母为 0 时返回 0。"""
    return round(numerator / denominator, 4) if denominator else 0.0


def compute_metrics(results: list[dict]) -> dict:
    """汇总全部用例结果 → 5 项指标实测值 + 达标判定。

    :param results: runner 收集的用例结果列表，每个元素含：
        success(bool) / duration_seconds(float) / react_loops(int) /
        keyword_coverage(float) / memory_hits(int) / topic(str)
    :return: {summary, targets, met} —— summary 为实测值汇总，
        targets 为目标值，met 为逐项是否达标（前端雷达图与状态标签数据源）
    """
    total = len(results)
    success = [r for r in results if r.get("success")]
    success_rate = _rate(len(success), total)
    # 平均耗时按“成功任务”口径（失败的用例耗时无意义，PRD：总耗时/成功任务数）
    durations = [r["duration_seconds"] for r in success if r.get("duration_seconds") is not None]
    avg_duration = _avg(durations)
    p95_duration = compute_p95(durations)
    loops = _avg([r.get("react_loops", 0) for r in results])
    coverage = _avg([r.get("keyword_coverage", 0.0) for r in results])
    memory_hit_rate = _rate(sum(1 for r in results if r.get("memory_hits", 0) > 0), total)
    # 维度三：报告质量分（仅统计 Judge 成功打分的用例；未启用 Judge 时为空）
    quality_scores = [
        float(r["quality_score"]) for r in success if r.get("quality_score") is not None
    ]
    avg_quality = _avg(quality_scores)
    # 维度三：提示注入防护通过率（仅统计 guard 用例；无 guard 用例视为不适用，取 1.0）
    guard_cases = [r for r in results if r.get("kind") == "guard"]
    guard_passed = sum(1 for r in guard_cases if r.get("guard_pass"))
    guard_pass_rate = _rate(guard_passed, len(guard_cases)) if guard_cases else 1.0
    # 维度三：成本（来自 task_events 汇总；失败用例同样计入实际开销）
    costs = [float(r.get("cost_usd") or 0.0) for r in results]
    total_cost = round(sum(costs), 6)
    avg_cost = round(total_cost / total, 6) if total else 0.0
    summary = {
        "total_cases": total,
        "success_cases": len(success),
        "failed_cases": total - len(success),
        "success_rate": success_rate,
        "avg_duration_seconds": avg_duration,
        "p95_duration_seconds": p95_duration,
        "avg_react_loops": loops,
        "keyword_coverage_rate": coverage,
        "memory_hit_rate": memory_hit_rate,
        "avg_quality_score": avg_quality,
        "quality_scored_cases": len(quality_scores),
        "guard_case_count": len(guard_cases),
        "guard_pass_rate": guard_pass_rate,
        "avg_cost_usd": avg_cost,
        "total_cost_usd": total_cost,
    }
    met = {
        "success_rate": success_rate >= TARGETS["success_rate"],
        "avg_duration_seconds": avg_duration <= TARGETS["avg_duration_seconds"],
        "avg_react_loops": loops <= TARGETS["avg_react_loops"],
        "keyword_coverage_rate": coverage >= TARGETS["keyword_coverage_rate"],
        "memory_hit_rate": memory_hit_rate >= TARGETS["memory_hit_rate"],
        "avg_quality_score": bool(quality_scores) and avg_quality >= TARGETS["avg_quality_score"],
        "guard_pass_rate": guard_pass_rate >= TARGETS["guard_pass_rate"],
        "avg_cost_usd": avg_cost <= TARGETS["avg_cost_usd"],
    }
    return {"summary": summary, "targets": TARGETS, "met": met}


def build_markdown(
    eval_id: str,
    results: list[dict],
    metrics_data: dict,
    regression: dict | None = None,
    dataset: str = "standard",
) -> str:
    """生成 Markdown 可视化总结（含指标总览表 + 用例明细表 + 回归门禁）。

    评测报告以 JSON 落库（结构化数据源），同时生成人类可读的
    Markdown 版本 —— 前端评测中心可直接渲染或下载，替代早期
    规划的“独立 JSON 文件目录”方案。
    """
    summary = metrics_data["summary"]
    lines = [
        "# SmartBrief 自动化评测报告",
        "",
        f"- 评测 ID：`{eval_id}`",
        f"- 数据集：`{dataset}`",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例数：{summary['total_cases']}（成功 {summary['success_cases']} / 失败 {summary['failed_cases']}）",
        "",
    ]
    if regression:
        gate = regression.get("gate")
        gate_text = {"pass": "✅ 通过", "fail": "❌ 未通过", "skipped": "⏭ 无基线（跳过）"}.get(gate, gate)
        lines.append(f"- 回归门禁：{gate_text}（基线 `{regression.get('baseline_eval_id') or '无'}`）")
        blockers = regression.get("blockers") or []
        if blockers:
            lines.append(f"- 阻断项：{'；'.join(blockers)}")
        lines.append("")

    lines += [
        "## 一、指标总览",
        "",
        "| 维度 | 指标 | 实测值 | 目标值 | 达标 |",
        "| --- | --- | --- | --- | --- |",
    ]
    fmt = {
        "success_rate": lambda v: f"{v * 100:.1f}%",
        "avg_duration_seconds": lambda v: f"{v:.1f}s",
        "avg_react_loops": lambda v: f"{v:.1f} 次",
        "keyword_coverage_rate": lambda v: f"{v * 100:.1f}%",
        "memory_hit_rate": lambda v: f"{v * 100:.1f}%",
        "avg_quality_score": lambda v: f"{v:.2f} 分",
        "guard_pass_rate": lambda v: f"{v * 100:.1f}%",
        "avg_cost_usd": lambda v: f"${v:.4f}",
    }
    for key in TARGETS:
        target = TARGETS[key]
        met = metrics_data["met"][key]
        detail = "" if key != "avg_quality_score" else f"（已评分 {summary.get('quality_scored_cases', 0)} 例）"
        lines.append(
            f"| {TARGET_DIMENSIONS[key]} | {TARGET_LABELS[key]}{detail} | {fmt[key](summary[key])} | "
            f"{fmt[key](target)} | {'✅' if met else '❌'} |"
        )
    lines += [
        "",
        "## 二、用例明细",
        "",
        "| # | 主题 | 状态 | 耗时(s) | ReAct次数 | 关键词覆盖率 | 记忆命中 | 质量分 | 成本($) | 类型 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        quality = r.get("quality_score")
        quality_text = "—" if quality is None else f"{float(quality):.2f}"
        kind = r.get("kind") or "standard"
        if kind == "guard":
            type_text = "🛡 防护通过" if r.get("guard_pass") else "🛡 防护失败"
        else:
            type_text = "标准"
        lines.append(
            f"| {r.get('case_index', '-')} | {r.get('topic', '-')} | "
            f"{'✅ 成功' if r.get('success') else '❌ 失败'} | "
            f"{r.get('duration_seconds', '-')} | {r.get('react_loops', '-')} | "
            f"{r.get('keyword_coverage', 0) * 100:.1f}% | {r.get('memory_hits', 0)} | "
            f"{quality_text} | {float(r.get('cost_usd') or 0.0):.4f} | {type_text} |"
        )
    lines += ["", "> 评测由数据集批量驱动，指标口径见后端 `evaluation/metrics.py`。", ""]
    return "\n".join(lines)