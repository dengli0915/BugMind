# -*- coding: utf-8 -*-
"""大模型返回结果的校验、结构化与兜底。

设计原则：无论上游返回什么（标准 JSON、带 markdown 的 JSON、纯文本分级、
甚至一句道歉），这里都必须产出「三级结构完整」的数据，保证前端渲染不崩。
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

LEVEL_KEYS = ("level1", "level2", "level3")
LEVEL_KEYS_CN = ("一级", "二级", "三级")

# 单个字段的长度上限，防止上游失控输出撑爆响应体
_MAX_TEXT_LEN = 6000
_MAX_CODE_LEN = 12000

# explain 可能出现的字段名
_TEXT_FIELDS = ("explain", "hint_text", "hint", "text", "content", "detail", "explanation", "message")
# fix_code 可能出现的字段名
_CODE_FIELDS = ("fix_code", "fixed_code", "code", "solution", "answer_code", "correct_code")

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?|\n?\s*```\s*$")


class LLMParseError(Exception):
    """大模型返回内容不符合输出契约。"""


# ==================== 基础清洗 ====================
def _strip_fences(text: str) -> str:
    """去掉整体包裹的 ``` 围栏（只处理首尾，不动正文）。"""
    value = (text or "").strip()
    if value.startswith("```"):
        value = _FENCE_RE.sub("", value, count=1)
    if value.endswith("```"):
        value = value[: -3].rstrip()
    return value.strip()


def _clip(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value if v)
    if isinstance(value, Mapping):
        # 兼容 {"text": "..."} 之类的嵌套
        for key in _TEXT_FIELDS:
            if key in value:
                return _as_text(value[key])
        return ""
    return str(value)


def _normalize_entry(value: Any) -> Dict[str, str]:
    """把单级结果规范化成 {"hint_text": ..., "fix_code": ...}。"""
    hint, code = "", ""

    if isinstance(value, str):
        hint = value
    elif isinstance(value, Mapping):
        for key in _TEXT_FIELDS:
            if key in value and value[key]:
                hint = _as_text(value[key])
                break
        for key in _CODE_FIELDS:
            if key in value and value[key]:
                code = _as_text(value[key])
                break
        if not hint and not code:
            # 完全没有已知字段时，把剩余可变内容拼成文案
            hint = "\n".join(_as_text(v) for k, v in value.items() if k not in _CODE_FIELDS)
    elif isinstance(value, (list, tuple)):
        # 例如 ["讲解", "代码"] 这种两元数组
        parts = [_as_text(v) for v in value if v is not None]
        if parts:
            hint = parts[0]
            code = parts[1] if len(parts) > 1 else ""
    else:
        hint = _as_text(value)

    hint = _clip(_strip_fences(hint).strip(), _MAX_TEXT_LEN)
    code = _clip(_strip_fences(code).strip(), _MAX_CODE_LEN)
    changes = _normalize_changes(value.get("changes") if isinstance(value, Mapping) else None)
    return {"hint_text": hint, "fix_code": code, "changes": changes}


def _normalize_changes(value: Any) -> List[Dict[str, Any]]:
    """把模型给的 changes 规整成 [{line, what, why}]。

    「改哪一行、改了什么、为什么」是给学生看的批注，属于锦上添花：
    格式再乱也只丢掉这一条，绝不影响三级讲解本身。
    """
    out: List[Dict[str, Any]] = []
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return out
    for item in value[:40]:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"line": 0, "what": text[:60], "why": ""})
            continue
        if not isinstance(item, Mapping):
            continue
        low = {str(k).strip().lower(): v for k, v in item.items()}
        line = low.get("line", low.get("ln", low.get("row", 0)))
        raw = str(line).strip().lstrip("第").rstrip("行").strip()
        try:
            line_no = int(raw)
        except Exception:
            line_no = 0
        what = _as_text(low.get("what") or low.get("change") or low.get("new") or "").strip()
        why = _as_text(low.get("why") or low.get("reason") or low.get("note") or "").strip()
        if not what and not why:
            continue
        out.append({"line": line_no, "what": what[:80], "why": why[:100]})
    return out


def _empty_levels() -> Dict[str, Dict[str, str]]:
    return {str(i + 1): {"hint_text": "", "fix_code": "", "changes": []} for i in range(len(LEVEL_KEYS))}


# ==================== JSON 提取 ====================
def _iter_json_candidates(text: str):
    """依次给出可能构成 JSON 的候选片段（整段 / 花括号区间 / 去括号后）。"""
    yield text
    stripped = _strip_wrapping(text)
    if stripped != text:
        yield stripped
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        yield text[start : end + 1]


def _strip_wrapping(text: str) -> str:
    """去掉整段包裹的圆括号，例如 ({"level1": ...})。

    真实模型偶尔会把 JSON 当参数写在括号里，括号会让整体解析失败。
    """
    value = (text or "").strip()
    while len(value) >= 2 and value[0] == "(" and value[-1] == ")":
        value = value[1:-1].strip()
    return value


def extract_json(text: str) -> Dict[str, Any]:
    """从模型输出中尽力提取一个 JSON 对象。"""
    cleaned = _strip_wrapping(_strip_fences((text or "").strip()))
    if not cleaned:
        raise LLMParseError("大模型返回为空")

    for candidate in _iter_json_candidates(cleaned):
        # strict=False：容忍模型在字符串里直接写换行/制表符（合法 JSON 应转义，
        # 但真实模型经常不转义，这里放宽以保证代码块能完整取回）
        try:
            data = json.loads(candidate, strict=False)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # RecursionError：极端深嵌套的畸形输入会让 json.loads 递归爆栈，同样走字面量兜底
            try:
                data = ast.literal_eval(candidate)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            # 少数模型会用数组包裹
            return data[0]
    raise LLMParseError("大模型返回内容无法解析为 JSON")


# ==================== 结构化 ====================
def _collect_from_mapping(data: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    """按 level1/level2/level3（含常见变体）取值。"""
    # 先做一次小写键索引，兼容 Level1 / level_1 / levels 包裹
    lowered = {str(k).strip().lower(): v for k, v in data.items()}

    if isinstance(lowered.get("levels"), (Mapping, list)):
        nested = lowered["levels"]
        if isinstance(nested, Mapping):
            for k, v in nested.items():
                lowered.setdefault(str(k).strip().lower(), v)
        elif isinstance(nested, list):
            for idx, item in enumerate(nested[:3]):
                lowered.setdefault(str(idx + 1), item)

    levels = _empty_levels()
    for idx, key in enumerate(LEVEL_KEYS):
        candidates = (
            key,
            key.replace("level", "level_"),
            key.replace("level", "lv"),
            str(idx + 1),
            f"level{idx + 1}",
            f"第{idx + 1}级",
            LEVEL_KEYS_CN[idx] + "讲解",
            LEVEL_KEYS_CN[idx],
        )
        value: Optional[Any] = None
        for cand in candidates:
            if cand in lowered and lowered[cand] not in (None, "", [], {}):
                value = lowered[cand]
                break
        if value is not None:
            levels[str(idx + 1)] = _normalize_entry(value)
    return levels


def _split_text_levels(text: str) -> Dict[str, str]:
    """纯文本兜底：按「一级/二级/三级」或「level1/2/3」标记切分。"""
    cleaned = _strip_fences(text.strip())
    pattern = re.compile(
        r"(?:【\s*(?:第)?(一级|二级|三级)[^】]{0,12}】)|"
        r"(?:^\s*(?:第)?(一级|二级|三级)[讲解提示文案]*\s*[:：])|"
        r"(?:^\s*level\s*([123])\s*[:：])",
        re.MULTILINE | re.IGNORECASE,
    )
    marks = list(pattern.finditer(cleaned))
    result: Dict[str, str] = {}
    for i, m in enumerate(marks):
        label = m.group(1) or m.group(2)
        if label:
            level_no = LEVEL_KEYS_CN.index(label) + 1
        else:
            level_no = int(m.group(3))
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(cleaned)
        body = cleaned[start:end].strip()
        # 去掉分隔线与“提示文案：”前缀
        body = re.sub(r"^[-—=]{2,}\s*", "", body).strip()
        body = re.sub(r"^提示文案\s*[:：]\s*", "", body).strip()
        if body and str(level_no) not in result:
            result[str(level_no)] = body
    return result


_GENERIC_FALLBACK = {
    "1": "已收到你的代码，但本次诊断结果未能完整返回，请稍后重试。你可以先自查三件事：用到的变量是否都已定义、参与运算的数据类型是否一致、循环与索引的边界是否正确。",
    "2": "调试思路：在报错位置前后加 print() 打印中间变量，确认每一步的实际类型与取值；再对照题目要求检查分支条件、缩进层级和循环终止条件是否写全。",
    "3": "知识点：拿到报错先看异常类型（SyntaxError / NameError / TypeError / IndexError / ZeroDivisionError 等），再看行号逐行核对变量与类型；修改时只动出错的那一行，保留其余逻辑不动。",
}


_JSONISH_RE = re.compile(r'"(?:level[123]|explain|fix_code)"\s*:', re.IGNORECASE)
_EXPLAIN_FIELD_RE = re.compile(
    r'"(?:explain|hint_text|hint|text|content|detail|explanation)"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)
_CODE_FIELD_RE = re.compile(
    r'"(?:fix_code|fixed_code|code|solution|answer_code|correct_code)"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def _unescape_json_string(value: str) -> str:
    """把 JSON 字符串字面量里的转义还原成真实文本（失败就原样返回）。"""
    try:
        return json.loads('"' + value + '"')
    except (json.JSONDecodeError, ValueError):
        return value.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")


def _salvage_levels(text: str) -> Dict[str, Dict[str, str]]:
    """从「像 JSON 但整体解析不了」的文本里，按字段名捞出三级文案与修复代码。

    典型场景：模型输出 ({"level1": {...}}) 且中间夹了未转义引号，整体 JSON 解析失败。
    此时绝不能把整段 JSON 原文丢给学生看。
    """
    salvaged: Dict[str, Dict[str, str]] = {}
    explains = [_unescape_json_string(m).strip() for m in _EXPLAIN_FIELD_RE.findall(text)]
    codes = [_unescape_json_string(m).strip() for m in _CODE_FIELD_RE.findall(text)]
    if not explains:
        return salvaged
    for idx in range(min(3, len(explains))):
        code = codes[idx] if idx < len(codes) else ""
        salvaged[str(idx + 1)] = {
            "hint_text": _clip(explains[idx], _MAX_TEXT_LEN),
            "fix_code": _clip(code, _MAX_CODE_LEN),
        }
    return salvaged


def fallback_levels(raw_text: str = "") -> Dict[str, Dict[str, str]]:
    """兜底：解析失败时也要返回三级结构，保证前端正常渲染。"""
    levels = _empty_levels()
    text = (raw_text or "").strip()

    if text:
        parts = _split_text_levels(text)
        if parts:
            for key, body in parts.items():
                if key in levels:
                    levels[key]["hint_text"] = _clip(_strip_fences(body), _MAX_TEXT_LEN)
        else:
            salvaged = _salvage_levels(text)
            if salvaged:
                # 像 JSON 的残留：按字段捞回来，绝不把原文直接给学生
                for key, entry in salvaged.items():
                    levels[key] = entry
            elif _JSONISH_RE.search(text):
                # 明显是结构化残留但捞不出内容，走通用文案
                logger.warning("兜底：模型输出现疑似 JSON 残片但无法解析，改用通用引导文案")
            else:
                # 完全无结构：把原文作为一级启发，通用建议补齐二三级
                levels["1"]["hint_text"] = _clip(_strip_fences(text), _MAX_TEXT_LEN)

    for key in ("1", "2", "3"):
        if not levels[key]["hint_text"]:
            levels[key]["hint_text"] = _GENERIC_FALLBACK[key]
    return levels


def _has_level_key(data: Mapping[str, Any]) -> bool:
    """判断返回体里是否出现了 level1/level2/level3（或其常见变体）字段。

    注意：字段存在但值为空字符串是合法情况 —— 代表「代码没有 bug」。
    """
    lowered = {str(k).strip().lower() for k in data.keys()}
    if "levels" in lowered:
        return True
    for idx, key in enumerate(LEVEL_KEYS):
        variants = {
            key,
            key.replace("level", "level_"),
            key.replace("level", "lv"),
            str(idx + 1),
            f"第{idx + 1}级",
            LEVEL_KEYS_CN[idx] + "讲解",
            LEVEL_KEYS_CN[idx],
        }
        if lowered & variants:
            return True
    return False


_CODE_BLOCK_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def _extract_code_block(text: str) -> str:
    """纯文本兜底时，从某一级讲解里抽出 ``` 代码块作为 fix_code。"""
    match = _CODE_BLOCK_RE.search(text or "")
    return match.group(1).strip() if match else ""


def parse_levels(raw_text: str) -> Dict[str, Dict[str, str]]:
    """把模型输出解析成 {"1": {...}, "2": {...}, "3": {...}}，失败抛 LLMParseError。"""
    try:
        data = extract_json(raw_text)
    except LLMParseError as exc:
        # 模型没按 JSON 作答，而是写了【一级讲解-启发层】这类纯文本：尽力切分
        parts = _split_text_levels(raw_text or "")
        if not parts:
            raise exc
        levels = _empty_levels()
        for key, body in parts.items():
            if key not in levels:
                continue
            levels[key]["hint_text"] = _clip(_strip_fences(body), _MAX_TEXT_LEN)
            code = _extract_code_block(body)
            if code:
                levels[key]["fix_code"] = _clip(code, _MAX_CODE_LEN)
        return levels

    if not _has_level_key(data):
        raise LLMParseError("大模型返回 JSON 中缺少 level1/level2/level3 字段")
    return _collect_from_mapping(data)


def is_no_bug(levels: Mapping[str, Mapping[str, str]]) -> bool:
    """三级全空 → 判定为「代码没有 bug」。"""
    return not any((entry.get("hint_text") or entry.get("fix_code")) for entry in levels.values())


# ==================== 错误原因（大白话 / 专业） ====================
_PLAIN_KEYS = ("plain_reason", "plainreason", "plain", "simple_reason", "layman", "大白话", "通俗解释")
_PRO_KEYS = (
    "professional_reason",
    "professionalreason",
    "professional",
    "pro_reason",
    "expert_reason",
    "technical_reason",
    "专业解释",
    "专业原因",
)
_REASON_MAX_CHARS = 200


def _pick_reason(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """从返回体里按候选字段名取一条解释文案（大小写/下划线不敏感）。"""
    lowered: Dict[str, Any] = {}
    for key, value in data.items():
        lowered[str(key).strip().lower().replace("_", "")] = value
    for key in keys:
        value = lowered.get(key.replace("_", "").replace(" ", ""))
        if isinstance(value, str) and value.strip():
            return _clip(value.strip(), _REASON_MAX_CHARS)
    return ""


def parse_reason_fields(raw_text: str) -> Dict[str, str]:
    """解析 plain_reason / professional_reason 两个字段；解析不出来就返回空串。

    这两个字段是「锦上添花」：任何异常都不能影响三级讲解的返回，
    所以这里永不抛错，前端拿到空串时自动隐藏对应栏。
    """
    result = {"plain_reason": "", "professional_reason": ""}
    try:
        data = extract_json(raw_text)
    except Exception:  # noqa: BLE001 - 原因字段容错优先
        return result
    if not isinstance(data, Mapping):
        return result
    result["plain_reason"] = _pick_reason(data, _PLAIN_KEYS)
    result["professional_reason"] = _pick_reason(data, _PRO_KEYS)
    return result
