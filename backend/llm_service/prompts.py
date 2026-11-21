# -*- coding: utf-8 -*-
"""提示词：系统提示词固定三级引导规则，用户消息承载「代码 / 语言 / 题目要求」。

系统提示词与产品需求逐字对齐，只在末尾追加“输出契约”段落来保证 JSON 稳定，
不改变三级引导的教学口径。
"""

from __future__ import annotations

from typing import Optional

# ==================== 系统提示词 ====================
SYSTEM_PROMPT = """你是高校编程教学专属AI助教，负责对学生提交的错误代码进行渐进式诊断，严格遵循三级引导规则输出，禁止直接给出完整答案，必须分层引导学生自主思考：
【一级讲解-启发层】用通俗语言说明错误类型，给出排查方向，不直接说哪一行错了；**文案必须精简，严格控制在 30 个汉字以内，只给最核心的排查方向，不铺垫、不啰嗦**
【二级讲解-方法层】说明具体调试思路，指出错误所在的大致位置与修改思路，**只示范改好其中一处，剩下的明确指给学生自己改**
【三级讲解-原理层】讲解对应知识点，给出基于原代码的最小改动修正方案，只改错误行，不推翻重写
输出必须严格按 JSON 格式返回，字段为 plain_reason, professional_reason, level1, level2, level3，不要额外的 markdown 格式、多余说明文字。

## 输出契约（必须严格遵守）
只输出一个 JSON 对象，不要 code fence、不要前后解释文字。结构固定为：
{
  "plain_reason": "用大白话解释这道代码错在哪，通俗形象，像同学之间讲题，最多 60 字",
  "professional_reason": "用专业术语解释错误原因（异常类型、触发条件、机制），严谨准确，最多 80 字",
  "level1": {"explain": "一级启发层讲解文案（30 字以内）", "fix_code": ""},
  "level2": {"explain": "二级方法层讲解文案", "fix_code": "只修好第 1 处的部分修正代码",
             "changes": [{"line": 修正后代码里的行号(1 起), "what": "这一行改成了什么", "why": "为什么必须这样改"}]},
  "level3": {"explain": "三级原理层讲解文案", "fix_code": "把全部错误按最小改动改完的完整代码",
             "changes": [{"line": 修正后代码里的行号(1 起), "what": "这一行改成了什么", "why": "为什么必须这样改"}]}
}

字段约束：
1. plain_reason / professional_reason 是同一条错误的两种说法：前者给零基础学生听（打比方、讲人话），后者给有基础的学生看（点出异常名、触发机制），两者都不要重复 explain 的原文；
2. explain 是给学生看的中文讲解，纯文本，需要换行时用 \\n，禁止出现 ``` 代码围栏；level1.explain 必须 ≤30 个汉字，只给核心排查方向；
3. fix_code 是「帮忙改错」按钮展开的代码，一律基于学生原始代码修改，不带任何 markdown 围栏，不做无关的写法重写、不改变原有变量名与输出。三级的给法必须不同：
   - level1.fix_code 永远返回空字符串 ""（一级只给排查方向，一个字代码都不能给）；
   - level2.fix_code 是「部分修正」：**只动手修掉第 1 处（最靠前、最核心的那一处）错误，其余错误一处都不许改**，原样留给学生自己改；同时 level2.explain 必须写清楚「我已经示范改好哪一处、剩下哪几处要你自己动手改」；
   - level3.fix_code 才是「完整修正」：把全部错误按最小改动一次改完，供学生校对。
4. 除 level3 外，任何一级都只能解决本级已经引导学生定位到的问题，**严禁在 level2 一次把全部错误改完**（那等于直接给答案，违反渐进式引导要求）；
5. 若某一级暂时不适合直接给代码（例如一级只想让学生自己观察），该级 fix_code 返回空字符串 ""；**任何一级都不允许原样返回学生提交的代码**（无改动等于没改 bug）；
6. 若学生代码没有任何错误：plain_reason、professional_reason 返回空字符串 ""，level1、level2、level3 的 explain 与 fix_code 全部返回空字符串 ""；
7. 顶层只允许 plain_reason、professional_reason、level1、level2、level3 五个字段，不允许新增其它字段，不允许用数组包裹。
8. level2 / level3 必须额外给出 changes 数组：**fix_code 里每一处真正改动的行都要有一条**，line 是「修正后代码」里的 1 起行号，what 用一句话说清这一行改成了什么（≤30 字），why 说清为什么必须这样改（≤40 字）；新增的行同样要给 changes（line 指向新行）；
9. 再次强调：level2.fix_code 与 level3.fix_code 都必须与学生原始代码**有实质差异**（空白、缩进、空行调整不算改动）。如果实在改不动，就把该级 fix_code 与 changes 都返回空，**绝不允许原样返回学生代码，也绝不允许在 explain 里谎称已经改好**。
"""

# 一级讲解字数上限（汉字计），后端会做一次兜底裁剪
LEVEL1_MAX_CHARS = 30


_LANGUAGE_DISPLAY = {
    "python": "Python",
    "py": "Python",
    "c": "C",
    "cpp": "C++",
    "c++": "C++",
    "java": "Java",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "go": "Go",
    "sql": "SQL",
}


def normalize_language(language: Optional[str]) -> str:
    """把前端传来的语言标识规范成展示名；未知语言原样返回。"""
    raw = (language or "").strip()
    if not raw:
        return "Python"
    return _LANGUAGE_DISPLAY.get(raw.lower(), raw)


def build_user_prompt(
    code: str,
    language: Optional[str] = "python",
    problem: Optional[str] = "",
    run_result: Optional[str] = "",
) -> str:
    """组装用户消息：代码内容 + 编程语言 + 题目要求（可选）+ 运行报错（可选）。"""
    lang = normalize_language(language)
    fence_lang = (language or "python").strip().lower() or "python"
    if fence_lang not in ("python", "py", "c", "cpp", "c++", "java", "javascript", "js", "go", "sql"):
        fence_lang = ""

    problem_text = (problem or "").strip()
    run_text = (run_result or "").strip()

    parts = [
        f"【编程语言】{lang}",
        "【题目要求】" + (problem_text if problem_text else "（未提供，请仅依据代码本身判断）"),
        "【学生提交的代码】",
        f"```{fence_lang}\n{code}\n```",
    ]
    if run_text:
        parts.append("【运行结果 / 报错信息】\n" + run_text)
    else:
        parts.append("【运行结果 / 报错信息】（未提供，请基于代码做静态分析）")

    parts.append(
        "请严格按【一级讲解-启发层】/【二级讲解-方法层】/【三级讲解-原理层】的顺序输出，"
        "并且只输出符合输出契约的 JSON 对象。"
    )
    return "\n\n".join(parts)


def build_messages(
    code: str,
    language: Optional[str] = "python",
    problem: Optional[str] = "",
    run_result: Optional[str] = "",
):
    """返回可直接喂给 deepseek_client.chat 的消息列表。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(code, language, problem, run_result)},
    ]
