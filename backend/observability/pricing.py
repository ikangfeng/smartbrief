"""LLM 成本计量（维度一）：按模型价目表估算单次调用成本（美元）。

价目单位：美元 / 1M tokens（input / output 分开计价）。
默认内置 DeepSeek 官方价（2025 公示价：cache miss 输入 $0.14/M、输出 $0.28/M）；
未知模型回退 default 价（取 GPT-4o-mini 量级），保证成本卡永远有数。

可通过环境变量覆盖（JSON 字符串），便于切换厂商：
    SMARTBRIEF_LLM_PRICING='{"deepseek-chat": {"in": 0.14, "out": 0.28}}'
"""
import json
import os

# 价目表：美元 / 1M tokens
_DEFAULT_PRICING = {
    "default": {"in": 0.15, "out": 0.60},        # 兜底价（GPT-4o-mini 量级）
    "deepseek-chat": {"in": 0.14, "out": 0.28},  # DeepSeek V3（cache miss 输入价）
    "deepseek-reasoner": {"in": 0.55, "out": 2.19},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "gpt-4o": {"in": 2.50, "out": 10.00},
}


def _pricing_table() -> dict:
    """价目表（环境变量覆盖优先，解析失败回退内置表）。"""
    raw = os.getenv("SMARTBRIEF_LLM_PRICING", "").strip()
    if raw:
        try:
            table = json.loads(raw)
            if isinstance(table, dict) and table:
                merged = dict(_DEFAULT_PRICING)
                merged.update(table)
                return merged
        except json.JSONDecodeError:
            pass
    return _DEFAULT_PRICING


def estimate_cost_usd(model: str, tokens_in: int | None, tokens_out: int | None) -> float:
    """估算一次 LLM 调用的成本（美元，保留 8 位小数）。

    :param model: 模型名（取价目表；未知模型用 default 价）
    :param tokens_in: prompt tokens（None 按 0 计）
    :param tokens_out: completion tokens（None 按 0 计）
    """
    table = _pricing_table()
    price = table.get(model) or table["default"]
    tin = max(0, int(tokens_in or 0))
    tout = max(0, int(tokens_out or 0))
    cost = (tin * price.get("in", 0.0) + tout * price.get("out", 0.0)) / 1_000_000
    return round(cost, 8)
