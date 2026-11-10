"""ReAct Researcher（第 24 节）：Thought→Action(web_search)→Observation 循环。

对单个 research 步骤执行最多 MAX_REACT_STEPS 轮 ReAct：
每轮由 LLM 输出行动决策（JSON），执行 MCP web_search 工具得到 Observation，
将 Thought/Action/Observation 全部追加到 react_logs（前端日志流展示）。
"""
import json
import re

from llm.client import chat
from mcp_client.mcp_client import mcp_manager
from security import UNTRUSTED_GUARD_NOTICE, wrap_untrusted

MAX_REACT_STEPS = 3

# 报告质量增强（引用源 + 数据支撑）：
# 搜索返回有效链接后，自动抓取排名靠前网页的正文（每步骤最多 MAX_FETCH_ATTEMPTS_PER_STEP
# 次尝试，控制 ReAct 次数与 token 成本）；robots.txt 拦截常见（知乎/行业站都挡），
# 被挡时换下一个来源再试；正文摘录挂到抓取成功的来源上，供 Writer 引用真实数字。
FETCH_CONTENT_LIMIT = 3000   # 单次抓取正文长度上限（fetch 工具 max_length）
FETCH_STORE_LIMIT = 1500     # 存入 research_data 的正文摘录上限
MAX_SOURCES_PER_SEARCH = 3   # 单次搜索最多结构化保留的来源数
MAX_FETCH_ATTEMPTS_PER_STEP = 3  # 每研究步骤最多抓取尝试次数（被挡换下一个来源）

SYSTEM_PROMPT = """你是市场调研研究员。围绕给定的研究子任务，通过 web_search 工具收集信息。

每轮你只输出一个 JSON 决策（不要输出任何其他文字或 Markdown）：
- 需要搜索时：{"action": "web_search", "query": "搜索关键词"}
- 信息已足够、可以结束本轮研究时：{"action": "done"}

搜索词策略（关键）：
- 行业研究类查询优先使用「英文关键词 + 数据口径 + 年份」，例如
  "portable power station market size North America 2025"、"energy storage shipments 2025"
  —— 英文查询更容易命中行业研究报告、机构新闻稿与统计数据页
- 中文查询时附上「报告 / 白皮书 / 出货量 / 市场规模」等限定词，避免命中词典、百科词条页
- 避免把整句子任务原样作为搜索词
系统会在你搜索后自动抓取排名最高网页的正文供你参考；正文中出现具体数字与来源时，可据此决策 done。
""" + UNTRUSTED_GUARD_NOTICE


def parse_decision(content: str, fallback_query: str) -> dict:
    """解析 LLM 的行动决策 JSON（容错处理代码块 / 多余文字）。

    决策格式：{"action": "web_search", "query": "..."} 或 {"action": "done"}
    与 parse_plan 同理：LLM 输出不可靠，需要逐级容错。
    解析失败时的兜底：默认返回一次 web_search（用 fallback_query）。

    :param content: LLM 输出文本
    :param fallback_query: 解析失败时使用的默认搜索词（通常是子任务原文）
    """
    text = (content or "").strip()
    # 1) 剥掉 ```json ... ``` 代码块
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)                 # 直接解析
    except json.JSONDecodeError:
        # 截取 { 到 } 的区间（剥掉前后废话）再试
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return {"action": "web_search", "query": fallback_query}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {"action": "web_search", "query": fallback_query}   # 彻底失败 → 默认搜索
    if not isinstance(data, dict):
        return {"action": "web_search", "query": fallback_query}
    action = data.get("action")
    if action == "done":
        return {"action": "done"}               # 决策：信息足够，结束研究
    # 决策：搜索。query 为空时用 fallback_query 兜底
    query = str(data.get("query", "")).strip() or fallback_query
    return {"action": "web_search", "query": query}


def _shorten(text: str, limit: int = 800) -> str:
    """截断 Observation 内容，避免日志过大（ReAct 日志要落库+展示）。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字）"


async def _run_search(query: str, step_id: int, logs: list) -> dict:
    """执行一次 web_search（Action + Observation），返回结果。

    教学要点：一次搜索 = Action 日志（调了哪个工具）+ Observation 日志（工具返回什么）。
    失败时不抛异常，而是把错误信息写入 Observation 并返回 {"error": ...}，
    由上层决定如何降级 —— 这是"工具调用失败不中断流程"的健壮性设计。

    :param query: 搜索关键词
    :param step_id: 归属的 plan 步骤 id
    :param logs: 共享 react_logs（原地追加 Action/Observation）
    :return: MCP web_search 的返回结果；失败时为 {"error": "..."}
    """
    logs.append({"type": "Action", "content": f"web_search(query={query!r})", "step_id": step_id})
    try:
        result = await mcp_manager.call_tool("web_search", {"query": query})   # 调 MCP 工具
        obs_text = _shorten(json.dumps(result, ensure_ascii=False))
        logs.append({"type": "Observation", "content": obs_text, "step_id": step_id})
        return result
    except Exception as exc:
        # 工具异常：记录 Observation，返回带 error 的结果（不抛异常）
        obs_text = f"web_search 调用失败：{type(exc).__name__}: {exc}"
        logs.append({"type": "Observation", "content": obs_text, "step_id": step_id})
        return {"error": f"{type(exc).__name__}: {exc}"}


def _extract_sources(result: dict, query: str) -> list[dict]:
    """从 web_search 返回中提取结构化来源列表（title/url/snippet/query）。

    web_search 返回 {"source": "bing"|"mock", "results": [{"title","url","snippet"}...]}；
    只保留 http(s) 链接，单次搜索最多 MAX_SOURCES_PER_SEARCH 条（控制 research_data 体积）。
    """
    items = result.get("results") if isinstance(result, dict) else None
    sources: list[dict] = []
    for item in items or []:
        url = str(item.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        sources.append(
            {
                "title": str(item.get("title") or "").strip() or url,
                "url": url,
                "snippet": str(item.get("snippet") or "").strip(),
                "query": query,
            }
        )
        if len(sources) >= MAX_SOURCES_PER_SEARCH:
            break
    return sources


async def _run_fetch(url: str, step_id: int, logs: list) -> str:
    """抓取网页正文（Action + Observation），返回正文文本；失败返回 ""。

    与 _run_search 同理：失败不抛异常，把错误写进 Observation 降级，
    保证「抓不到正文」只影响证据丰富度，不会中断整个研究步骤。
    """
    logs.append({"type": "Action", "content": f"fetch(url={url!r})", "step_id": step_id})
    try:
        result = await mcp_manager.call_tool("fetch", {"url": url, "max_length": FETCH_CONTENT_LIMIT})
        text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
        if not text:
            raise ValueError("正文为空")
        logs.append(
            {"type": "Observation", "content": _shorten(f"网页正文抓取成功（{len(text)} 字）：{text}", 600), "step_id": step_id}
        )
        return text
    except Exception as exc:
        logs.append({"type": "Observation", "content": f"fetch 调用失败：{type(exc).__name__}: {exc}", "step_id": step_id})
        return ""


async def react_researcher_run(task: str, step_id: int, logs: list) -> dict:
    """对单个 research 步骤执行 ReAct 循环（最多 MAX_REACT_STEPS 轮）。

    循环结构（每轮三拍）：
        拍1 Thought（思考）：LLM 输出决策文本
        拍2 Action（行动）：决策是 web_search → 执行 _run_search
        拍3 Observation（观察）：把搜索结果追加到 messages，供下一轮思考

    结束条件（满足其一）：
        1. LLM 决策为 done（信息已足够）
        2. 用完 MAX_REACT_STEPS 轮（防止无限循环）
        3. 搜索调用失败（带 error 直接 break）

    :param task: 该步骤的子任务描述
    :param step_id: 对应 plan 步骤 id（日志归属）
    :param logs: 共享 react_logs 列表（原地追加）
    :return: {"observations": [web_search 结果列表], "sources": [结构化来源]}
        sources 仅在有有效链接时返回；首个抓取成功的来源附带网页正文摘录（content 字段）。
    """
    observations = []                                   # 收集本轮所有搜索结果
    sources: list[dict] = []                            # 结构化来源（供报告 [n] 引用）
    fetch_attempts = 0                                  # 每步骤抓取尝试计数（成本控制）
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"研究子任务：{task}"},
    ]
    for round_no in range(1, MAX_REACT_STEPS + 1):
        # ---------- 拍1 Thought：让 LLM 思考并输出决策 ----------
        try:
            resp = await chat(messages)
            decision_text = resp.choices[0].message.content or ""
        except Exception as exc:
            # LLM 决策失败：记录 Thought 后降级为直接搜索一次
            logs.append(
                {"type": "Thought", "content": f"第 {round_no} 轮：LLM 决策失败（{exc}），降级为直接搜索", "step_id": step_id}
            )
            decision = {"action": "web_search", "query": task}
            # LLM 不可用，降级搜索一次即结束（避免空转重试）
            result = await _run_search(task, step_id, logs)
            observations.append({"query": task, "result": result})
            break
        else:
            logs.append(
                {"type": "Thought", "content": f"第 {round_no} 轮：{_shorten(decision_text, 200)}", "step_id": step_id}
            )
            decision = parse_decision(decision_text, fallback_query=task)   # 容错解析决策

        # ---------- 拍2/3 Action+Observation：执行搜索 ----------
        if decision.get("action") == "done":
            logs.append({"type": "Thought", "content": "信息已足够，结束本轮研究", "step_id": step_id})
            break                                    # 结束条件1：LLM 主动停止
        query = decision.get("query") or task        # 搜索词（解析失败时兜底为子任务）

        # 执行一次搜索：Action 日志 + Observation 日志 + 返回结果
        result = await _run_search(query, step_id, logs)
        if "error" in result:
            break                                    # 结束条件3：搜索失败
        observations.append({"query": query, "result": result})

        # ---------- 报告质量增强：结构化来源 + 抓取来源正文（被挡换下一家）----------
        # 每轮搜索都收集来源（去重与编号在 Writer 侧 collect_sources 统一处理）；
        # 正文抓取每步骤最多 MAX_FETCH_ATTEMPTS_PER_STEP 次，控制 ReAct 次数与成本。
        new_sources = _extract_sources(result, query)
        sources.extend(new_sources)
        fetched_body = ""
        if result.get("source") != "mock":
            for src in new_sources:
                if fetched_body or fetch_attempts >= MAX_FETCH_ATTEMPTS_PER_STEP:
                    break
                fetch_attempts += 1
                body = await _run_fetch(src["url"], step_id, logs)
                if body:
                    fetched_body = body
                    # 正文摘录挂到抓取成功的来源上（Writer 据此写出有真实数字支撑的段落）
                    src["content"] = body[:FETCH_STORE_LIMIT]

        # 携带上一轮 Observation 继续思考（限两轮，防止上下文过长）
        # 维度二：搜索结果/网页正文是外部不可信内容，包注入防护边界后再进 Prompt
        if round_no < MAX_REACT_STEPS:
            safe_obs = wrap_untrusted(
                _shorten(json.dumps(result, ensure_ascii=False), 300), source="web_search"
            )
            parts = [f"上一轮搜索结果：{safe_obs}"]
            if fetched_body:
                parts.append(
                    "首个来源网页正文摘录："
                    + wrap_untrusted(_shorten(fetched_body, 600), source="fetch")
                )
            messages.append({"role": "user", "content": "\n\n".join(parts)})

    output = {"observations": observations}          # 该步骤的研究成果
    if sources:
        output["sources"] = sources                  # 结构化来源（有有效链接时才带该键）
    return output
