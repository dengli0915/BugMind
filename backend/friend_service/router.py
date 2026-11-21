# -*- coding: utf-8 -*-
"""好友体系 HTTP 接口（APIRouter, prefix="/api"）。

接口一览
    POST /api/friends/request        发起好友申请
    GET  /api/friends/inbox?user_id= 我收到的待处理申请
    GET  /api/friends/outbox?user_id=我发出的申请与状态
    POST /api/friends/reply          同意 / 拒绝
    GET  /api/friends/list?user_id=  我的好友（已通过）
    GET  /api/friends/summary?user_id= 待处理数量（角标用）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class SendRequest(BaseModel):
    from_id: Optional[str] = None
    from_name: Optional[str] = None
    from_class: Optional[str] = None
    to_id: Optional[str] = None
    to_name: Optional[str] = None
    to_class: Optional[str] = None


class ReplyRequest(BaseModel):
    request_id: Optional[str] = None
    user_id: Optional[str] = None
    accept: Optional[bool] = True


@router.post("/friends/request")
def api_send_request(body: SendRequest) -> Dict[str, Any]:
    try:
        res = svc.send_request(
            body.from_id or "",
            body.from_name or "",
            body.from_class or "",
            body.to_id or "",
            body.to_name or "",
            body.to_class or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError:
        # 数据文件 IO 异常：不把底层细节透给前端，只记日志并给统一提示
        logger.exception("好友申请写入失败（数据文件 IO 异常）")
        raise HTTPException(status_code=500, detail="数据保存失败，请稍后重试")
    return {"success": True, **res}


@router.get("/friends/inbox")
def api_inbox(user_id: str = "") -> Dict[str, Any]:
    items = svc.list_inbox(user_id)
    return {"success": True, "items": items, "count": len(items)}


@router.get("/friends/outbox")
def api_outbox(user_id: str = "") -> Dict[str, Any]:
    items = svc.list_outbox(user_id)
    return {"success": True, "items": items, "count": len(items)}


@router.post("/friends/reply")
def api_reply(body: ReplyRequest) -> Dict[str, Any]:
    try:
        # accept 显式传 null 时按“未传”处理（默认同意）；bool(None) 会误判成拒绝
        accept = True if body.accept is None else bool(body.accept)
        res = svc.reply(body.request_id or "", body.user_id or "", accept)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError:
        logger.exception("好友申请处理写入失败（数据文件 IO 异常）")
        raise HTTPException(status_code=500, detail="数据保存失败，请稍后重试")
    return {"success": True, **res}


@router.get("/friends/list")
def api_friends(user_id: str = "") -> Dict[str, Any]:
    items = svc.list_friends(user_id)
    return {"success": True, "items": items, "count": len(items)}


@router.get("/friends/summary")
def api_summary(user_id: str = "") -> Dict[str, Any]:
    items = svc.list_inbox(user_id)
    return {"success": True, "pending": len(items)}
