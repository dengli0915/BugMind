# -*- coding: utf-8 -*-
"""好友体系业务逻辑：申请、收件箱、回复、好友列表。

为什么需要后端这一层：
    前端的好友数据原本只存在浏览器 localStorage 里，A 同学在电脑甲发出申请，
    B 同学在电脑乙（或另一个浏览器）打开页面时根本读不到这条记录，
    于是出现「发了申请对方看不到」。这里把申请与好友关系落到后端 JSON 表，
    任何设备登录都能读到同一份数据。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from homework_service.store import mutate, read_table, write_table

logger = logging.getLogger(__name__)

REQ_TABLE = "friend_requests"
FRIEND_TABLE = "friendships"

MAX_FRIENDS = 500
MAX_PENDING = 50

# 入参字符串长度上限：防止异常超长字符串写入 JSON 表（校验失败由路由层转 400）
_MAX_ID_LEN = 64
_MAX_NAME_LEN = 64
_MAX_CLASS_LEN = 128


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _s(v: Any) -> str:
    return str(v or "").strip()


def _day(ts: str) -> str:
    return (ts or "")[:10]


# ---------- 好友关系 ----------

def list_friends(user_id: str) -> List[Dict[str, Any]]:
    uid = _s(user_id)
    if not uid:
        return []
    rows = [r for r in read_table(FRIEND_TABLE) if _s(r.get("user_id")) == uid]
    rows.sort(key=lambda r: _s(r.get("created_at")))
    return [
        {
            "uid": _s(r.get("friend_id")),
            "name": _s(r.get("friend_name")),
            "cls": _s(r.get("friend_class")),
            "since": _s(r.get("created_at")),
        }
        for r in rows
    ]


def is_friend(user_id: str, other_id: str) -> bool:
    uid, oid = _s(user_id), _s(other_id)
    if not uid or not oid:
        return False
    return any(
        _s(r.get("user_id")) == uid and _s(r.get("friend_id")) == oid
        for r in read_table(FRIEND_TABLE)
    )


# ---------- 申请 ----------

def send_request(
    from_id: str,
    from_name: str = "",
    from_class: str = "",
    to_id: str = "",
    to_name: str = "",
    to_class: str = "",
) -> Dict[str, Any]:
    fid, tid = _s(from_id), _s(to_id)
    if not fid or not tid:
        raise ValueError("缺少发起人或接收人")
    if fid == tid:
        raise ValueError("不能添加自己为好友")
    # 长度上限校验：超长字符串会撑爆 JSON 表，且前端正常输入远小于这些上限
    if len(fid) > _MAX_ID_LEN or len(tid) > _MAX_ID_LEN:
        raise ValueError("用户编号过长")
    if len(_s(from_name)) > _MAX_NAME_LEN or len(_s(to_name)) > _MAX_NAME_LEN:
        raise ValueError("姓名过长")
    if len(_s(from_class)) > _MAX_CLASS_LEN or len(_s(to_class)) > _MAX_CLASS_LEN:
        raise ValueError("班级名称过长")

    def _work(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 已是好友
        friends = read_table(FRIEND_TABLE)
        if any(
            _s(r.get("user_id")) == fid and _s(r.get("friend_id")) == tid
            for r in friends
        ):
            return {"ok": True, "duplicated": True, "is_friend": True,
                    "request": None, "message": "你们已经是好友了"}

        pending = [
            r for r in rows
            if r.get("status") == "pending"
            and _s(r.get("from_id")) == fid and _s(r.get("to_id")) == tid
        ]
        if pending:
            return {"ok": True, "duplicated": True, "is_friend": False,
                    "request": pending[0], "message": "申请已发送，等待对方处理"}

        if len([r for r in rows if _s(r.get("to_id")) == tid and r.get("status") == "pending"]) >= MAX_PENDING:
            raise ValueError("对方待处理申请过多，请稍后再试")

        item = {
            "id": uuid.uuid4().hex[:12],
            "from_id": fid,
            "from_name": _s(from_name),
            "from_class": _s(from_class),
            "to_id": tid,
            "to_name": _s(to_name),
            "to_class": _s(to_class),
            "status": "pending",
            "created_at": _now(),
            "replied_at": "",
        }
        rows.append(item)
        return {"ok": True, "duplicated": False, "is_friend": False,
                "request": item, "message": "好友申请已发送"}

    result = mutate(REQ_TABLE, _work)
    if result.get("duplicated"):
        logger.info("好友申请重复提交，已忽略: %s -> %s", fid, tid)
    else:
        logger.info("好友申请已创建: %s -> %s", fid, tid)
    return result


def list_inbox(user_id: str) -> List[Dict[str, Any]]:
    uid = _s(user_id)
    if not uid:
        return []
    rows = [
        r for r in read_table(REQ_TABLE)
        if _s(r.get("to_id")) == uid and r.get("status") == "pending"
    ]
    rows.sort(key=lambda r: _s(r.get("created_at")), reverse=True)
    return [
        {
            "id": _s(r.get("id")),
            "from": _s(r.get("from_id")),
            "fromName": _s(r.get("from_name")) or _s(r.get("from_id")),
            "fromClass": _s(r.get("from_class")),
            "date": _day(_s(r.get("created_at"))),
            "created_at": _s(r.get("created_at")),
            "status": "pending",
        }
        for r in rows
    ]


def list_outbox(user_id: str) -> List[Dict[str, Any]]:
    uid = _s(user_id)
    if not uid:
        return []
    rows = [r for r in read_table(REQ_TABLE) if _s(r.get("from_id")) == uid]
    rows.sort(key=lambda r: _s(r.get("created_at")), reverse=True)
    return [
        {
            "id": _s(r.get("id")),
            "to": _s(r.get("to_id")),
            "toName": _s(r.get("to_name")) or _s(r.get("to_id")),
            "toClass": _s(r.get("to_class")),
            "date": _day(_s(r.get("created_at"))),
            "status": _s(r.get("status")) or "pending",
        }
        for r in rows
    ]


def reply(request_id: str, user_id: str, accept: bool) -> Dict[str, Any]:
    rid, uid = _s(request_id), _s(user_id)
    if not rid or not uid:
        raise ValueError("缺少申请编号或处理人")
    # 长度上限校验：申请编号/处理人编号由系统生成，正常远小于上限
    if len(rid) > _MAX_ID_LEN or len(uid) > _MAX_ID_LEN:
        raise ValueError("申请编号或用户编号过长")

    def _work(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        target: Optional[Dict[str, Any]] = None
        for r in rows:
            if _s(r.get("id")) == rid:
                target = r
                break
        if target is None:
            raise ValueError("该申请不存在或已被撤回")
        if _s(target.get("to_id")) != uid:
            raise ValueError("无权处理该申请")
        if target.get("status") != "pending":
            raise ValueError("该申请已处理过了")

        target["status"] = "accepted" if accept else "rejected"
        target["replied_at"] = _now()

        if accept:
            friends = read_table(FRIEND_TABLE)
            fid = _s(target.get("from_id"))
            # 好友上限（500）：服务端也做一次硬校验，避免绕过前端直接调接口
            mine = sum(1 for r in friends if _s(r.get("user_id")) == uid)
            if mine >= MAX_FRIENDS:
                raise ValueError(f"好友已达上限 {MAX_FRIENDS} 人，无法再添加")
            pairs = {(fid, uid), (uid, fid)}
            for a, b in pairs:
                exists = any(
                    _s(r.get("user_id")) == a and _s(r.get("friend_id")) == b
                    for r in friends
                )
                if not exists:
                    friends.append({
                        "user_id": a,
                        "friend_id": b,
                        "friend_name": _s(target.get("from_name")) if a == uid else _s(target.get("to_name")),
                        "friend_class": _s(target.get("from_class")) if a == uid else _s(target.get("to_class")),
                        "created_at": _now(),
                    })
            write_table(FRIEND_TABLE, friends)

        return {
            "ok": True,
            "request_id": rid,
            "status": target["status"],
            "from_id": _s(target.get("from_id")),
            "from_name": _s(target.get("from_name")),
            "from_class": _s(target.get("from_class")),
        }

    result = mutate(REQ_TABLE, _work)
    logger.info("好友申请处理完成: %s 由 %s 处理，结果=%s", rid, uid, result.get("status"))
    return result


def pending_count(user_id: str) -> int:
    return len(list_inbox(user_id))
