# -*- coding: utf-8 -*-
"""冲分变式练习：AI 侧的同源变式题生成与判题规则解析。

与诊断链路完全解耦（独立的提示词、独立的解析器），
诊断的三级讲解契约、提示词一个字都不动。

题目契约（前端直接渲染）：
    {
      "question":  "题干（纯文本，可含换行）",
      "starter_code": "起始代码模板（可为空串）",
      "expected_output": "正确程序应输出/返回的内容",
      "checks": {"must": ["正则1", ...], "must_not": ["正则2", ...]},
      "solution": "参考答案代码",
      "hint": "一句话引导",
      "difficulty": "easy|mid|hard"
    }

判题思路（与本地模式保持一致，全程不执行学生代码）：
    正确解必须命中 checks.must 里的全部代码特征，
    且不得出现 checks.must_not 里的典型错误特征。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

VARIANT_SYSTEM_PROMPT = """你是高校编程课程的出题助教，负责为一道学生刚刚做错的题目，出一道「同源变式题」用于冲分考核。

要求：
1. 只出一道题，考点必须与原错题的知识点、错误类型完全一致，难度相当（大一编程入门，不超纲）。
2. 题干简洁（不超过 120 字），必须包含：任务要求 + 输入输出说明（或函数签名），不要长篇故事。
3. 题目必须是可以用一段 Python 代码作答的编程题，不要出选择题。
4. 判题采用「关键代码特征检查」，不运行代码，因此：
   - checks.must：列出正确解必然包含的 1~3 个代码特征（写成 Python 正则字符串），必须精准且唯一，避免误伤合理写法；
   - checks.must_not：列出典型错误写法特征（对应学生这次的错误点），1~2 个即可。
   正则只能用 ASCII 字符与常规符号，不要使用中文变量名。
5. expected_output：正确程序的输出内容（若是函数题则写期望返回值）。
6. solution：一段可运行的参考代码，直接给出最终正确写法。
7. hint：一句话点拨，指出该题最容易踩的坑，不给完整答案。

只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字，字段固定为：
{"question": "...", "starter_code": "...", "expected_output": "...", "checks": {"must": ["..."], "must_not": ["..."]}, "solution": "...", "hint": "...", "difficulty": "easy"}
"""

_DIFFICULTIES = {"easy", "mid", "hard"}
_MAX_FIELD = 4000


def build_variant_prompt(
    knowledge_point: str,
    error_type: str = "",
    code: str = "",
    language: str = "python",
) -> str:
    """组装变式题生成请求（复用诊断链路的“同一知识点”上下文）。"""
    parts: List[str] = []
    parts.append(f"【编程语言】{language or 'python'}")
    parts.append(f"【知识点】{knowledge_point or '通用编程基础'}")
    if error_type:
        parts.append(f"【学生这次的错误类型】{error_type}")
    if code:
        snippet = code if len(code) <= 1500 else code[:1500] + "\n# ...（已截断）"
        parts.append("【学生出错的代码（仅作参考，不要照抄）】\n```" + (language or "python") + "\n" + snippet + "\n```")
    parts.append("请出 1 道同源变式题，并按要求输出 JSON。")
    return "\n\n".join(parts)


def _clip(text: Any, limit: int = _MAX_FIELD) -> str:
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    s = s.strip()
    return s if len(s) <= limit else s[:limit]


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            text = _clip(item, 200)
            if text:
                out.append(text)
        return out
    return []


def _extract_json(raw: str) -> Dict[str, Any]:
    """宽松提取：先去围栏，再取首个 { 到末个 }。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        chunk = text[start : end + 1]
        try:
            data = json.loads(chunk)
            if isinstance(data, dict):
                return data
        except Exception:
            # 尝试把中文引号换回英文引号后再解析
            try:
                data = json.loads(chunk.replace("“", '"').replace("”", '"'))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def _valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def parse_variant(raw: str) -> Dict[str, Any]:
    """把模型输出转成题目契约；缺字段就抛 ValueError，由调用方决定降级。"""
    data = _extract_json(raw)
    question = _clip(data.get("question") or data.get("题目") or data.get("q"))
    solution = _clip(data.get("solution") or data.get("answer") or data.get("参考代码"))
    if not question:
        raise ValueError("模型未给出题干")
    checks_raw = data.get("checks") or data.get("判题规则") or {}
    if not isinstance(checks_raw, dict):
        checks_raw = {}
    must = [p for p in _as_list(checks_raw.get("must") or checks_raw.get("包含")) if _valid_regex(p)]
    must_not = [
        p for p in _as_list(checks_raw.get("must_not") or checks_raw.get("mustNot") or checks_raw.get("排除"))
        if _valid_regex(p)
    ]
    difficulty = _clip(data.get("difficulty") or "easy", 16).lower()
    if difficulty not in _DIFFICULTIES:
        difficulty = "easy"
    return {
        "question": question,
        "starter_code": _clip(data.get("starter_code") or ""),
        "expected_output": _clip(data.get("expected_output") or ""),
        "checks": {"must": must[:4], "must_not": must_not[:4]},
        "solution": solution,
        "hint": _clip(data.get("hint") or "", 300),
        "difficulty": difficulty,
    }
