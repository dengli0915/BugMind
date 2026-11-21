# -*- coding: utf-8 -*-
"""作业体系 + 统一积分 + 文件上传的路由集合。

接口一览（全部挂在 /api 下，与诊断接口互不影响）：

    教师端
        POST   /api/homeworks                     发布作业
        GET    /api/homeworks?teacher_id=         本班作业列表（含提交人数）
        GET    /api/homeworks/{id}/submissions    某份作业的学生提交详情
        POST   /api/homeworks/{id}/grade          打分 0-100（并按 5:1 折算积分）
    学生端
        GET    /api/homeworks?class_id=&student_id=  本班作业 + 我的提交状态
        GET    /api/homeworks/{id}?student_id=       作业详情
        POST   /api/homeworks/{id}/submit            提交作业（含附件）
    通用
        POST   /api/upload                        附件上传（base64，返回可访问地址）
        POST   /api/points/award                  统一积分入账
        GET    /api/points/ledger?student_id=     积分流水（自查/教师端展示）
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import points as pts
from . import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["homework"])

MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # 单个附件上限 40MB（base64 解码后）
_ALLOWED_EXT = {
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    # 文档
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".csv", ".zip", ".rar",
    # 视频
    ".mp4", ".mov", ".webm", ".avi", ".mkv",
}
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


# ==================== 请求模型 ====================
class HomeworkCreate(BaseModel):
    teacher_id: str
    teacher_name: Optional[str] = ""
    class_id: str
    class_name: Optional[str] = ""
    title: str
    content: str
    deadline: str
    attachments: Optional[List[Dict[str, Any]]] = None
    # 定向作业：只发给名单内的学生（分组变式练习用）；为空则是发给整个班级
    target_students: Optional[List[str]] = None
    # 逾期规则：截止后仍未提交，按 penalty_points 扣冲分积分（默认扣 10 分）
    penalize_late: bool = False
    penalty_points: int = 10
    # 幂等键：同一个教师 + 同一个 source_key 只生成一份作业（防连点重复发布）
    source_key: Optional[str] = ""


class SubmitRequest(BaseModel):
    student_id: str
    student_name: Optional[str] = ""
    class_id: Optional[str] = ""
    content: Optional[str] = ""
    files: Optional[List[Dict[str, Any]]] = None


class GradeRequest(BaseModel):
    student_id: str
    score: int
    comment: Optional[str] = ""
    teacher_id: Optional[str] = ""


class UploadRequest(BaseModel):
    name: str
    data: str  # base64（可带 data:xxx;base64, 前缀）
    mime: Optional[str] = ""


class AwardRequest(BaseModel):
    student_id: Optional[str] = ""
    scene: str = "retry"
    key: str = ""
    difficulty: Optional[str] = "mid"
    rank_index: Optional[int] = 0
    homework_score: Optional[int] = None


class PenaltyRequest(BaseModel):
    """作业逾期扣分：points 传正数，落账时记成负数流水。"""

    student_id: str
    key: str
    points: int = 10
    detail: Optional[str] = ""


# ==================== 工具函数 ====================
def _clean(value: Optional[str], limit: int = 4000) -> str:
    text = (value or "").strip()
    return text[:limit]


def _parse_deadline(raw: str) -> Dict[str, Any]:
    text = _clean(raw, 40)
    if not text:
        raise HTTPException(status_code=400, detail="请选择作业截止时间。")
    dt = None
    matched = ""
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            matched = fmt
            break
        except ValueError:
            continue
    if dt is None:
        raise HTTPException(status_code=400, detail="截止时间格式不正确，应为「YYYY-MM-DD HH:MM」。")
    if matched == "%Y-%m-%d":
        # 只给了日期：按当天 23:59 截止（不让学生因为没填时分而被判逾期）
        dt = dt.replace(hour=23, minute=59)
    return {"text": dt.strftime("%Y-%m-%d %H:%M"), "ts": int(dt.timestamp() * 1000)}


def _deadline_expired(deadline_ts: int) -> bool:
    return int(datetime.now().timestamp() * 1000) > int(deadline_ts or 0)


def _class_svc():
    """惰性导入班级服务（避免模块级循环依赖）。"""
    from class_service import classes as cls_svc

    return cls_svc


def class_display_name(class_id: Optional[str]) -> str:
    """把班级 ID 还原成班级名；老数据里 class_id 本身就是班级名，原样返回。"""
    key = str(class_id or "").strip()
    if not key:
        return ""
    try:
        row = _class_svc().class_by_id(key) or _class_svc().class_by_name(key)
    except Exception:  # 班级服务不可用时不影响作业主流程
        return key
    return str(row.get("name") or key) if row else key


def _class_keys(class_id: str = "", class_name: str = "") -> Dict[str, set]:
    """把「按班级过滤」的参数展开成 {ids, names}，兼容只存班级名的历史作业。"""
    raw = [str(class_id or "").strip(), str(class_name or "").strip()]
    ids: set = set(x for x in raw if x)
    names: set = set(x for x in raw if x)
    try:
        row = _class_svc().resolve_class(class_id, class_name)
    except Exception:
        row = None
    if row:
        ids.add(str(row.get("class_id") or ""))
        names.add(str(row.get("name") or ""))
    return {"ids": ids, "names": names}


def _hw_matches_class(row: Dict[str, Any], keys: Dict[str, set]) -> bool:
    cid = str(row.get("class_id") or "")
    cname = str(row.get("class_name") or "")
    return cid in keys["ids"] or cid in keys["names"] or cname in keys["names"] or cname in keys["ids"]


def _hw_brief(row: Dict[str, Any], *, submission: Optional[Dict[str, Any]] = None,
              count: int = 0) -> Dict[str, Any]:
    item = {
        "id": row.get("id"),
        "title": row.get("title"),
        "content": row.get("content"),
        "class_id": row.get("class_id"),
        "class_name": row.get("class_name") or class_display_name(row.get("class_id")),
        "teacher_id": row.get("teacher_id"),
        "teacher_name": row.get("teacher_name"),
        "created_at": row.get("created_at"),
        "deadline": row.get("deadline"),
        "deadline_ts": row.get("deadline_ts"),
        "attachments": row.get("attachments") or [],
        "expired": _deadline_expired(row.get("deadline_ts") or 0),
        "submission_count": count,
        "target_students": row.get("target_students") or [],
        "target_count": len(row.get("target_students") or []),
        "penalize_late": bool(row.get("penalize_late")),
        "penalty_points": int(row.get("penalty_points") or 0),
        "source_key": row.get("source_key") or "",
        "has_ai_config": bool(((row.get("grading_config") or {}).get("reference_answer") or "").strip()),
        "ai_config_at": (row.get("grading_config") or {}).get("updated_at") or "",
    }
    if submission:
        item["my_submission"] = submission
        item["status"] = "graded" if submission.get("graded") else "submitted"
    else:
        item["my_submission"] = None
        item["status"] = "pending"
    return item


# ==================== 作业：发布 / 列表 / 详情 ====================
@router.post("/homeworks")
async def create_homework(req: HomeworkCreate):
    teacher_id = _clean(req.teacher_id, 40)
    class_id = _clean(req.class_id, 60)
    title = _clean(req.title, 120)
    content = _clean(req.content, 4000)
    if not teacher_id:
        raise HTTPException(status_code=400, detail="缺少教师账号，请重新登录后重试。")
    if not class_id:
        raise HTTPException(status_code=400, detail="请选择作业所属班级。")
    if not title:
        raise HTTPException(status_code=400, detail="请填写作业题目。")
    if not content:
        raise HTTPException(status_code=400, detail="请填写作业要求，便于学生理解。")
    deadline = _parse_deadline(req.deadline)

    # 定向名单：去空、去重、限长（一个分组最多几十人，200 足够宽松）
    targets: List[str] = []
    for raw in (req.target_students or []):
        sid = _clean(str(raw), 40)
        if sid and sid not in targets:
            targets.append(sid)
    targets = targets[:200]
    penalty = max(0, min(100, int(req.penalty_points or 0)))
    penalize_late = bool(req.penalize_late) and penalty > 0
    source_key = _clean(req.source_key, 120)

    # 幂等：同一教师 + 同一 source_key 已经有作业时直接返回旧的，避免连点生成重复作业
    if source_key:
        for old in store.read_table("homeworks"):
            if str(old.get("teacher_id") or "") == teacher_id and str(old.get("source_key") or "") == source_key:
                # 重复发布：返回已存在的作业，不重复建课
                logger.info("重复发布被幂等拦截：教师=%s source_key=%s 返回旧作业=%s", teacher_id, source_key, old.get("id"))
                return {"success": True, "duplicated": True, "data": _hw_brief(old)}

    # 班级归属校验：能解析成班级表里的班时，必须是该教师自己的班
    try:
        cls_row = _class_svc().resolve_class(class_id, req.class_name or "")
    except Exception:
        cls_row = None
    if cls_row:
        # V30：主讲老师（main_teacher_id）说了算；池里没人认领的班不能直接发作业
        owner = str(cls_row.get("main_teacher_id") or cls_row.get("teacher_id") or "")
        scope = str(cls_row.get("scope") or ("private" if owner else "pool"))
        if scope == "pool" and not owner:
            alt = None
            try:
                alt = _class_svc().class_by_name(str(req.class_name or class_id), teacher_id)
            except Exception:
                alt = None
            if alt and str(alt.get("main_teacher_id") or alt.get("teacher_id") or "") == teacher_id:
                cls_row = alt
                owner = teacher_id
            else:
                raise HTTPException(
                    status_code=403,
                    detail="这个班级还在班级池里，请先在「我的班级」认领，再给它发布作业。",
                )
        if owner and owner != teacher_id:
            # 同名班级可能由不同教师各建一份：按「教师 + 班名」重新解析一次
            try:
                alt = _class_svc().class_by_name(class_id, teacher_id) or _class_svc().class_by_name(
                    str(req.class_name or ""), teacher_id
                )
            except Exception:
                alt = None
            if alt:
                cls_row = alt
            else:
                raise HTTPException(status_code=403, detail="只能给自己主讲的班级发布作业。")
        real_class_id = str(cls_row.get("class_id"))
        real_class_name = str(cls_row.get("name") or class_id)
    else:
        # 兼容未迁移的历史班级：class_id 直接是班级名
        real_class_id = class_id
        real_class_name = str(req.class_name or class_id)

    row = {
        "id": store.new_id("hw"),
        "teacher_id": teacher_id,
        "teacher_name": _clean(req.teacher_name, 40),
        "class_id": real_class_id,
        "class_name": real_class_name,
        "title": title,
        "content": content,
        "deadline": deadline["text"],
        "deadline_ts": deadline["ts"],
        "attachments": req.attachments or [],
        "target_students": targets,
        "penalize_late": penalize_late,
        "penalty_points": penalty if penalize_late else 0,
        "source_key": source_key,
        "created_at": store.now_str(),
    }
    store.mutate("homeworks", lambda rows: rows.append(row))
    # 发布作业成功：记录关键信息便于回溯（谁、哪个班、定向人数、逾期规则）
    logger.info(
        "发布作业 hw=%s 教师=%s 班级=%s 定向名单=%d 人 逾期扣分=%s(扣%d)",
        row["id"], teacher_id, real_class_name, len(targets), penalize_late, penalty,
    )
    return {"success": True, "data": _hw_brief(row)}


@router.get("/homeworks")
async def list_homeworks(
    class_id: Optional[str] = None,
    teacher_id: Optional[str] = None,
    student_id: Optional[str] = None,
    class_name: Optional[str] = None,
):
    rows = store.read_table("homeworks")
    subs = store.read_table("homework_submissions")

    if teacher_id:
        rows = [r for r in rows if str(r.get("teacher_id")) == str(teacher_id)]

    if class_id or class_name:
        # 指定班级：按班级 ID 或班级名过滤（兼容历史作业把班级名存在 class_id 里的情况）
        keys = _class_keys(class_id or "", class_name or "")
        rows = [r for r in rows if _hw_matches_class(r, keys)]
    elif student_id:
        # 学生端：只返回「已加入班级」的作业；一个班都没有时返回空列表
        try:
            scope = _class_svc().class_scope_of_user(str(student_id))
        except Exception:
            scope = {"ids": [], "names": []}
        ids = set(scope.get("ids") or [])
        names = set(scope.get("names") or [])
        rows = [
            r for r in rows
            if str(r.get("class_id") or "") in ids
            or str(r.get("class_id") or "") in names
            or str(r.get("class_name") or "") in names
        ]

    # 定向作业（分组变式练习）只对名单内的学生可见；名单为空表示全班可见
    if student_id:
        sid = str(student_id)
        rows = [
            r for r in rows
            if not r.get("target_students") or sid in [str(x) for x in (r.get("target_students") or [])]
        ]

    rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)

    items: List[Dict[str, Any]] = []
    for row in rows:
        hw_subs = [s for s in subs if str(s.get("homework_id")) == str(row.get("id"))]
        mine = None
        if student_id:
            for s in hw_subs:
                if str(s.get("student_id")) == str(student_id):
                    mine = s
                    break
        items.append(_hw_brief(row, submission=mine, count=len(hw_subs)))
    return {"success": True, "items": items, "total": len(items)}


@router.get("/homeworks/{hw_id}")
async def get_homework(hw_id: str, student_id: Optional[str] = None):
    row = next((r for r in store.read_table("homeworks") if str(r.get("id")) == str(hw_id)), None)
    if not row:
        raise HTTPException(status_code=404, detail="作业不存在或已被删除。")
    subs = [s for s in store.read_table("homework_submissions") if str(s.get("homework_id")) == str(hw_id)]
    mine = None
    if student_id:
        mine = next((s for s in subs if str(s.get("student_id")) == str(student_id)), None)
    return {"success": True, "data": _hw_brief(row, submission=mine, count=len(subs))}


# ==================== 作业：提交 / 批改 ====================
@router.post("/homeworks/{hw_id}/submit")
async def submit_homework(hw_id: str, req: SubmitRequest):
    hw = next((r for r in store.read_table("homeworks") if str(r.get("id")) == str(hw_id)), None)
    if not hw:
        raise HTTPException(status_code=404, detail="作业不存在或已被删除。")
    student_id = _clean(req.student_id, 40)
    if not student_id:
        raise HTTPException(status_code=400, detail="缺少学生账号，请重新登录后重试。")

    files = req.files or []
    content = _clean(req.content, 4000)
    if not files and not content:
        raise HTTPException(status_code=400, detail="请先选择附件或填写提交说明。")
    # 截止后仍允许补交：记为 late，由作业的 penalize_late / penalty_points 规则处理逾期，
    # 不在这里直接拒绝（产品规则：允许补交但可扣分）

    payload = {
        "homework_id": hw_id,
        "student_id": student_id,
        "student_name": _clean(req.student_name, 40),
        "class_id": _clean(req.class_id, 60) or hw.get("class_id"),
        "content": content,
        "files": files,
        "submitted_at": store.now_str(),
        "late": _deadline_expired(hw.get("deadline_ts") or 0),
    }

    def _upsert(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        for idx, row in enumerate(rows):
            if str(row.get("homework_id")) == str(hw_id) and str(row.get("student_id")) == str(student_id):
                payload["id"] = row.get("id") or store.new_id("hs")
                payload["score"] = row.get("score")
                payload["comment"] = row.get("comment")
                payload["graded"] = row.get("graded", False)
                payload["graded_at"] = row.get("graded_at")
                payload["points"] = row.get("points", 0)
                rows[idx] = payload
                return payload
        payload["id"] = store.new_id("hs")
        payload["score"] = None
        payload["comment"] = ""
        payload["graded"] = False
        payload["graded_at"] = ""
        payload["points"] = 0
        rows.append(payload)
        return payload

    saved = store.mutate("homework_submissions", _upsert)
    # 提交作业：记录谁在什么时间提交/改交、是否逾期（重复提交为更新，不是新增）
    logger.info("提交作业 hw=%s 学生=%s 附件=%d 个 迟到=%s", hw_id, student_id, len(files), payload["late"])
    return {"success": True, "data": saved}


@router.get("/homeworks/{hw_id}/submissions")
async def list_submissions(hw_id: str, teacher_id: Optional[str] = None):
    hw = next((r for r in store.read_table("homeworks") if str(r.get("id")) == str(hw_id)), None)
    if not hw:
        raise HTTPException(status_code=404, detail="作业不存在或已被删除。")
    # 教师归属：传了 teacher_id 时必须与作业发布者一致，防止教师 A 查看教师 B 的作业提交；
    # 未传时保持历史兼容（前端老版本仍可查看，接口契约不变）
    if teacher_id and str(hw.get("teacher_id") or "") and str(teacher_id) != str(hw.get("teacher_id")):
        raise HTTPException(status_code=403, detail="只能查看自己发布的作业的提交。")
    subs = [s for s in store.read_table("homework_submissions") if str(s.get("homework_id")) == str(hw_id)]
    subs.sort(key=lambda s: str(s.get("submitted_at", "")), reverse=True)
    graded = [s for s in subs if s.get("graded")]
    avg = round(sum(float(s.get("score") or 0) for s in graded) / len(graded), 1) if graded else 0
    return {
        "success": True,
        "homework": _hw_brief(hw, count=len(subs)),
        "items": subs,
        "stats": {"total": len(subs), "graded": len(graded), "average": avg},
    }


@router.post("/homeworks/{hw_id}/grade")
async def grade_homework(hw_id: str, req: GradeRequest):
    hw = next((r for r in store.read_table("homeworks") if str(r.get("id")) == str(hw_id)), None)
    if not hw:
        raise HTTPException(status_code=404, detail="作业不存在或已被删除。")
    if req.score is None or not (0 <= int(req.score) <= 100):
        raise HTTPException(status_code=400, detail="分数必须是 0-100 的整数。")
    score = int(req.score)
    comment = _clean(req.comment, 500)
    student_id = _clean(req.student_id, 40)
    if not student_id:
        raise HTTPException(status_code=400, detail="缺少学生账号，无法打分。")
    # 教师归属：传了 teacher_id 时必须与作业发布者一致，防止批改他人作业；
    # 未传时保持历史兼容（AI 批改走 ai_grading 的独立链路，不走本接口）
    if req.teacher_id and str(hw.get("teacher_id") or "") and str(req.teacher_id) != str(hw.get("teacher_id")):
        raise HTTPException(status_code=403, detail="只能批改自己发布的作业。")

    target: Dict[str, Any] = {}

    def _apply(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if str(row.get("homework_id")) == str(hw_id) and str(row.get("student_id")) == str(student_id):
                row["score"] = score
                row["comment"] = comment
                row["graded"] = True
                row["graded_at"] = store.now_str()
                row["grader_id"] = _clean(req.teacher_id, 40)
                # 人工复核：AI 批过的提交被教师手动改分时打上标记，保留 AI 明细供对照
                if str(row.get("grade_source") or "") == "ai":
                    row["grade_source"] = "manual"
                    row["reviewed"] = True
                    row["reviewed_at"] = row["graded_at"]
                elif not row.get("grade_source"):
                    row["grade_source"] = "manual"
                target.update(row)
                return
        raise HTTPException(status_code=404, detail="该学生还没有提交这份作业。")

    store.mutate("homework_submissions", _apply)

    # 5 分作业分 = 1 冲分积分；同一份作业只入账一次
    award_points = pts.homework_points(score)
    result = pts.award(
        student_id=student_id,
        scene="homework",
        key=f"hw:{hw_id}",
        points=award_points,
        base=score,
        coef=0.2,
        detail=f"作业《{hw.get('title')}》得分 {score}",
    )

    def _mark(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if str(row.get("homework_id")) == str(hw_id) and str(row.get("student_id")) == str(student_id):
                # 只有真正新入账时才更新「已获得积分」；重复评分不发放也不再改动展示值
                if not result.get("deduped"):
                    row["points"] = award_points
                row["points_awarded"] = 0 if result.get("deduped") else award_points
                row["awarded_at"] = result.get("awarded_at", "")
                return

    store.mutate("homework_submissions", _mark)

    # 批改作业：记录打分与积分入账结果（重复打分不重复入账）
    logger.info(
        "批改作业 hw=%s 学生=%s 分数=%d 应得积分=%d 本次入账=%d",
        hw_id, student_id, score, award_points, 0 if result.get("deduped") else award_points,
    )

    return {
        "success": True,
        "student_id": student_id,
        "score": score,
        "comment": comment,
        "points": award_points,          # 该分数应得的总积分
        "awarded_now": 0 if result.get("deduped") else award_points,  # 本次实际新入账
        "deduped": bool(result.get("deduped")),
        "awarded_at": result.get("awarded_at", ""),
    }


# ==================== 作业：删除（教师撤回误发） ====================
@router.delete("/homeworks/{hw_id}")
async def delete_homework(hw_id: str, teacher_id: Optional[str] = None):
    teacher_id = _clean(teacher_id, 40)
    # 删除必须表明身份：不传 teacher_id 时旧逻辑会放行任何人删除任何作业，属于越权漏洞
    if not teacher_id:
        raise HTTPException(status_code=400, detail="缺少教师账号，请重新登录后重试。")

    def _drop(rows: List[Dict[str, Any]]) -> bool:
        for idx, row in enumerate(rows):
            if str(row.get("id")) == str(hw_id):
                if str(row.get("teacher_id")) != str(teacher_id):
                    raise HTTPException(status_code=403, detail="只能删除自己发布的作业。")
                rows.pop(idx)
                return True
        return False

    removed = store.mutate("homeworks", _drop)
    if not removed:
        raise HTTPException(status_code=404, detail="作业不存在或已被删除。")

    def _purge_subs(rows: List[Dict[str, Any]]) -> int:
        before = len(rows)
        rows[:] = [r for r in rows if str(r.get("homework_id")) != str(hw_id)]
        return before - len(rows)

    removed_subs = store.mutate("homework_submissions", _purge_subs)
    # 删除作业：记录操作者与被连带清除的提交数，便于误删回溯
    logger.info("删除作业 hw=%s 教师=%s 连带清除提交=%d 条", hw_id, teacher_id, removed_subs)
    return {"success": True, "id": hw_id}


# ==================== 文件上传 ====================
@router.post("/upload")
async def upload_file(req: UploadRequest):
    name = _clean(req.name, 160) or "附件"
    raw_b64 = (req.data or "").strip()
    if not raw_b64:
        raise HTTPException(status_code=400, detail="文件内容为空，请重新选择文件。")
    if raw_b64.startswith("data:") and "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw_b64, validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="文件内容解析失败，请重新选择文件。") from None

    if not blob:
        raise HTTPException(status_code=400, detail="文件内容为空，请重新选择文件。")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB），请压缩后再上传。",
        )

    ext = ""
    if "." in name:
        ext = "." + name.rsplit(".", 1)[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail="不支持的文件类型，请上传图片、文档或视频（png/jpg/pdf/doc/docx/xls/xlsx/ppt/pptx/txt/mp4 等）。",
        )

    safe_stem = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", name.rsplit(".", 1)[0])[:60] or "file"
    # 用业务 ID 做文件名前缀：即使同一毫秒多人上传同名文件也不会互相覆盖
    fname = f"{store.new_id('up')}_{safe_stem}{ext}"
    store.ensure_dirs()
    target = store.UPLOAD_DIR / fname
    try:
        target.write_bytes(blob)
    except OSError:
        # 写盘失败（磁盘满/权限/占用）不再冒成裸 500 堆栈，给出友好提示并留日志
        logger.exception("附件写入磁盘失败：%s", target)
        raise HTTPException(status_code=500, detail="附件保存失败，请稍后重试。") from None

    kind = "image" if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else (
        "video" if ext in {".mp4", ".mov", ".webm", ".avi", ".mkv"} else "doc"
    )
    return {
        "success": True,
        "url": f"/uploads/{fname}",
        "name": name,
        "size": len(blob),
        "type": kind,
        "mime": _clean(req.mime, 80),
    }


# ==================== 统一积分 ====================
@router.post("/points/award")
async def award_points(req: AwardRequest):
    scene = str(req.scene or "retry").strip().lower()
    student_id = _clean(req.student_id, 40)

    if scene == "homework":
        if req.homework_score is None:
            raise HTTPException(status_code=400, detail="作业加分需要提供 homework_score（0-100）。")
        value = pts.homework_points(req.homework_score)
        calc = {"points": value, "base": int(req.homework_score), "coef": 0.2, "rank_index": 0}
    else:
        calc = pts.difficulty_points(req.difficulty, int(req.rank_index or 0))

    if not req.key:
        # 无 key 只试算不入账（前端预览积分用），不要求 student_id，保持旧行为
        return {"success": True, "scene": scene, "deduped": False, **calc, "awarded_at": store.now_str()}

    # 真实入账必须落到具体学生，防止空账号写脏流水
    if not student_id:
        raise HTTPException(status_code=400, detail="缺少学生账号，无法发放积分。")

    result = pts.award(
        student_id=student_id,
        scene=scene,
        key=req.key,
        points=calc["points"],
        base=calc["base"],
        coef=calc["coef"],
        detail=f"{pts.SCENE_LABEL.get(scene, scene)} · {req.difficulty or ''}".strip(" ·"),
    )
    # 积分入账：记录场景、去重键与是否重复（幂等保障的核心审计点）
    logger.info(
        "积分入账 scene=%s key=%s 学生=%s points=%d 重复=%s",
        scene, req.key, student_id, calc["points"], bool(result.get("deduped")),
    )
    return {
        "success": True,
        "scene": scene,
        "points": 0 if result.get("deduped") else calc["points"],
        "base": calc["base"],
        "coef": calc["coef"],
        "rank_index": calc["rank_index"],
        "deduped": bool(result.get("deduped")),
        "awarded_at": result.get("awarded_at", ""),
    }


@router.get("/points/ledger")
async def points_ledger(student_id: str = Query(...), limit: int = 50):
    return {"success": True, "items": pts.ledger_for(student_id, max(1, min(limit, 200)))}


@router.get("/points/summary")
async def points_summary(student_id: str = Query(...)):
    """学生积分汇总（前端登录 / 进入作业页时用来同步 AI 批改等跨端入账）。"""
    rows = [r for r in store.read_table("point_awards") if str(r.get("student_id")) == str(student_id)]
    by_scene: Dict[str, int] = {}
    total = 0
    for row in rows:
        value = int(row.get("points") or 0)
        total += value
        scene = str(row.get("scene") or "other")
        by_scene[scene] = by_scene.get(scene, 0) + value
    latest = max((str(r.get("awarded_at") or "") for r in rows), default="")
    return {
        "success": True,
        "student_id": student_id,
        "total": total,
        "by_scene": by_scene,
        "count": len(rows),
        "latest": latest,
    }


@router.post("/points/penalty")
async def penalty_points(req: PenaltyRequest):
    """作业逾期扣分（同一 key 只扣一次，重复调用返回 deduped）。"""
    student_id = _clean(req.student_id, 40)
    key = _clean(req.key, 120)
    if not student_id:
        raise HTTPException(status_code=400, detail="缺少学生账号，无法扣分。")
    if not key:
        raise HTTPException(status_code=400, detail="缺少扣分标识，无法扣分。")
    value = -abs(int(req.points or 0))
    if value == 0:
        raise HTTPException(status_code=400, detail="扣分分值必须大于 0。")
    result = pts.award(
        student_id=student_id,
        scene="late",
        key=key,
        points=value,
        detail=_clean(req.detail, 200) or "作业逾期未交",
    )
    # 逾期扣分：与发积分共用 (student_id, key) 幂等，重复调用只扣一次
    logger.info(
        "逾期扣分 key=%s 学生=%s points=%d 重复=%s",
        key, student_id, value, bool(result.get("deduped")),
    )
    return {
        "success": True,
        "deduped": bool(result.get("deduped")),
        "points": 0 if result.get("deduped") else value,
        "awarded_at": result.get("awarded_at", ""),
    }
