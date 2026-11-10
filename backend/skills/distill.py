"""技能沉淀（报告 → Skill 闭环）：把一份完成的调研报告抽象为可复用的写作技能。

闭环叙事：系统用久了，好的写作模式应该能沉淀下来 ——
「使用 → 审核通过 → 一键沉淀 → 下次撰写自动挂载 → 越用越聪明」。

与长期记忆的区别：长期记忆复用「数据」（上次查到什么），
技能沉淀复用「方法」（这类报告该怎么写：章节框架、术语口径、引用规范）。
"""
import json
import logging
import re
from uuid import uuid4

from llm.client import chat
from skills.skills_manager import manager as skill_manager

logger = logging.getLogger(__name__)

DISTILL_PROMPT = """你是写作方法论提炼专家。给你一份已完成的调研报告及其主题，
请抽象出「这类报告的写作技能」，供以后撰写同类报告时复用。

只输出一个 JSON（不要输出任何其他文字或 Markdown 代码块）：
{
  "name": "技能英文名（小写字母/数字/中划线，如储能市场分析 → storage-market-analysis）",
  "display_name": "技能中文名",
  "description": "一句话说明适用场景（30 字内）",
  "body": "技能正文（Markdown），必须包含：\\n## 适用场景\\n## 章节框架\\n## 数据与口径规范\\n## 引用规范"
}

提炼要求：
- 抽结构不抄内容：正文里不得出现本报告的具体数字/结论，只保留可复用的框架与写法
- 章节框架要给出推荐的标题层级与每章要点
- 数据与口径规范写明关键指标的定义与表述方式（如 YoY、渗透率、市场规模口径）
- 引用规范写明行内引用 [n] 与文末参考资料的标注要求
"""


def _safe_skill_name(raw: str) -> str:
    """把 LLM 给的名称清洗为合法技能名；失败则回退为 report_skill_<随机后缀>。"""
    slug = re.sub(r"[^a-z0-9-]+", "-", (raw or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:48]
    if not slug or not re.match(r"^[a-zA-Z0-9]", slug):
        slug = f"report-skill-{uuid4().hex[:6]}"
    # 撞名时追加随机后缀
    if skill_manager.get_skill(slug) is not None:
        slug = f"{slug}-{uuid4().hex[:4]}"
    return slug


def _parse_distill_result(text: str) -> dict:
    """解析 LLM 的提炼结果 JSON（容错代码块/多余文字）。"""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("LLM 提炼结果不是合法 JSON")
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict) or not str(data.get("body") or "").strip():
        raise ValueError("LLM 提炼结果缺少 body 字段")
    return data


async def distill_skill_from_task(task: dict) -> dict:
    """把完成的任务报告沉淀为写作技能，注册进技能池并持久化。

    :param task: task_repository.get_task 的返回（需含 topic / final_report）
    :return: 新建技能的 to_dict()
    :raises ValueError: 提炼失败（LLM 输出不可用）
    """
    topic = str(task.get("topic") or "")
    report = str(task.get("final_report") or task.get("draft_content") or "")
    # 报告截断：提炼只需结构与写法，不需要全文（控制 token 成本）
    report_excerpt = report[:6000]
    resp = await chat([
        {"role": "system", "content": DISTILL_PROMPT},
        {"role": "user", "content": f"报告主题：{topic}\n\n报告全文（可能截断）：\n{report_excerpt}"},
    ])
    data = _parse_distill_result(resp.choices[0].message.content or "")
    name = _safe_skill_name(str(data.get("name") or ""))
    skill = skill_manager.create_skill(
        name=name,
        display_name=str(data.get("display_name") or name),
        description=str(data.get("description") or f"由报告「{topic}」沉淀的写作技能")[:80],
        category="writing",
        skill_md=str(data["body"]),
    )
    logger.info("报告沉淀为技能：task=%s -> skill=%s", task.get("task_id"), name)
    return skill
