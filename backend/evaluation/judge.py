"""LLM-as-Judge：报告质量自动评分（维度三）。

评测只靠「关键词覆盖率」无法判断报告写得好不好 —— 关键词全命中也可能
是堆砌、没有数据支撑。本模块用同一个 LLM 充当评审员，按 4 个维度的
Rubric 给报告打分（1-5 分），产出结构化结果，供评测指标与回归门禁使用。

四个维度：
- structure  结构完整性（是否有清晰的框架：背景 / 市场 / 竞争 / 趋势 / 结论）
- evidence   数据支撑（是否有具体数字、来源、时间口径，而非空泛描述）
- insight    洞察深度（是否有超越常识的判断、差异化结论）
- expression 表达规范（条理清晰、术语准确、无明显语病）

设计要点：
- **不阻断评测**：LLM 调用失败 / 输出无法解析时返回 `score=None + error`，
  runner 照常完成整轮评测，前端把该用例质量分显示为「—」。
- **成本不计入任务**：Judge 调用发生在任务工作流之外，调用前解除
  observability 的任务绑定，避免评审开销混入被评测任务的成本卡。
"""
import json
import logging
import os

from llm import client
from observability import tracing

logger = logging.getLogger(__name__)

# Judge 评审模型：与被测系统解耦（否则「deepseek-chat 评 deepseek-chat」是自评同源，
# 面试/答辩时会被挑战）。通过 backend/.env 的 JUDGE_MODEL 配置；
# 未配置时回退到与主系统相同的模型（保证开箱可用，但评测报告会如实标注模型名）
JUDGE_MODEL = os.getenv("JUDGE_MODEL") or None

DIMENSION_LABELS = {
    "structure": "结构完整性",
    "evidence": "数据支撑",
    "insight": "洞察深度",
    "expression": "表达规范",
}

# 质量分达标线（与 metrics.TARGETS["avg_quality_score"] 保持一致）
QUALITY_PASS_SCORE = 3.5
# 报告过长时截断，避免评审 prompt 无谓膨胀
_MAX_REPORT_CHARS = 6000

_SYSTEM_PROMPT = (
    "你是一名资深市场研究报告评审员。请严格按 Rubric 评审用户提供的报告，"
    "只输出一个 JSON 对象，不要输出任何解释性文字或 Markdown 代码块。"
)

_USER_TEMPLATE = """请评审以下市场研究报告。

研究主题：{topic}

报告正文：
---
{report}
---

评分 Rubric（每个维度 1-5 分，5 分为最佳）：
1. structure（结构完整性）：是否有清晰的框架，覆盖背景 / 市场规模 / 竞争格局 / 趋势 / 结论。
2. evidence（数据支撑）：是否给出具体数字、时间口径，且关键数字有引用源标注（如 [1]）与可核验的来源，而非空泛描述。
3. insight（洞察深度）：是否有超越常识的判断、差异化结论或可执行建议。
4. expression（表达规范）：是否条理清晰、术语准确、无明显语病与重复。

输出 JSON，字段固定如下（score 为四维平均分，保留 1 位小数）：
{{"score": 4.0, "dimensions": {{"structure": 4, "evidence": 4, "insight": 4, "expression": 4}}, "verdict": "pass", "rationale": "一句话说明评分理由"}}
其中 verdict 取 "pass"（score >= 3.5）或 "needs_improvement"。"""


def get_model_name() -> str:
    """当前 Judge 使用的模型名（写入评测报告供溯源；读取失败返回 unknown）。

    优先返回 JUDGE_MODEL（独立的评审模型）；未配置时为主系统模型名。
    """
    try:
        return JUDGE_MODEL or client.get_model()
    except Exception:  # pragma: no cover - 极端环境
        return "unknown"


async def _call_llm(messages: list) -> str:
    """调用 LLM 返回文本内容（测试可 patch 本函数，避免真实网络调用）。"""
    resp = await client.chat(messages, model=JUDGE_MODEL)
    return (getattr(resp.choices[0].message, "content", None) or "").strip()


def _empty(error: str) -> dict:
    """评分不可用时的统一结构（不阻断评测，前端显示「—」）。"""
    return {
        "score": None,
        "dimensions": {},
        "verdict": "error",
        "rationale": "",
        "error": error,
    }


def _clamp_score(value) -> float | None:
    """把分数收敛到 1-5（非法值返回 None）。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    return round(max(1.0, min(5.0, score)), 2)


def parse_judge_response(text: str) -> dict:
    """解析 Judge 输出文本 → 结构化评分（纯函数，便于单测）。

    容错：允许模型输出 ```json 围栏、前后带说明文字（截取首个 { 到末个 }）。
    解析失败返回 `score=None + error`，不抛异常。
    """
    raw = (text or "").strip()
    if not raw:
        return _empty("Judge 返回空响应")

    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return _empty("Judge 输出未包含 JSON 对象")

    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        return _empty(f"Judge 输出 JSON 解析失败：{exc}")

    if not isinstance(data, dict):
        return _empty("Judge 输出不是 JSON 对象")

    score = _clamp_score(data.get("score"))
    if score is None:
        return _empty("Judge 输出缺少合法 score（1-5）")

    dimensions_raw = data.get("dimensions")
    dimensions: dict[str, float] = {}
    if isinstance(dimensions_raw, dict):
        for key in DIMENSION_LABELS:
            value = _clamp_score(dimensions_raw.get(key))
            if value is not None:
                dimensions[key] = value

    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("pass", "needs_improvement"):
        verdict = "pass" if score >= QUALITY_PASS_SCORE else "needs_improvement"

    return {
        "score": score,
        "dimensions": dimensions,
        "verdict": verdict,
        "rationale": str(data.get("rationale") or "").strip()[:400],
        "error": None,
    }


async def judge_report(topic: str, report: str) -> dict:
    """对单篇报告打分。

    :return: {score, dimensions, verdict, rationale, error}
        score 为 None 表示本次评分不可用（LLM 失败 / 解析失败），调用方不应据此阻断评测。
    """
    body = (report or "").strip()
    if not body:
        return _empty("报告为空，无法评分")

    # 评审发生在任务工作流之外：解除任务绑定，避免 Judge 的 LLM span
    # 被记到被评测任务的 task_events（成本卡/时间轴会虚高）
    try:
        tracing.unbind_task()
    except Exception:  # pragma: no cover - 观测层异常绝不影响评分
        pass

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(
                topic=topic or "（未提供主题）",
                report=body[:_MAX_REPORT_CHARS],
            ),
        },
    ]
    try:
        text = await _call_llm(messages)
    except Exception as exc:
        logger.warning("Judge 调用失败（不阻断评测）：%s", exc)
        return _empty(f"Judge 调用失败：{type(exc).__name__}")

    result = parse_judge_response(text)
    if result["score"] is None:
        logger.warning("Judge 输出无法解析（不阻断评测）：%s", result["error"])
    return result
