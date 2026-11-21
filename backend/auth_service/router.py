# -*- coding: utf-8 -*-
"""账号服务路由：/api/auth/*

与前端约定（前端 html 里的账号逻辑完全对齐）：
    密码哈希 = sha256("bugmind_cloud_v2::" + 明文 + "::bugmind_cloud_v2").hexdigest()
    用户对象字段沿用前端命名：username / password / name / role / studentId /
    classId / teacherNo / teacherClass / createdAt / lastActive / rank / pk /
    friends / friendReqs / sentReqs

接口一览：
    GET  /api/auth/ping      探测服务是否可用 + 账号总数（前端据此决定走服务端还是本地）
    POST /api/auth/login     登录。返回 {ok, reason?, user?}
                             reason: not_found（账号不存在）/ bad_password / no_password
    POST /api/auth/register  注册。返回 {ok, reason?}
    POST /api/auth/reset     找回密码（重置为 123456，与前端本地逻辑一致）
    POST /api/auth/change    修改密码（需校验旧密码）
    POST /api/auth/profile   上传用户资料（段位/积分/好友等），用于跨设备恢复
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from homework_service import store

router = APIRouter(prefix="/api/auth", tags=["auth"])

AUTH_SALT = "bugmind_cloud_v2"
DEFAULT_PASSWORD = "123456"
TABLE = "users"

SEED_FILE = store.DATA_DIR / "users_seed.json"

# 前端用户对象里允许通过 /profile 上传的字段（password 单独由 register/change/reset 管）
PROFILE_FIELDS = (
    "name", "role", "studentId", "classId", "teacherNo", "teacherClass",
    "createdAt", "lastActive", "rank", "pk", "friends", "friendReqs", "sentReqs",
)


def hash_pass(plain: str) -> str:
    """与前端 hashPass() 完全一致的密码哈希。"""
    raw = f"{AUTH_SALT}::{plain}::{AUTH_SALT}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_hashed(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


def _rows() -> List[Dict[str, Any]]:
    data = store.read_table(TABLE)
    return data if isinstance(data, list) else []


def _save(rows: List[Dict[str, Any]]) -> None:
    store.write_table(TABLE, rows)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """返回给前端的用户对象：去掉密码哈希。"""
    out = {k: v for k, v in row.items() if k != "password"}
    return out


def _find(rows: List[Dict[str, Any]], account: str) -> Optional[Dict[str, Any]]:
    account = (account or "").strip()
    if not account:
        return None
    for row in rows:
        if row.get("username") == account:
            return row
    for row in rows:
        if row.get("studentId") == account:
            return row
    return None


def _seed_if_empty() -> int:
    """首次启动：把前端种子账号导入服务端账号表（幂等，只在表为空时执行）。"""
    rows = _rows()
    if rows:
        return 0
    if not SEED_FILE.exists():
        return 0
    try:
        import json

        with SEED_FILE.open("r", encoding="utf-8") as fp:
            seed = json.load(fp)
    except (OSError, ValueError):
        return 0
    if not isinstance(seed, list):
        return 0
    out: List[Dict[str, Any]] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for item in seed:
        if not isinstance(item, dict) or not item.get("username"):
            continue
        row = dict(item)
        pw = row.get("password")
        if is_hashed(pw):
            row["password"] = pw.lower()
        elif isinstance(pw, str) and pw:
            row["password"] = hash_pass(pw)
        else:
            row["password"] = hash_pass(DEFAULT_PASSWORD)
        row.setdefault("createdAt", now)
        out.append(row)
    if out:
        _save(out)
    return len(out)


class LoginReq(BaseModel):
    account: str = ""
    password: str = ""


class RegisterReq(BaseModel):
    username: str = ""
    password: str = ""
    name: str = ""
    role: str = "student"
    studentId: Optional[str] = None
    classId: Optional[str] = None
    teacherNo: Optional[str] = None
    teacherClass: Optional[str] = None


class ResetReq(BaseModel):
    account: str = ""
    role: Optional[str] = None
    class_name: Optional[str] = None


class ChangeReq(BaseModel):
    account: str = ""
    old_password: str = ""
    new_password: str = ""


class ProfileReq(BaseModel):
    user: Dict[str, Any] = {}


@router.get("/ping")
def auth_ping():
    seeded = 0
    try:
        seeded = _seed_if_empty()
    except Exception:  # 种子导入失败不影响探测
        seeded = 0
    rows = _rows()
    return {
        "success": True,
        "ok": True,
        "users": len(rows),
        "seeded": seeded,
        "salt": AUTH_SALT,
        "password_rule": "sha256(salt::plain::salt)",
    }


@router.get("/users")
def auth_users(role: str = "", class_name: str = "", limit: int = 5000):
    """只读账号名录：给「班级排行榜 / 全平台排行榜」补段位用。

    榜单原本只认本机 users 表，换台设备就只能看到「暂无积分」；
    这里回一份不含密码的展示字段，前端并进榜单数据源即可跨设备一致。
    """
    _seed_if_empty()
    rows = _rows()
    cls = (class_name or "").strip()
    want_role = (role or "").strip()
    items: List[Dict[str, Any]] = []
    for r in rows:
        if want_role and str(r.get("role") or "") != want_role:
            continue
        if cls and str(r.get("classId") or "") != cls:
            continue
        rank = r.get("rank") if isinstance(r.get("rank"), dict) else {}
        pk = r.get("pk") if isinstance(r.get("pk"), dict) else {}
        items.append({
            "username": r.get("username") or "",
            "name": r.get("name") or r.get("username") or "",
            "role": r.get("role") or "student",
            "studentId": r.get("studentId") or "",
            "classId": r.get("classId") or "",
            "teacherNo": r.get("teacherNo") or "",
            "teacherClass": r.get("teacherClass") or "",
            "rank": rank,
            "pk": pk,
            "lastActive": r.get("lastActive") or "",
        })
    try:
        cap = max(1, min(int(limit or 5000), 8000))
    except Exception:
        cap = 5000
    return {"success": True, "total": len(items), "items": items[:cap]}


@router.post("/login")
def auth_login(req: LoginReq):
    _seed_if_empty()
    rows = _rows()
    row = _find(rows, req.account)
    if row is None:
        return {"ok": False, "reason": "not_found", "success": False}
    stored = row.get("password")
    if not stored:
        return {"ok": False, "reason": "no_password", "success": False}
    if not is_hashed(stored):
        # 历史明文：就地升级为哈希（与前端 legacy 升级一致）
        if stored == req.password:
            row["password"] = hash_pass(req.password)
            _save(rows)
            row["lastActive"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return {"ok": True, "success": True, "user": _public(row), "upgraded": True}
        return {"ok": False, "reason": "bad_password", "success": False}
    if stored != hash_pass(req.password):
        return {"ok": False, "reason": "bad_password", "success": False}
    row["lastActive"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(rows)
    return {"ok": True, "success": True, "user": _public(row)}


@router.post("/register")
def auth_register(req: RegisterReq):
    _seed_if_empty()
    rows = _rows()
    username = (req.username or "").strip()
    if not username:
        return {"ok": False, "reason": "empty_account", "success": False}
    if len((req.password or "").strip()) < 4:
        return {"ok": False, "reason": "bad_password", "success": False}
    if _find(rows, username) is not None:
        return {"ok": False, "reason": "exists", "success": False}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    row: Dict[str, Any] = {
        "username": username,
        "password": hash_pass(req.password),
        "name": req.name or username,
        "role": "teacher" if req.role == "teacher" else "student",
        "createdAt": now,
        "lastActive": now,
    }
    if req.studentId:
        row["studentId"] = req.studentId
    if req.classId:
        row["classId"] = req.classId
    if req.teacherNo:
        row["teacherNo"] = req.teacherNo
    if req.teacherClass:
        row["teacherClass"] = req.teacherClass
    rows.append(row)
    _save(rows)
    return {"ok": True, "success": True, "user": _public(row)}


@router.post("/reset")
def auth_reset(req: ResetReq):
    _seed_if_empty()
    rows = _rows()
    row = _find(rows, req.account)
    if row is None:
        return {"ok": False, "reason": "not_found", "success": False}
    row["password"] = hash_pass(DEFAULT_PASSWORD)
    row["lastActive"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(rows)
    return {"ok": True, "success": True, "password": DEFAULT_PASSWORD, "user": _public(row)}


@router.post("/change")
def auth_change(req: ChangeReq):
    _seed_if_empty()
    rows = _rows()
    row = _find(rows, req.account)
    if row is None:
        return {"ok": False, "reason": "not_found", "success": False}
    new_password = (req.new_password or "").strip()
    if len(new_password) < 4:
        return {"ok": False, "reason": "bad_password", "success": False}
    stored = row.get("password")
    if stored and is_hashed(stored) and stored != hash_pass(req.old_password):
        return {"ok": False, "reason": "bad_password", "success": False}
    if stored and not is_hashed(stored) and stored != req.old_password:
        return {"ok": False, "reason": "bad_password", "success": False}
    row["password"] = hash_pass(new_password)
    row["lastActive"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(rows)
    return {"ok": True, "success": True}


@router.post("/profile")
def auth_profile(req: ProfileReq):
    """上传用户资料（段位、积分、好友等），用于跨设备恢复与教师端视角一致。"""
    incoming = req.user if isinstance(req.user, dict) else {}
    account = (incoming.get("username") or "").strip()
    if not account:
        return {"ok": False, "reason": "empty_account", "success": False}
    _seed_if_empty()
    rows = _rows()
    row = _find(rows, account)
    if row is None:
        # 服务端还没有这个账号：只在本地账号迁移时创建（带密码）
        pw = incoming.get("password")
        if not (isinstance(pw, str) and pw):
            return {"ok": False, "reason": "not_found", "success": False}
        row = {"username": account, "password": pw if is_hashed(pw) else hash_pass(pw)}
        rows.append(row)
    for field in PROFILE_FIELDS:
        if field in incoming and incoming[field] is not None:
            row[field] = incoming[field]
    row["lastActive"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(rows)
    return {"ok": True, "success": True, "user": _public(row)}
