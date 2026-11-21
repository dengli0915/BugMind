# -*- coding: utf-8 -*-
"""班级体系 HTTP 接口（APIRouter, prefix="/api"）。

接口一览
    POST /api/classes                     教师创建班级（自动生成 6 位唯一班级码）
    GET  /api/classes?teacher_id=         教师：我创建的班级（含班级码、人数）
    GET  /api/classes?user_id=            学生：我加入的班级
    GET  /api/classes/{class_id}          班级详情
    GET  /api/classes/{class_id}/students 班级学生列表（仅主讲教师）
    POST /api/classes/join                学生凭班级码加入
    POST /api/classes/leave               学生退出班级
    DELETE /api/classes/{class_id}        教师删除自己的班级
    POST /api/classes/sync                历史数据迁移（幂等）
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import classes as svc

router = APIRouter(prefix="/api")
logger = logging.getLogger("bugmind.class")


class CreateClassRequest(BaseModel):
    name: Optional[str] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    # V30 班级池：scope=private（默认，创建者即主讲）/ pool（投放到池，等认领）
    scope: Optional[str] = None
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None
    owner_role: Optional[str] = None


class ClaimClassRequest(BaseModel):
    class_id: Optional[str] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None


class ReleaseClassRequest(BaseModel):
    class_id: Optional[str] = None
    teacher_id: Optional[str] = None


class JoinClassRequest(BaseModel):
    code: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None


class LeaveClassRequest(BaseModel):
    class_id: Optional[str] = None
    user_id: Optional[str] = None


class SyncClassItem(BaseModel):
    name: Optional[str] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None


class SyncMemberItem(BaseModel):
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    class_name: Optional[str] = None


class SyncRequest(BaseModel):
    classes: Optional[List[SyncClassItem]] = None
    memberships: Optional[List[SyncMemberItem]] = None


# ---------------------------------------------------------------- 写入
@router.post("/classes")
def create_class(req: CreateClassRequest):
    name = (req.name or "").strip()
    teacher_id = (req.teacher_id or "").strip()
    scope = "pool" if str(req.scope or "").strip().lower() == "pool" else "private"
    owner_role = "student" if str(req.owner_role or "").strip() == "student" else "teacher"
    if not name:
        raise HTTPException(status_code=400, detail="请填写班级名称。")
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="班级名称不能超过 40 个字。")
    # 教师建私密班必须有教师标识；投放到池 / 学生建班则允许只带 owner
    if not teacher_id and scope == "private" and owner_role != "student":
        raise HTTPException(status_code=400, detail="缺少教师标识，请重新登录后再试。")
    try:
        item = svc.create_class(
            name,
            teacher_id,
            req.teacher_name or "",
            scope=scope,
            owner_id=req.owner_id or "",
            owner_name=req.owner_name or "",
            owner_role=owner_role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "%s %s 创建班级「%s」（%s，scope=%s）",
        "教师" if owner_role == "teacher" else "学生",
        teacher_id or req.owner_id or "-",
        name,
        item.get("class_id"),
        scope,
    )
    return {"success": True, "data": item}


@router.post("/classes/claim")
def claim_class(req: ClaimClassRequest):
    """认领班级池里的班：一个班同一时间只能有一个主讲。"""
    class_id = (req.class_id or "").strip()
    teacher_id = (req.teacher_id or "").strip()
    if not class_id or not teacher_id:
        raise HTTPException(status_code=400, detail="缺少班级或教师标识，请重新登录后再试。")
    try:
        item = svc.claim_class(class_id, teacher_id, req.teacher_name or "")
    except ValueError as exc:
        msg = str(exc)
        # 「已有主讲」是并发冲突，用 409 让前端能单独提示
        raise HTTPException(status_code=409 if "主讲老师" in msg else 400, detail=msg) from exc
    logger.info("教师 %s 认领班级「%s」（%s）", teacher_id, item.get("name"), class_id)
    return {"success": True, "data": item, "message": "已认领「%s」，你现在是主讲老师。" % item.get("name")}


@router.post("/classes/release")
def release_class(req: ReleaseClassRequest):
    """放弃主讲：班级回到池里等别人认领。"""
    class_id = (req.class_id or "").strip()
    teacher_id = (req.teacher_id or "").strip()
    if not class_id or not teacher_id:
        raise HTTPException(status_code=400, detail="缺少班级或教师标识。")
    try:
        item = svc.release_class(class_id, teacher_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    logger.info("教师 %s 放弃班级「%s」（%s）", teacher_id, item.get("name"), class_id)
    return {"success": True, "data": item, "message": "已放弃「%s」，班级回到班级池。" % item.get("name")}


@router.post("/classes/join")
def join_class(req: JoinClassRequest):
    code = (req.code or "").strip().upper()
    user_id = (req.user_id or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入班级码。")
    # 班级码统一为 6 位字母数字（生成器保证），提前拦掉明显非法的输入
    if not re.fullmatch(r"[A-Z0-9]{6}", code):
        raise HTTPException(status_code=400, detail="班级码格式不正确：应为 6 位字母数字组合。")
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少学生标识，请重新登录后再试。")
    row = svc.class_by_code(code)
    if not row:
        raise HTTPException(status_code=404, detail="班级码不存在，请向老师确认后重新输入。")
    if svc.is_member(user_id, row["class_id"]):
        cls = svc.class_by_id(row["class_id"])
        summary = svc.list_class_summary([cls])[0] if cls else None
        return {
            "success": True,
            "already": True,
            "message": "你已经在「%s」班里了。" % row.get("name"),
            "data": summary,
        }
    try:
        svc.add_member(user_id, row["class_id"], req.user_name or "")
    except ValueError as exc:
        # 加入人数上限等业务限制：翻译成 400 而不是 500
        logger.warning("学生 %s 加入班级 %s 失败: %s", user_id, row["class_id"], exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cls = svc.class_by_id(row["class_id"])
    counts = svc.list_class_summary([cls]) if cls else []
    logger.info("学生 %s 加入班级「%s」（%s）", user_id, row.get("name"), row["class_id"])
    return {
        "success": True,
        "already": False,
        "message": "已加入「%s」。" % row.get("name"),
        "data": counts[0] if counts else None,
    }


@router.post("/classes/leave")
def leave_class(req: LeaveClassRequest):
    class_id = (req.class_id or "").strip()
    user_id = (req.user_id or "").strip()
    if not class_id or not user_id:
        raise HTTPException(status_code=400, detail="缺少班级或学生标识。")
    ok = svc.remove_member(user_id, class_id)
    logger.info("学生 %s 退出班级 %s（removed=%s）", user_id, class_id, ok)
    return {"success": True, "removed": ok}


@router.delete("/classes/{class_id}")
def delete_class(class_id: str, teacher_id: str = ""):
    teacher_id = (teacher_id or "").strip()
    if not teacher_id:
        raise HTTPException(status_code=400, detail="缺少账号信息，无法删除班级。")
    try:
        row = svc.delete_by_owner(class_id, teacher_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    logger.info("教师 %s 删除自己创建的班级「%s」（%s）", teacher_id, row.get("name"), class_id)
    return {"success": True, "message": "班级「%s」已删除。" % (row.get("name") or "")}


# ---------------------------------------------------------------- 查询
@router.get("/classes")
def list_classes(teacher_id: str = "", user_id: str = ""):
    teacher_id = (teacher_id or "").strip()
    user_id = (user_id or "").strip()
    if teacher_id:
        # 教师端 = 我当主讲的班（可管理）+ 班级池（只读 / 可认领）
        groups = svc.classes_visible_to_teacher(teacher_id)
        mine, pool = groups["mine"], groups["pool"]
        items = mine + pool
        return {
            "success": True,
            "role": "teacher",
            "items": items,
            "mine": mine,
            "pool": pool,
            "total": len(items),
            "mine_total": len(mine),
            "pool_total": len(pool),
            "unclaimed": len([r for r in pool if r.get("claimable")]),
        }
    if user_id:
        items = svc.classes_of_user(user_id)
        return {"success": True, "role": "student", "items": items, "total": len(items)}
    raise HTTPException(status_code=400, detail="缺少参数：teacher_id 或 user_id。")


@router.get("/classes/{class_id}")
def get_class(class_id: str):
    row = svc.class_by_id(class_id)
    if not row:
        raise HTTPException(status_code=404, detail="班级不存在或已被删除。")
    summary = svc.list_class_summary([row])
    return {"success": True, "data": summary[0]}


@router.get("/classes/{class_id}/students")
def class_students(class_id: str, teacher_id: str = ""):
    row = svc.class_by_id(class_id)
    if not row:
        raise HTTPException(status_code=404, detail="班级不存在或已被删除。")
    main_id = str(row.get("main_teacher_id") or row.get("teacher_id") or "")
    scope = str(row.get("scope") or ("private" if main_id else "pool"))
    if teacher_id and main_id != teacher_id.strip() and scope != "pool":
        raise HTTPException(status_code=403, detail="只能查看自己班级的学生。")
    if not teacher_id:
        # 保持既有开放行为（避免影响已上线的调用方），仅记录告警便于后续收紧
        logger.warning("查看班级学生列表未携带 teacher_id（class_id=%s）", class_id)
    items = svc.students_of_class(class_id)
    return {"success": True, "class": {"class_id": row.get("class_id"), "name": row.get("name")}, "items": items, "total": len(items)}


@router.get("/classes/{class_id}/members")
def class_members(class_id: str):
    """班级成员 ID 列表（只返回账号与姓名，供班级排行榜等客户端过滤使用）。"""
    row = svc.class_by_id(class_id)
    if not row:
        raise HTTPException(status_code=404, detail="班级不存在或已被删除。")
    items = svc.students_of_class(class_id)
    members = [{"user_id": i.get("user_id"), "user_name": i.get("user_name")} for i in items]
    return {
        "success": True,
        "class": {"class_id": row.get("class_id"), "name": row.get("name")},
        "items": members,
        "user_ids": [m["user_id"] for m in members],
        "total": len(members),
    }


# ---------------------------------------------------------------- 迁移
@router.post("/classes/sync")
def sync_classes(req: SyncRequest):
    payload_classes: List[Dict[str, Any]] = [
        {
            "name": c.name,
            "teacher_id": c.teacher_id,
            "teacher_name": c.teacher_name,
        }
        for c in (req.classes or [])
    ]
    payload_members: List[Dict[str, Any]] = [
        {"user_id": m.user_id, "user_name": m.user_name, "class_name": m.class_name}
        for m in (req.memberships or [])
    ]
    summary = svc.sync_legacy(payload_classes, payload_members)
    return {"success": True, "data": summary}
