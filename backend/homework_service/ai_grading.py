# -*- coding: utf-8 -*-
"""AI 作业批改：参考答案校验、一键批量批改、进度查询。

设计约束：
    - 完全复用 llm_service.chat（与诊断/变式共用同一份 .env 配置，不新增密钥）；
    - 批改结果写回现有 homework_submissions（新增 grade_source / grade_detail / ai_comment 字段）；
    - 积分复用 homework_service.points.award，key 与手动打分一致（hw:<作业ID>），
      因此同一份作业只会入账一次，AI 与手动混用不会重复加分；
    - 批量批改在后台 asyncio 任务里跑，前端用 job_id 轮询进度，不阻塞请求。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from llm_service import FRIENDLY_ERROR_MESSAGE, LLMServiceError, active_model, chat
from llm_service.parser import LLMParseError, extract_json

from . import points as pts
from . import store

router = APIRouter(prefix="/api", tags=["ai-grading"])

MAX_BATCH = 200
MAX_STUDENT_IDS = 500  # 单次指定名单长度上限（名单只做过滤，实际批改量仍受 MAX_BATCH 限制）
JOB_TTL = 6 * 3600  # 完成 6 小时后的任务从内存清理

logger = logging.getLogger(__name__)

CHECK_SYSTEM_PROMPT = """你是一位严谨的编程课命题审核老师。教师会给你一道作业题、参考答案、解题过程和评分标准。
你的任务是检查这份参考答案与解题过程是否正确、完整、无歧义。

【输出硬性要求】
- 只输出一个 JSON 对象，不要输出分析过程、Markdown 代码块或任何解释文字。
- issues 最多 5 条，每条 detail 不超过 80 字；corrected 仅在确实需要修正时填写。
- 如果答案正确，ok=true，issues 可以为空数组。

JSON 结构：
{
  "ok": true 或 false,
  "summary": "一句话结论（40 字以内）",
  "issues": [
    {"severity": "high|medium|low", "title": "问题标题（20 字以内）", "detail": "具体说明与修改建议（80 字以内）"}
  ],
  "corrected": {
    "reference_answer": "修正后的参考答案（ok 为 true 时为空字符串）",
    "solution": "修正后的解题过程（ok 为 true 时为空字符串）",
    "rubric": [{"item": "得分点", "max": 10, "desc": "判定说明"}]
  }
}"""

GRADE_SYSTEM_PROMPT = """你是一位认真、公正的编程课作业批改老师。教师会给你：作业题目、参考答案、解题过程、评分标准，以及一位学生的提交内容。
请严格按评分标准逐项打分，并给出面向学生的详细评语。

批改要求：
1. 总分 0-100 的整数；各得分点分数之和应等于总分（允许因四舍五入相差 1 分）；
2. 学生答案正确但写法不同，只要逻辑与结果等价就应给分，不要抠字面；
3. 指出具体错误位置（代码行、步骤或结论），说明错因和改法；
4. 评语先肯定做得好的地方，再指出问题，最后给 1-2 条可执行的改进建议；
5. 如果学生只提交了附件、没有文字内容，score 给 0，comment 说明“仅提交附件，需教师人工批阅”。

只输出一个 JSON 对象，不要输出 Markdown 或额外解释，结构如下：
{
  "score": 85,
  "rubric": [
    {"item": "得分点名称", "max": 20, "score": 17, "comment": "该得分点的判定说明"}
  ],
  "errors": [
    {"location": "第 3 行 / 第 2 步", "issue": "错误描述", "suggestion": "修改建议"}
  ],
  "strengths": ["做得好的地方"],
  "comment": "面向学生的总体评语（120 字以内）",
  "suggestion": "改进建议（80 字以内）"
}"""


# ==================== 请求模型 ====================
class RubricItem(BaseModel):
    item: Optional[str] = ""
    max: Optional[float] = 0
    desc: Optional[str] = ""


class AIConfigRequest(BaseModel):
    teacher_id: Optional[str] = ""
    question: Optional[str] = ""
    reference_answer: str
    solution: Optional[str] = ""
    rubric: Optional[List[RubricItem]] = None
    check_status: Optional[str] = ""      # ok / corrected / original / unchecked
    check_summary: Optional[str] = ""


class AICheckRequest(BaseModel):
    teacher_id: Optional[str] = ""
    question: Optional[str] = ""
    reference_answer: str
    solution: Optional[str] = ""
    rubric: Optional[List[RubricItem]] = None


class AIGradeRequest(BaseModel):
    teacher_id: Optional[str] = ""
    force: bool = False
    student_ids: Optional[List[str]] = None


# ==================== 基础工具 ====================
def _clip(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _find_hw(hw_id: str) -> Dict[str, Any]:
    row = next((r for r in store.read_table("homeworks") if str(r.get("id")) == str(hw_id)), None)
    if not row:
        raise HTTPException(status_code=404, detail="作业不存在或已被删除。")
    return row


def _load_config(hw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = hw.get("grading_config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def _require_config(hw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _load_config(hw)
    if not _clip(cfg.get("reference_answer")):
        raise HTTPException(status_code=400, detail="请先设置参考答案，再使用 AI 批改。")
    return cfg


def _require_teacher(hw: Dict[str, Any], teacher_id: Optional[str], detail: str) -> None:
    """教师归属校验：作业已有属主时，操作者必须一致（与 router.py 删除作业同思路）。

    兼容历史数据：作业没有 teacher_id、或调用方未传 teacher_id 时不拦截，
    仅在双方都有值且不一致时返回 403（老客户端零改动）。
    """
    owner = _clip(hw.get("teacher_id"), 40)
    who = _clip(teacher_id, 40)
    if owner and who and owner != who:
        logger.warning("越权拦截：作业 %s 属于教师 %s，操作者 %s", hw.get("id"), owner, who)
        raise HTTPException(status_code=403, detail=detail)


def _normalize_rubric(raw: Any) -> List[Dict[str, Any]]:
    """把前端 / 模型给出的评分标准规整成 [{item, max, desc}]。"""
    if isinstance(raw, str):
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                rows.append({"item": line[:120], "max": 0, "desc": ""})
        return rows[:30]
    out: List[Dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        item = _clip(entry.get("item") or entry.get("name") or entry.get("title"), 120)
        if not item:
            continue
        try:
            mx = float(entry.get("max") or entry.get("score") or 0)
        except (TypeError, ValueError):
            mx = 0.0
        out.append({
            "item": item,
            "max": max(0.0, min(100.0, round(mx, 2))),
            "desc": _clip(entry.get("desc") or entry.get("comment"), 500),
        })
        if len(out) >= 30:
            break
    return out


def _rubric_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "（教师未提供评分标准，请按题目要求自行划分得分点，总分 100）"
    lines = []
    for idx, row in enumerate(rows, 1):
        desc = f"：{row['desc']}" if row.get("desc") else ""
        lines.append(f"{idx}. {row['item']}（{row.get('max', 0)} 分）{desc}")
    return "\n".join(lines)


def _submission_text(sub: Dict[str, Any]) -> str:
    content = _clip(sub.get("content"), 4000)
    files = sub.get("files") or []
    if content:
        return content
    if files:
        names = "、".join(_clip(f.get("name"), 80) for f in files[:6] if isinstance(f, dict))
        return f"（学生未填写文字说明，仅提交了附件：{names}）"
    return "（学生未填写任何内容）"


JSON_FORMATTER_PROMPT = """你是 JSON 格式化器。用户会给你一段老师/模型的自然语言分析，请把它整理成严格 JSON。
只输出 JSON 对象本身，不要 Markdown 代码块、不要解释。字段缺失时用空字符串或空数组补齐。
结构必须是：
{"ok": true/false, "summary": "结论", "issues": [{"severity": "high|medium|low", "title": "标题", "detail": "说明"}],
 "corrected": {"reference_answer": "", "solution": "", "rubric": [{"item": "", "max": 0, "desc": ""}]}}"""


async def _chat(system: str, user: str, *, temperature: float, max_tokens: int) -> str:
    try:
        return await chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
    except LLMServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.friendly) from None
    except Exception:
        raise HTTPException(status_code=503, detail=FRIENDLY_ERROR_MESSAGE) from None


def _repair_json_text(text: str) -> str:
    """把被截断的 JSON 补齐引号与括号（模型偶尔在字符串中间断掉）。"""
    raw = (text or "").strip()
    start = raw.find("{")
    if start < 0:
        return ""
    raw = raw[start:].split("```")[0]
    out: List[str] = []
    in_str = False
    escaped = False
    stack: List[str] = []
    for ch in raw:
        out.append(ch)
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack:
            stack.pop()
    fixed = "".join(out)
    if in_str:
        fixed += '"'
    fixed = fixed.rstrip()
    while fixed and fixed[-1] in ",:":
        fixed = fixed[:-1]
    for closer in reversed(stack):
        fixed += closer
    return fixed


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    for candidate in (text, _repair_json_text(text)):
        if not candidate:
            continue
        try:
            data = extract_json(candidate)
        except LLMParseError:
            continue
        if isinstance(data, dict):
            return data
    return None


async def _llm_json(system: str, user: str, *, temperature: float, max_tokens: int = 2200) -> Dict[str, Any]:
    """调用大模型并解析 JSON；解析失败时把原文交给格式化器再试一次。"""
    raw = await _chat(system, user, temperature=temperature, max_tokens=max_tokens)
    data = _try_parse_json(raw)
    if data is None:
        # 少数模型会先输出一段分析再给 JSON，甚至只给分析：用一次低温格式化兜底
        logger.warning("AI 返回无法直接解析为 JSON（原文长度 %d），用格式化器兜底重试", len(raw or ""))
        formatted = await _chat(
            JSON_FORMATTER_PROMPT,
            raw[:6000] or "（模型没有返回内容，请按结构给出 ok=false 与一条说明）",
            temperature=0.0,
            max_tokens=1800,
        )
        data = _try_parse_json(formatted)
    if data is None:
        raise HTTPException(status_code=502, detail="AI 返回内容格式异常，请重试。")
    return data


def _as_score(value: Any, fallback: float = 0.0) -> int:
    """把模型给出的分数规整为 0-100 整数。

    模型偶尔输出 "nan"/"1e999" 之类非法数值：NaN/Infinity 直接按 fallback 处理，
    否则 int(round(nan)) 抛 ValueError、int(round(inf)) 抛 OverflowError，
    且 NaN 一旦落盘会让整张 JSON 表在下次读取时被判为损坏。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = fallback
    if not math.isfinite(num):
        num = fallback
    return max(0, min(100, int(round(num))))


def _clean_errors(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            issue = _clip(entry.get("issue") or entry.get("error") or entry.get("detail"), 300)
            if not issue:
                continue
            out.append({
                "location": _clip(entry.get("location") or entry.get("where"), 80),
                "issue": issue,
                "suggestion": _clip(entry.get("suggestion") or entry.get("fix"), 300),
            })
        elif isinstance(entry, str) and entry.strip():
            out.append({"location": "", "issue": entry.strip()[:300], "suggestion": ""})
        if len(out) >= 20:
            break
    return out


def _clean_rubric_scores(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        item = _clip(entry.get("item") or entry.get("name") or entry.get("title"), 120)
        if not item:
            continue
        try:
            mx = float(entry.get("max") or entry.get("full") or 0)
        except (TypeError, ValueError):
            mx = 0.0
        try:
            sc = float(entry.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        mx = max(0.0, min(100.0, mx))
        sc = max(0.0, min(mx or 100.0, sc))
        out.append({
            "item": item,
            "max": round(mx, 1),
            "score": round(sc, 1),
            "comment": _clip(entry.get("comment") or entry.get("desc"), 300),
        })
        if len(out) >= 30:
            break
    return out


# ==================== 参考答案校验 ====================
def _check_prompt(hw: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    question = _clip(cfg.get("question") or hw.get("content"), 4000)
    rubric = _normalize_rubric(cfg.get("rubric"))
    return (
        f"【作业题目】\n{question}\n\n"
        f"【教师参考答案】\n{_clip(cfg.get('reference_answer'), 4000)}\n\n"
        f"【解题过程】\n{_clip(cfg.get('solution'), 4000) or '（未提供）'}\n\n"
        f"【评分标准】\n{_rubric_text(rubric)}\n"
    )


def _normalize_check(data: Dict[str, Any]) -> Dict[str, Any]:
    ok = data.get("ok")
    if isinstance(ok, str):
        ok = ok.strip().lower() in ("true", "1", "yes", "ok")
    issues = []
    for entry in data.get("issues") or []:
        if isinstance(entry, dict):
            title = _clip(entry.get("title") or entry.get("issue"), 120)
            detail = _clip(entry.get("detail") or entry.get("suggestion"), 800)
            if title or detail:
                issues.append({
                    "severity": _clip(entry.get("severity") or "medium", 16),
                    "title": title or "可优化点",
                    "detail": detail,
                })
        elif isinstance(entry, str) and entry.strip():
            issues.append({"severity": "medium", "title": "可优化点", "detail": entry.strip()[:800]})
        if len(issues) >= 12:
            break
    corrected_raw = data.get("corrected") if isinstance(data.get("corrected"), dict) else {}
    corrected = {
        "reference_answer": _clip(corrected_raw.get("reference_answer") or corrected_raw.get("answer"), 4000),
        "solution": _clip(corrected_raw.get("solution") or corrected_raw.get("process"), 4000),
        "rubric": _normalize_rubric(corrected_raw.get("rubric")),
    }
    return {
        "ok": bool(ok),
        "summary": _clip(data.get("summary"), 400),
        "issues": issues,
        "corrected": corrected,
        "model": active_model(),
        "checked_at": store.now_str(),
    }


@router.post("/homeworks/{hw_id}/ai-check")
async def check_reference(hw_id: str, req: AICheckRequest):
    hw = _find_hw(hw_id)
    _require_teacher(hw, req.teacher_id, "只能校验自己发布的作业。")
    cfg = {
        "question": req.question or hw.get("content"),
        "reference_answer": req.reference_answer,
        "solution": req.solution,
        "rubric": [r.model_dump() for r in (req.rubric or [])],
    }
    if not _clip(cfg["reference_answer"]):
        raise HTTPException(status_code=400, detail="请先填写参考答案。")
    data = await _llm_json(CHECK_SYSTEM_PROMPT, _check_prompt(hw, cfg), temperature=0.2, max_tokens=2400)
    result = _normalize_check(data)
    logger.info("参考答案校验完成 hw=%s ok=%s", hw_id, result.get("ok"))
    return {"success": True, "check": result}


# ==================== 参考答案配置 ====================
@router.get("/homeworks/{hw_id}/ai-config")
async def get_config(hw_id: str, teacher_id: Optional[str] = None):
    hw = _find_hw(hw_id)
    _require_teacher(hw, teacher_id, "只能查看自己发布的作业。")
    cfg = _load_config(hw)
    return {
        "success": True,
        "config": {
            "question": cfg.get("question") or hw.get("content") or "",
            "reference_answer": cfg.get("reference_answer") or "",
            "solution": cfg.get("solution") or "",
            "rubric": _normalize_rubric(cfg.get("rubric")),
            "check_status": cfg.get("check_status") or "",
            "check_summary": cfg.get("check_summary") or "",
            "updated_at": cfg.get("updated_at") or "",
        },
        "has_config": bool(_clip(cfg.get("reference_answer"))),
    }


@router.put("/homeworks/{hw_id}/ai-config")
async def save_config(hw_id: str, req: AIConfigRequest):
    hw = _find_hw(hw_id)
    _require_teacher(hw, req.teacher_id, "只能修改自己发布的作业。")
    reference = _clip(req.reference_answer)
    if len(reference) < 2:
        raise HTTPException(status_code=400, detail="参考答案不能为空。")
    rubric = _normalize_rubric([r.model_dump() for r in (req.rubric or [])])
    total_max = sum(float(r.get("max") or 0) for r in rubric)
    check_status = _clip(req.check_status, 20) or "unchecked"
    # check_status 只允许四个固定取值（见模块顶部设计约定），非法值直接拒绝，避免脏数据入库
    if check_status not in ("ok", "corrected", "original", "unchecked"):
        raise HTTPException(status_code=400, detail="check_status 仅支持 ok / corrected / original / unchecked。")
    if total_max > 100:
        # 评分标准总分超过 100：AI 仍按 0-100 分制夹紧，这里只记录不改数据（保留既有产品行为）
        logger.warning("作业 %s 评分标准总分 %.1f 超过 100，AI 将按 0-100 分制夹紧", hw_id, total_max)
    payload = {
        "question": _clip(req.question or hw.get("content"), 4000),
        "reference_answer": reference,
        "solution": _clip(req.solution, 4000),
        "rubric": rubric,
        "rubric_total": round(total_max, 1),
        "check_status": check_status,
        "check_summary": _clip(req.check_summary, 600),
        "updated_at": store.now_str(),
        "updated_by": _clip(req.teacher_id, 40),
    }

    def _apply(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if str(row.get("id")) == str(hw_id):
                row["grading_config"] = payload
                return

    store.mutate("homeworks", _apply)
    logger.info("AI 批改配置已保存 hw=%s rubric=%d 总分=%.1f", hw_id, len(rubric), round(total_max, 1))
    return {"success": True, "config": payload}


# ==================== 批量批改（异步任务） ====================
_JOBS: Dict[str, Dict[str, Any]] = {}


def _purge_jobs() -> None:
    """清理已完成超过 JOB_TTL 的批改任务（status 轮询与新批次发起时都会触发）。"""
    now = time.time()
    removed = 0
    for key in list(_JOBS.keys()):
        job = _JOBS[key]
        if job.get("finished_ts") and now - job["finished_ts"] > JOB_TTL:
            _JOBS.pop(key, None)
            removed += 1
    if removed:
        logger.info("已清理过期批改任务 %d 个（TTL=%dh）", removed, JOB_TTL // 3600)


def _job_public(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "hw_id": job["hw_id"],
        "status": job["status"],
        "total": job["total"],
        "done": job["done"],
        "failed": job["failed"],
        "skipped": job["skipped"],
        "progress": round(job["done"] / job["total"] * 100, 1) if job["total"] else 100,
        "started_at": job.get("started_at", ""),
        "finished_at": job.get("finished_at", ""),
        "message": job.get("message", ""),
        "items": job.get("items", []),
    }


async def _grade_one(hw: Dict[str, Any], sub: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """批改一个学生提交，落库并结算积分。"""
    student_id = str(sub.get("student_id") or "")
    if not student_id:
        # 提交记录缺少学生标识：无法落库、积分 key 也无法归属，按跳过处理（正常提交链路必带 student_id）
        logger.warning("提交记录缺少 student_id（homework=%s），跳过 AI 批改", hw.get("id"))
        return {
            "student_id": "",
            "student_name": sub.get("student_name") or "",
            "status": "skipped",
            "error": "学生信息缺失，无法 AI 批改。",
        }
    content = _clip(sub.get("content"), 4000)
    has_files = bool(sub.get("files"))
    if not content:
        return {
            "student_id": student_id,
            "student_name": sub.get("student_name") or student_id,
            "status": "skipped",
            "error": "学生只提交了附件，请人工批改。" if has_files else "学生没有填写文字内容。",
        }

    rubric = _normalize_rubric(cfg.get("rubric"))
    prompt = (
        f"【作业题目】\n{_clip(cfg.get('question') or hw.get('content'), 4000)}\n\n"
        f"【参考答案】\n{_clip(cfg.get('reference_answer'), 4000)}\n\n"
        f"【解题过程】\n{_clip(cfg.get('solution'), 2000) or '（未提供）'}\n\n"
        f"【评分标准】\n{_rubric_text(rubric)}\n\n"
        f"【学生提交】\n{_submission_text(sub)}\n"
    )
    t0 = time.time()
    data = await _llm_json(GRADE_SYSTEM_PROMPT, prompt, temperature=0.2, max_tokens=2200)

    score = _as_score(data.get("score"))
    logger.info("AI 批改单份完成 hw=%s student=%s score=%s 用时=%.1fs",
                hw.get("id"), student_id, score, time.time() - t0)
    rubric_scores = _clean_rubric_scores(data.get("rubric"))
    errors = _clean_errors(data.get("errors"))
    strengths = [_clip(x, 200) for x in (data.get("strengths") or []) if _clip(x)][:8]
    comment = _clip(data.get("comment"), 800)
    suggestion = _clip(data.get("suggestion"), 500)
    detail = {
        "score": score,
        "rubric": rubric_scores,
        "errors": errors,
        "strengths": strengths,
        "comment": comment,
        "suggestion": suggestion,
        "model": active_model(),
        "graded_at": store.now_str(),
    }

    applied = {"found": False}

    def _apply(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if str(row.get("homework_id")) == str(hw.get("id")) and str(row.get("student_id")) == student_id:
                row["score"] = score
                row["comment"] = comment
                row["graded"] = True
                row["graded_at"] = detail["graded_at"]
                row["grade_source"] = "ai"
                row["grade_detail"] = detail
                row["ai_comment"] = comment
                row["ai_model"] = detail["model"]
                row["reviewed"] = False
                applied["found"] = True
                return

    store.mutate("homework_submissions", _apply)
    if not applied["found"]:
        # 批改期间该提交被并发删除：分数未落库，积分也不再结算，避免留下无主流水
        logger.warning("批改落库时未找到提交记录（homework=%s student=%s），跳过积分结算", hw.get("id"), student_id)
        return {
            "student_id": student_id,
            "student_name": sub.get("student_name") or student_id,
            "status": "skipped",
            "error": "该提交已被删除，未批改。",
        }

    # 与手动打分同一个 key：同一份作业只入账一次，不会重复加分
    entitlement = pts.homework_points(score)
    award = pts.award(
        student_id=student_id,
        scene="homework",
        key=f"hw:{hw.get('id')}",
        points=entitlement,
        base=score,
        coef=0.2,
        detail=f"作业《{hw.get('title')}》AI 批改得分 {score}",
    )
    awarded_now = 0 if award.get("deduped") else entitlement

    def _mark(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if str(row.get("homework_id")) == str(hw.get("id")) and str(row.get("student_id")) == student_id:
                if not award.get("deduped"):
                    row["points"] = entitlement
                row["points_awarded"] = awarded_now
                row["awarded_at"] = award.get("awarded_at", "")
                return

    store.mutate("homework_submissions", _mark)

    return {
        "student_id": student_id,
        "student_name": sub.get("student_name") or student_id,
        "status": "done",
        "score": score,
        "points": awarded_now,
        "comment": comment,
    }


async def _run_job(job_id: str, hw: Dict[str, Any], subs: List[Dict[str, Any]], cfg: Dict[str, Any]) -> None:
    job = _JOBS.get(job_id)
    if not job:
        return
    sem = asyncio.Semaphore(2)

    async def _task(sub: Dict[str, Any]) -> None:
        async with sem:
            student_id = str(sub.get("student_id") or "")
            try:
                result = await _grade_one(hw, sub, cfg)
            except HTTPException as exc:
                logger.warning("单份批改失败 hw=%s student=%s：%s", hw.get("id"), student_id, exc.detail)
                result = {
                    "student_id": sub.get("student_id"),
                    "student_name": sub.get("student_name") or sub.get("student_id"),
                    "status": "failed",
                    "error": exc.detail,
                }
            except Exception as exc:  # 单份失败不影响整批
                logger.warning("单份批改异常 hw=%s student=%s：%s", hw.get("id"), student_id, exc)
                result = {
                    "student_id": sub.get("student_id"),
                    "student_name": sub.get("student_name") or sub.get("student_id"),
                    "status": "failed",
                    "error": str(exc)[:200] or "批改失败",
                }
            job["items"].append(result)
            if result["status"] == "done":
                job["done"] += 1
            elif result["status"] == "skipped":
                job["skipped"] += 1
                job["done"] += 1
            else:
                job["failed"] += 1
                job["done"] += 1

    try:
        await asyncio.gather(*(_task(s) for s in subs))
        job["status"] = "done"
        job["message"] = f"批改完成：成功 {job['done'] - job['skipped'] - job['failed']} 份，跳过 {job['skipped']} 份，失败 {job['failed']} 份。"
    except asyncio.CancelledError:
        # 服务关闭等取消场景：标记后继续向上抛，保持取消语义，避免任务卡在 running
        job["status"] = "failed"
        job["message"] = "批改任务被取消（服务关闭）。"
        raise
    except Exception as exc:
        # _task 已兜底单份异常，这里防 gather 本身异常导致任务永远停在 running
        job["status"] = "failed"
        job["message"] = f"批量批改中断：{str(exc)[:200]}"
        logger.exception("批量批改中断 job=%s hw=%s", job_id, hw.get("id"))
    finally:
        job["finished_at"] = store.now_str()
        job["finished_ts"] = time.time()
        logger.info(
            "批量批改结束 job=%s status=%s total=%d done=%d failed=%d skipped=%d",
            job_id, job["status"], job["total"], job["done"], job["failed"], job["skipped"],
        )


@router.post("/homeworks/{hw_id}/ai-grade")
async def start_batch_grade(hw_id: str, req: AIGradeRequest):
    hw = _find_hw(hw_id)
    cfg = _require_config(hw)
    _require_teacher(hw, req.teacher_id, "只能批改自己发布的作业。")
    _purge_jobs()

    subs = [s for s in store.read_table("homework_submissions") if str(s.get("homework_id")) == str(hw_id)]
    # 指定名单：去空、去重、限长；名单只做过滤，实际批改数量仍受 MAX_BATCH 限制
    if req.student_ids:
        wanted_ids: List[str] = []
        for raw in req.student_ids:
            sid = _clip(str(raw), 40)
            if sid and sid not in wanted_ids:
                wanted_ids.append(sid)
        if len(wanted_ids) > MAX_STUDENT_IDS:
            raise HTTPException(status_code=400, detail=f"学生名单过长（上限 {MAX_STUDENT_IDS} 人）。")
        wanted = set(wanted_ids)
        subs = [s for s in subs if str(s.get("student_id")) in wanted]
    if not req.force:
        subs = [s for s in subs if not s.get("graded")]
    subs = subs[:MAX_BATCH]

    job_id = store.new_id("aig")
    job = {
        "job_id": job_id,
        "hw_id": str(hw_id),
        "status": "running" if subs else "done",
        "total": len(subs),
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "items": [],
        "started_at": store.now_str(),
        "message": "" if subs else "没有需要批改的提交。",
    }
    _JOBS[job_id] = job
    if subs:
        asyncio.create_task(_run_job(job_id, hw, subs, cfg))
    else:
        job["finished_at"] = store.now_str()
        job["finished_ts"] = time.time()
    logger.info("发起批量批改 hw=%s job=%s total=%d force=%s teacher=%s",
                hw_id, job_id, len(subs), bool(req.force), _clip(req.teacher_id, 40) or "-")
    return {"success": True, "job": _job_public(job)}


@router.get("/homeworks/{hw_id}/ai-grade/status")
async def batch_status(hw_id: str, job_id: str = ""):
    # 轮询时顺手清理过期任务，保证 TTL 不依赖下一次发起批改才生效
    _purge_jobs()
    job = _JOBS.get(job_id)
    if not job or str(job.get("hw_id")) != str(hw_id):
        raise HTTPException(status_code=404, detail="批改任务不存在，或服务重启后已失效，请重新发起。")
    return {"success": True, "job": _job_public(job)}


@router.post("/homeworks/{hw_id}/ai-grade/{student_id}")
async def regrade_one(hw_id: str, student_id: str, teacher_id: Optional[str] = None):
    hw = _find_hw(hw_id)
    cfg = _require_config(hw)
    _require_teacher(hw, teacher_id, "只能批改自己发布的作业。")
    sub = next(
        (s for s in store.read_table("homework_submissions")
         if str(s.get("homework_id")) == str(hw_id) and str(s.get("student_id")) == str(student_id)),
        None,
    )
    if not sub:
        raise HTTPException(status_code=404, detail="该学生还没有提交这份作业。")
    result = await _grade_one(hw, sub, cfg)
    if result.get("status") != "done":
        raise HTTPException(status_code=400, detail=result.get("error") or "这份提交无法 AI 批改，请人工处理。")
    logger.info("单份重批完成 hw=%s student=%s score=%s", hw_id, student_id, result.get("score"))
    return {"success": True, "data": result}
