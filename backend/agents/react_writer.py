"""ReAct Writer（第 24 节 / 第 26 节）：Thought→Action(llm_gen)→Observation 循环。

对单个 writing 步骤调用 LLM 生成章节草稿（Action = llm_gen），
将 Thought/Action/Observation 追加到 react_logs。与 Researcher 的区别：
Writer 的行动是「调用 LLM 生成文本」而非调用外部搜索工具。

第 26 节增强：撰写前检索 RAG 知识库的**术语表**（rag_collection，terminology 类），
把相关术语定义注入 Prompt，提升报告专业度（如 YoY、渗透率等）。
"""
import asyncio
import json
import re

from llm.client import chat
from rag import store as rag_store
from security import UNTRUSTED_GUARD_NOTICE, wrap_untrusted

SYSTEM_PROMPT = """你是市场调研报告撰写员。基于给定的研究数据与撰写子任务，输出对应的 Markdown 章节草稿。

要求：
- 直接输出正文内容（Markdown），不要输出代码块包裹
- 引用研究数据中的关键数字与事实（数据不足时明确标注「数据待补充」）
- 行内引用：涉及关键数字、事实或结论时，在句末标注来源编号（如 [1]、[2]），
  编号必须来自「可引用的来源列表」，不得编造编号或来源
- 图表：当研究数据中存在可对比或有趋势的真实数字（如同一批来源中的多组数据）时，
  可在合适位置插入一个 ```chart 代码块，内容为合法 JSON 的 ECharts option
  （仅使用 title / tooltip / xAxis / yAxis / series 等基础字段）；
  图表数字必须来自研究数据，禁止编造；没有可靠数字时不要插入图表
- 结构清晰，使用二级/三级标题组织
- 涉及专业指标时，准确使用参考术语定义中的表述（如 YoY、渗透率等）
- 知识库提供的专业材料（行业模板、上传资料）中的口径、数据与框架可优先遵循并引用
""" + UNTRUSTED_GUARD_NOTICE


def collect_sources(research_data: dict) -> list[dict]:
    """汇总各研究步骤的来源列表：按步骤号升序、按 URL 去重、统一编号（供 [n] 引用）。

    编号在这里集中分配（而非 Researcher 侧），保证 Writer 看到的编号
    与 Scheduler 追加的「参考资料」章节编号完全一致。

    :param research_data: {"step_1": {"observations": [...], "sources": [...]}, ...}
    :return: [{"id": 1, "title", "url", "snippet", "content"}, ...]
    """
    def _order(key) -> int:
        m = re.match(r"step_(\d+)", str(key))
        return int(m.group(1)) if m else 9999

    sources: list[dict] = []
    seen: set[str] = set()
    for key in sorted((research_data or {}).keys(), key=_order):
        value = research_data[key]
        if not isinstance(value, dict):
            continue
        for item in value.get("sources") or []:
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "id": len(sources) + 1,
                    "title": str(item.get("title") or "").strip() or url,
                    "url": url,
                    "snippet": str(item.get("snippet") or "").strip(),
                    "content": str(item.get("content") or "").strip(),
                }
            )
    return sources


def _summarize_research(research_data: dict) -> str:
    """把研究数据压缩成供 Writer 引用的 Markdown 摘要。

    为什么要压缩？
    1. 研究数据是原始 JSON（多轮搜索的完整返回），直接塞给 LLM 会爆上下文
    2. 摘要按步骤分组（### step_1 / step_2 ...），让 LLM 清楚每条数据来自哪一步
    3. 每个关键字段限 300 字，防止上下文爆炸

    :param research_data: {"step_1": {"observations": [...]}, ...}
    :return: 压缩后的 Markdown 摘要文本
    """
    if not research_data:
        return "（暂无研究数据）"
    parts = []
    for key, value in research_data.items():         # 遍历每个步骤的研究结果
        parts.append(f"### {key}")                   # 用标题区分步骤
        if isinstance(value, dict) and "observations" in value:
            for obs in value["observations"]:        # 展开该步骤下的每条搜索结果
                parts.append(f"- 关键词 {obs.get('query', '')}：{json.dumps(obs.get('result', ''), ensure_ascii=False)[:300]}")
        else:
            parts.append(f"- {json.dumps(value, ensure_ascii=False)[:300]}")   # 兜底：直接截断
    # 追加「可引用的来源列表」：编号供报告行内引用 [n]（与文末参考资料一致）；
    # 附抓取的网页正文摘录，让 Writer 拿到真实数字而非只有搜索摘要。
    sources = collect_sources(research_data)
    if sources:
        parts.append("### 可引用的来源列表（行内引用请使用这里的编号，如 [1]）")
        for s in sources:
            parts.append(f"[{s['id']}] {s['title']} | {s['url']} | 摘要：{s['snippet'][:200]}")
            if s.get("content"):
                parts.append(f"    正文摘录：{s['content'][:800]}")
    return "\n".join(parts)


async def _retrieve_terms(task: str) -> list:
    """（第 26 节）检索与撰写任务相关的 RAG 术语定义（线程池包装，便于测试 patch）。

    :return: 术语定义文本列表；RAG 不可用时返回 []（不影响撰写）
    """
    try:
        return await asyncio.to_thread(rag_store.search_rag_terms, task)
    except Exception:
        return []


async def _retrieve_materials(task: str) -> list[dict]:
    """检索 RAG 专业材料（行业模板 / 上传资料，非术语类），供撰写引用真实口径与数据。

    与 _retrieve_terms 同理的容错包装：RAG 不可用时返回 []（不影响撰写）。

    :return: [{"content", "source", "category", "score"}, ...]
    """
    try:
        hits = await asyncio.to_thread(rag_store.search_rag, task, 3)
    except Exception:
        return []
    return [
        h for h in hits
        if h.get("category") != "terminology" and h.get("score", 0) >= 0.35
    ]


# 报告 → Skill 闭环（复用侧）：撰写时自动挂载已沉淀的写作技能
MAX_MOUNTED_SKILLS = 2        # 每次撰写最多挂载技能数（控制 prompt 体积）
SKILL_BODY_LIMIT = 1500       # 单个技能正文注入上限（截断）


def _mount_writing_skills() -> list:
    """挑出可挂载的写作技能：已启用 + 非内置 + category=writing（沉淀技能的落点分类）。

    同步无 LLM 调用、纯注册表查询，不做 try 包裹 —— 注册表异常属于程序错误，应暴露。
    """
    from skills import registry

    mounted = []
    for skill in registry.registry.get_all():
        if skill.builtin or not skill.enabled or skill.category != "writing":
            continue
        body = (getattr(skill, "skill_md", "") or "").strip()
        if not body:
            continue
        mounted.append({"name": skill.name, "display_name": skill.display_name,
                        "body": body[:SKILL_BODY_LIMIT]})
        if len(mounted) >= MAX_MOUNTED_SKILLS:
            break
    return mounted


async def react_writer_run(task: str, step_id: int, logs: list, research_data: dict) -> str:
    """对单个 writing 步骤执行 ReAct 循环（行动 = llm_gen）。

    与 Researcher 的区别：Writer 的"行动"不是调用外部搜索工具，
    而是直接调用 LLM 生成章节文本（llm_gen），随后把生成结果作为 Observation。

    执行步骤：
    1. 记 Thought：开始撰写 + Action：llm_gen
    2. 把研究数据压缩成摘要，拼进 user prompt
    3. 调 LLM 生成章节 Markdown
    4. LLM 失败 → 返回占位文本（不中断整个任务）
    5. 记 Observation：已生成章节草稿

    :param task: 该步骤的撰写子任务
    :param step_id: 对应 plan 步骤 id
    :param logs: 共享 react_logs 列表（原地追加）
    :param research_data: 已收集的研究数据（供引用）
    :return: 生成的章节 Markdown 文本
    """
    # ---------- 第 26 节：撰写前检索 RAG 术语表 ----------
    term_texts: list = await _retrieve_terms(task)
    # ---------- 报告质量增强：检索 RAG 专业材料（行业模板/上传资料）----------
    materials: list[dict] = await _retrieve_materials(task)
    if materials:
        logs.append({
            "type": "Thought",
            "content": f"RAG 检索到 {len(materials)} 条专业材料："
                       + "；".join((m.get("source") or "未知来源") for m in materials),
            "step_id": step_id,
        })

    # 日志记录 RAG 检索结果（便于前端观察术语注入情况）
    if term_texts:
        logs.append({
            "type": "Thought",
            "content": f"RAG 检索到 {len(term_texts)} 条相关术语：{'；'.join(term_texts)}",
            "step_id": step_id,
        })
    else:
        logs.append({
            "type": "Thought",
            "content": "RAG 检索：未找到相关术语定义",
            "step_id": step_id,
        })

    # ---------- 报告 → Skill 闭环：挂载已沉淀的写作技能 ----------
    mounted_skills = _mount_writing_skills()
    if mounted_skills:
        logs.append({
            "type": "Thought",
            "content": f"已挂载写作技能：{'、'.join(s['display_name'] for s in mounted_skills)}",
            "step_id": step_id,
        })

    logs.append({"type": "Thought", "content": f"开始撰写：{task}", "step_id": step_id})
    # Action 日志：llm_gen（检索到术语时附带提示，便于前端观察 RAG 生效）
    action_content = "llm_gen(query=撰写本章节草稿)"
    if term_texts:
        action_content += f"（已注入 RAG 术语 {len(term_texts)} 条）"
    logs.append({"type": "Action", "content": action_content, "step_id": step_id})

    # 压缩研究数据 → 作为 Prompt 素材（避免上下文爆炸）
    # 维度二：研究数据源自联网搜索、术语源自 RAG 检索，均为外部不可信内容，
    # 进 Prompt 前统一包注入防护边界（边界内仅为数据，指令不执行）
    research_summary = wrap_untrusted(_summarize_research(research_data), source="web_search")
    # 术语定义 → 追加进 Prompt（写手据此用词更专业）
    term_block = ""
    if term_texts:
        term_block = "\n\n可参考的术语定义（撰写相关指标时请准确使用）：\n" + wrap_untrusted(
            "\n".join(f"- {t}" for t in term_texts), source="rag"
        )
    # 专业材料（行业模板/上传资料）→ 追加进 Prompt（口径、数据与框架可优先遵循）
    material_block = ""
    if materials:
        material_block = "\n\n可参考的专业材料（来自知识库；其中的口径、数据与框架可优先遵循）：\n" + wrap_untrusted(
            "\n".join(
                f"- （来源：{m.get('source') or '未知'}）{str(m.get('content') or '')[:400]}"
                for m in materials
            ),
            source="rag",
        )
    # 已沉淀的写作技能 → 追加进 Prompt（章节框架与写作规范优先遵循）
    skill_block = ""
    if mounted_skills:
        skill_block = "\n\n已挂载的写作技能（优先遵循其章节框架、口径与引用规范）：\n" + wrap_untrusted(
            "\n\n".join(f"### 技能：{s['display_name']}\n{s['body']}" for s in mounted_skills),
            source="skill",
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"撰写子任务：{task}\n\n可引用的研究数据：\n{research_summary}{term_block}{material_block}{skill_block}",
        },
    ]
    try:
        resp = await chat(messages)                  # 行动：调用 LLM 生成文本
        content = resp.choices[0].message.content or ""
    except Exception as exc:
        # LLM 失败：返回占位文本 + Observation 记录（任务继续跑，不中断）
        content = f"（本节撰写失败：{exc}）"
        logs.append({"type": "Observation", "content": f"llm_gen 调用失败：{exc}", "step_id": step_id})
        return content

    logs.append(
        {
            "type": "Observation",
            "content": f"已生成章节草稿（{len(content)} 字），写入 draft_content",
            "step_id": step_id,
        }
    )
    return content                                    # 章节文本返回给 Scheduler 拼接
