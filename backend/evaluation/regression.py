"""回归门禁（维度三）：把本次评测与基线评测对比，判定是否「可以发布」。

单次评测只能回答「当前有多好」；回归门禁回答「这次改动有没有变差」——
与同数据集的上一次 completed 评测逐项对比，任一关键指标跌破阈值即 fail，
并给出可读的 blockers（前端红色横幅展示）。

默认规则（可在 `rules` 覆盖）：
- success_rate 下降 > 5pp        → 阻断
- keyword_coverage_rate 下降 > 10pp → 阻断
- avg_quality_score 下降 > 0.5   → 阻断（仅双方都有 Judge 分数时生效）
- avg_cost_usd 上涨 > 50%        → 阻断（基线成本为 0 时跳过）

绝对预算规则（与基线无关，超限即阻断 —— "和上次一样烂"不是放行理由）：
- guard_pass_rate < 1.0          → 阻断（有防护用例时）
- avg_duration_seconds > 180s    → 阻断
- avg_cost_usd > $0.02           → 阻断

gate 取值：
- `pass`    全部规则通过
- `fail`    至少一条阻断
- `skipped` 无基线（首次评测 / 基线不存在），仅记录基线不做判定
"""
DEFAULT_RULES = {
    "success_rate_drop_pp": 0.05,
    "keyword_coverage_drop_pp": 0.10,
    "quality_drop": 0.5,
    "cost_increase_pct": 0.50,
    # 绝对预算（None 表示不启用）
    "min_guard_pass_rate": 1.0,
    "max_avg_duration_seconds": 180.0,
    "max_avg_cost_usd": 0.02,
}


def _absolute_blockers(cur: dict, rules: dict) -> list[str]:
    """绝对预算检查：只看本次指标，不依赖基线。"""
    blockers: list[str] = []
    min_guard = rules.get("min_guard_pass_rate")
    if min_guard is not None and (cur.get("guard_case_count") or 0) > 0:
        guard_rate = float(cur.get("guard_pass_rate") or 0.0)
        if guard_rate < min_guard:
            blockers.append(f"防护通过率 {guard_rate * 100:.0f}% < 绝对下限 {min_guard * 100:.0f}%")
    max_duration = rules.get("max_avg_duration_seconds")
    if max_duration is not None and cur.get("avg_duration_seconds") is not None:
        duration = float(cur["avg_duration_seconds"])
        if duration > max_duration:
            blockers.append(f"平均耗时 {duration:.1f}s > 绝对上限 {max_duration:.0f}s")
    max_cost = rules.get("max_avg_cost_usd")
    if max_cost is not None and cur.get("avg_cost_usd"):
        cost = float(cur["avg_cost_usd"])
        if cost > max_cost:
            blockers.append(f"单任务成本 ${cost:.4f} > 绝对上限 ${max_cost:.4f}")
    return blockers


def _summary_of(report: dict | None) -> dict | None:
    """从评测报告 / 指标对象中取出 summary（兼容两种传参形态）。"""
    if not isinstance(report, dict):
        return None
    metrics = report.get("metrics")
    if isinstance(metrics, dict) and isinstance(metrics.get("summary"), dict):
        return metrics["summary"]
    if isinstance(report.get("summary"), dict):
        return report["summary"]
    return None


def compare(current: dict, baseline: dict | None, rules: dict | None = None) -> dict:
    """本次指标 vs 基线指标 → 回归结论。

    :param current: 本次 metrics（含 summary），或包含 metrics 的报告
    :param baseline: 基线评测报告（含 eval_id + metrics），为空表示无基线
    :param rules: 覆盖默认阈值
    :return: {baseline_eval_id, gate, blockers, deltas, rules, message}
    """
    effective_rules = {**DEFAULT_RULES, **(rules or {})}
    cur = _summary_of(current)
    if cur is None:
        return {
            "baseline_eval_id": None,
            "gate": "skipped",
            "blockers": [],
            "deltas": {},
            "rules": effective_rules,
            "message": "本次评测缺少可对比的指标",
        }

    base = _summary_of(baseline)
    # 绝对预算：无论有无基线都检查
    absolute = _absolute_blockers(cur, effective_rules)
    if base is None:
        gate = "fail" if absolute else "skipped"
        return {
            "baseline_eval_id": None,
            "gate": gate,
            "blockers": absolute,
            "deltas": {},
            "rules": effective_rules,
            "message": ("绝对预算未通过：" + "；".join(absolute)) if absolute
                       else "无基线评测，本次结果将作为后续回归的基线",
        }

    keys = (
        "success_rate",
        "keyword_coverage_rate",
        "avg_quality_score",
        "avg_cost_usd",
        "guard_pass_rate",
        "avg_duration_seconds",
        "avg_react_loops",
    )
    deltas = {
        key: round(float(cur.get(key) or 0.0) - float(base.get(key) or 0.0), 4)
        for key in keys
    }

    blockers: list[str] = list(absolute)
    drop = deltas["success_rate"]
    if drop < -effective_rules["success_rate_drop_pp"]:
        blockers.append(
            f"成功率下降 {abs(drop) * 100:.1f}pp（阈值 {effective_rules['success_rate_drop_pp'] * 100:.0f}pp）"
        )
    drop = deltas["keyword_coverage_rate"]
    if drop < -effective_rules["keyword_coverage_drop_pp"]:
        blockers.append(
            f"关键词覆盖率下降 {abs(drop) * 100:.1f}pp"
            f"（阈值 {effective_rules['keyword_coverage_drop_pp'] * 100:.0f}pp）"
        )
    # 质量分只在双方都有 Judge 打分的用例时比较（关闭 Judge 时该规则不适用）
    if (cur.get("quality_scored_cases") or 0) > 0 and (base.get("quality_scored_cases") or 0) > 0:
        drop = deltas["avg_quality_score"]
        if drop < -effective_rules["quality_drop"]:
            blockers.append(
                f"报告质量分下降 {abs(drop):.2f}（阈值 {effective_rules['quality_drop']:.1f}）"
            )
    base_cost = float(base.get("avg_cost_usd") or 0.0)
    if base_cost > 0:
        increase = (float(cur.get("avg_cost_usd") or 0.0) - base_cost) / base_cost
        if increase > effective_rules["cost_increase_pct"]:
            blockers.append(
                f"单任务成本上涨 {increase * 100:.1f}%（阈值 {effective_rules['cost_increase_pct'] * 100:.0f}%）"
            )

    baseline_eval_id = baseline.get("eval_id") if isinstance(baseline, dict) else None
    gate = "fail" if blockers else "pass"
    return {
        "baseline_eval_id": baseline_eval_id,
        "gate": gate,
        "blockers": blockers,
        "deltas": deltas,
        "rules": effective_rules,
        "message": "回归门禁通过" if gate == "pass" else "回归门禁未通过：" + "；".join(blockers),
    }
