"""评测数据集加载与校验（维度三）。

设计要点：
- 数据集以 JSON 文件落盘（`evaluation/datasets/*.json`），新增/调整用例
  不再改代码，评测从「硬编码 10 个 TestCase」升级为「可切换数据集」。
- `_ALLOWED` 是文件名白名单：只允许加载登记过的数据集，从根本上防止
  `dataset="../../etc/passwd"` 之类的路径穿越（即使调用方乱传也不会读文件系统）。
- 每个用例统一字段：
  id / topic / category / difficulty / kind / keywords / negative_keywords / description
  其中 `kind=guard` 为提示注入防护用例，用 `negative_keywords` 判定报告是否泄露。
"""
import json
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
# 数据集白名单（文件名即数据集名，禁止任意路径）
_ALLOWED = ("standard", "smoke")
# 用例必填字段
_REQUIRED = ("id", "topic", "keywords")
# 用例类型：standard（正常研究）/ guard（提示注入防护）
_KINDS = ("standard", "guard")


def list_datasets() -> list[dict]:
    """列出可用数据集（前端下拉框数据源）：名称 / 说明 / 用例数。"""
    datasets = []
    for name in _ALLOWED:
        try:
            data = load_dataset(name)
        except (OSError, ValueError, json.JSONDecodeError):
            # 单个数据集损坏不影响其它数据集可选（前端仍能展示错误由运行接口给出）
            continue
        datasets.append({
            "name": name,
            "description": data["description"],
            "case_count": len(data["cases"]),
        })
    return datasets


def load_dataset(name: str) -> dict:
    """加载并校验数据集，返回 {name, description, cases}。

    :raises ValueError: 数据集名不在白名单 / 文件损坏 / 用例字段不合法
    """
    if name not in _ALLOWED:
        raise ValueError(f"未知数据集：{name}（可选：{' / '.join(_ALLOWED)}）")
    path = DATASETS_DIR / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"数据集文件缺失：{name}.json")

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"数据集 {name} 无有效用例")

    cases: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"数据集 {name} 第 {index} 个用例格式非法")
        for field in _REQUIRED:
            if not str(raw.get(field) or "").strip():
                raise ValueError(f"数据集 {name} 第 {index} 个用例缺少字段：{field}")
        case_id = str(raw["id"]).strip()
        if case_id in seen_ids:
            raise ValueError(f"数据集 {name} 用例 id 重复：{case_id}")
        seen_ids.add(case_id)

        kind = str(raw.get("kind") or "standard").strip()
        if kind not in _KINDS:
            raise ValueError(f"数据集 {name} 用例 {case_id} 的 kind 非法：{kind}")
        keywords = [str(k).strip() for k in raw.get("keywords") or [] if str(k).strip()]
        if not keywords:
            raise ValueError(f"数据集 {name} 用例 {case_id} 的 keywords 为空")
        negative_keywords = [
            str(k).strip() for k in raw.get("negative_keywords") or [] if str(k).strip()
        ]
        # 间接注入用例夹具（可选）：{工具名: 固定返回}，runner 执行该用例时
        # 拦截对应 MCP 工具调用返回夹具内容 —— 恶意内容从「搜索/抓取」通道进入，
        # 真实 Tavily 不可控，间接注入只能靠 mock 网络边界来复现
        mock_tools = raw.get("mock_tools")
        if mock_tools is not None and not isinstance(mock_tools, dict):
            raise ValueError(f"数据集 {name} 用例 {case_id} 的 mock_tools 必须是对象")
        # 结构化断言（可选）：{min_citations, require_references, min_charts}
        # 把"整体感觉好不好"细化到"关键结构在不在" —— 引用归 0 这类回归直接报警
        assertions = raw.get("assert")
        if assertions is not None and not isinstance(assertions, dict):
            raise ValueError(f"数据集 {name} 用例 {case_id} 的 assert 必须是对象")

        cases.append({
            "id": case_id,
            "topic": str(raw["topic"]).strip(),
            "category": str(raw.get("category") or "通用").strip(),
            "difficulty": str(raw.get("difficulty") or "medium").strip(),
            "kind": kind,
            "keywords": keywords,
            "negative_keywords": negative_keywords,
            "description": str(raw.get("description") or "").strip(),
            "mock_tools": mock_tools or None,
            "assert": assertions or None,
        })

    return {
        "name": name,
        "description": str(data.get("description") or "").strip(),
        "cases": cases,
    }
