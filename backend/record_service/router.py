# -*- coding: utf-8 -*-
"""错题记录同步服务。

存在的原因：
    错题记录原先只写浏览器 localStorage（`bugmind_records`），教师端读的也是
    同一份 localStorage。学生和老师不在同一台电脑 / 同一个浏览器时，学生新提交
    的错题永远到不了教师端——这就是「学生提交了错题，教师端数据不更新」的根因。
    这个模块把记录放到后端存一份，学生提交时上传、教师打开主页时下拉合并。

可见范围（教师）：
    只有「自己创建的班级」的记录可见。判定按两层：
        1. 记录里的 class_id 属于该教师的某个班级；
        2. 或记录的学号在该教师班级的成员表（user_class_rel）里。
    第二层用于兜底历史数据：学生记录里存的班级名称/ID 可能与班级表对不上，
    只要他确实是这个班的学生，记录也要能看见。

去重策略（两层）：
    1. 记录自带 id（前端生成的 `r<时间戳><随机>`）相同 → 视为同一条；
    2. id 不同但「学号 + 日期 + 知识点 + 错误摘要」相同 → 视为同一条（历史种子
       数据在不同浏览器里各自生成过 id，靠这一层才不会重复计数）。

落盘：后端/data/records.json
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from homework_service.store import mutate, new_id, now_str, read_table

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

MAX_BATCH = 500
MAX_LIMIT = 5000

# 单字段字符串长度上限：防止异常超长字段撑爆 records.json（正常前端输入远小于上限）
_FIELD_MAX = {
    "id": 64, "student_id": 64, "student_name": 64, "class_name": 128,
    "class_id": 128, "course": 64, "date": 32, "language": 32,
    "error_type": 64, "knowledge_point": 256, "error_summary": 4000,
    "resolved": 32, "code": 30000, "error_message": 30000, "source": 64,
}

# since 只接受日期或日期时间两种写法（与记录里 date 字段的格式一致）
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")

FIELDS = (
    "id", "student_id", "student_name", "class_name", "class_id", "course",
    "date", "language", "error_type", "knowledge_point", "error_summary",
    "repeat_count", "hint_level", "resolved", "code", "error_message", "source",
)


class RecordItem(BaseModel):
    id: Optional[str] = None
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    class_name: Optional[str] = None
    class_id: Optional[str] = None
    course: Optional[str] = None
    date: Optional[str] = None
    language: Optional[str] = None
    error_type: Optional[str] = None
    knowledge_point: Optional[str] = None
    error_summary: Optional[str] = None
    repeat_count: Optional[int] = None
    hint_level: Optional[int] = None
    resolved: Optional[str] = None
    code: Optional[str] = None
    error_message: Optional[str] = None
    source: Optional[str] = None


class RecordsPost(BaseModel):
    records: Optional[List[RecordItem]] = None
    record: Optional[RecordItem] = None
    student_id: Optional[str] = None
    class_name: Optional[str] = None


def record_sig(row: Dict[str, Any]) -> str:
    raw = "|".join(str(row.get(k) or "") for k in ("student_id", "date", "knowledge_point", "error_summary"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _clean(item: RecordItem, fallback_student: str = "", fallback_class: str = "") -> Dict[str, Any]:
    data = item.model_dump(exclude_none=True)
    out: Dict[str, Any] = {}
    for key in FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ""):
            out[key] = value
    if not out.get("student_id"):
        out["student_id"] = (fallback_student or "").strip()
    if not out.get("class_name"):
        out["class_name"] = (fallback_class or "").strip() or str(out.get("class_id") or "")
    return out


# ==================== 教师可见范围 ====================
def _teacher_scope(teacher_id: str) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, set]]:
    """返回 (班级名 → 班级记录, 班级ID → 学生学号集合)。

    可见范围 = 我当主讲的班 + 班级池里的全部班（池班对每位教师只读可见）。
    """
    classes = []
    for c in read_table("classes"):
        main_id = str(c.get("main_teacher_id") or c.get("teacher_id") or "")
        scope = str(c.get("scope") or ("private" if main_id else "pool"))
        if main_id == teacher_id or scope == "pool":
            classes.append(c)
    by_name = {str(c.get("name") or ""): c for c in classes}
    class_ids = {str(c.get("class_id") or "") for c in classes}
    roster: Dict[str, set] = {cid: set() for cid in class_ids if cid}
    for rel in read_table("user_class_rel"):
        cid = str(rel.get("class_id") or "")
        if cid in roster:
            uid = str(rel.get("user_id") or "")
            if uid:
                roster[cid].add(uid)
    return by_name, roster


def _visible_rows(rows: List[Dict[str, Any]], by_name: Dict[str, Dict[str, Any]], roster: Dict[str, set]) -> List[Dict[str, Any]]:
    """按班级名判定可见性。

    记录里的 class_id / class_name 存的是班级名字（前端既有数据模型就是这样），
    所以这里以「名字」为准；名字能对上就可见，并用教师的班级名做一次归一化。
    只有记录完全没带班级信息时，才退回成员表兜底（且仅当该教师只有一个班）。
    """
    names = {str(k) for k in by_name.keys() if k}
    only_name = next(iter(names)) if len(names) == 1 else ""
    sid2name: Dict[str, str] = {}
    for cid, members in roster.items():
        cls = next((c for c in by_name.values() if str(c.get("class_id") or "") == cid), None)
        name = str((cls or {}).get("name") or "")
        for sid in members:
            sid2name.setdefault(sid, name)

    out: List[Dict[str, Any]] = []
    for r in rows:
        known = str(r.get("class_id") or r.get("class_name") or "")
        item = dict(r)
        if known in names:
            item["class_name"] = known
            out.append(item)
            continue
        if not known:
            fixed = sid2name.get(str(r.get("student_id") or "")) or only_name
            if fixed in names:
                item["class_id"] = fixed
                item["class_name"] = fixed
                out.append(item)
    return out


@router.post("/records")
def upsert_records(req: RecordsPost):
    """学生提交错题后批量上报（幂等：同一条记录重复上报不会产生副本）。"""
    batch = list(req.records or [])
    if req.record:
        batch.append(req.record)
    batch = batch[:MAX_BATCH]
    if not batch:
        raise HTTPException(status_code=400, detail="没有可写入的错题记录。")

    cleaned = [_clean(item, req.student_id or "", req.class_name or "") for item in batch]
    cleaned = [c for c in cleaned if c.get("student_id")]
    if not cleaned:
        raise HTTPException(status_code=400, detail="错题记录缺少学号（student_id）。")
    for row in cleaned:
        if not row.get("knowledge_point"):
            raise HTTPException(status_code=400, detail="错题记录缺少知识点（knowledge_point）。")

    # 字段越界防护：字符串超长 / 数值为负 → 400，防止恶意或异常数据撑爆 JSON 表
    for row in cleaned:
        for key, limit in _FIELD_MAX.items():
            value = row.get(key)
            if isinstance(value, str) and len(value) > limit:
                raise HTTPException(status_code=400, detail=f"字段 {key} 内容过长（最多 {limit} 字符）。")
        for key in ("repeat_count", "hint_level"):
            value = row.get(key)
            if isinstance(value, int) and value < 0:
                raise HTTPException(status_code=400, detail=f"字段 {key} 不能为负数。")

    def apply(rows: List[Dict[str, Any]]):
        by_id = {str(r.get("id")): r for r in rows if r.get("id")}
        by_sig = {str(r.get("sig")): r for r in rows if r.get("sig")}
        added = skipped = 0
        for row in cleaned:
            sig = record_sig(row)
            rid = str(row.get("id") or "")
            hit = by_id.get(rid) if rid else None
            if hit is None:
                hit = by_sig.get(sig)
            if hit is not None:
                # 只同步会变化的字段，避免老数据被历史版本覆盖
                for key in ("resolved", "hint_level", "repeat_count", "student_name", "class_name", "class_id"):
                    if key in row:
                        hit[key] = row[key]
                skipped += 1
                continue
            new_row = dict(row)
            new_row["id"] = rid or new_id("rec")
            new_row["sig"] = sig
            new_row["server_at"] = now_str()
            rows.append(new_row)
            by_id[str(new_row["id"])] = new_row
            by_sig[sig] = new_row
            added += 1
        return {"added": added, "skipped": skipped, "stored": len(rows)}

    try:
        stat = mutate("records", apply)
    except OSError:
        # 数据文件 IO 异常：不把底层细节透给前端，只记日志并给统一提示
        logger.exception("错题记录写入失败（数据文件 IO 异常）")
        raise HTTPException(status_code=500, detail="数据保存失败，请稍后重试")
    logger.info(
        "错题记录批量写入完成：新增 %s 条，跳过 %s 条，表内共 %s 条",
        stat["added"], stat["skipped"], stat["stored"],
    )
    return {"success": True, **stat}


@router.get("/records")
def list_records(
    teacher_id: str = "",
    class_name: str = "",
    student_id: str = "",
    since: str = "",
    limit: int = MAX_LIMIT,
):
    """教师下拉班级错题 / 学生回填自己错题。teacher_id 用教师的登录号（如 T001）。"""
    teacher_id = (teacher_id or "").strip()
    class_name = (class_name or "").strip()
    student_id = (student_id or "").strip()

    # 查询参数防护：长度上限（400）；since 格式非法直接 400，避免静默返回空结果
    if len(teacher_id) > 64:
        raise HTTPException(status_code=400, detail="查询参数 teacher_id 过长。")
    if len(class_name) > 128:
        raise HTTPException(status_code=400, detail="查询参数 class_name 过长。")
    if len(student_id) > 64:
        raise HTTPException(status_code=400, detail="查询参数 student_id 过长。")
    if since:
        since = since.strip()
        if not _SINCE_RE.fullmatch(since):
            raise HTTPException(
                status_code=400,
                detail="since 参数格式不正确，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。",
            )

    rows = read_table("records")
    if student_id:
        rows = [r for r in rows if str(r.get("student_id") or "") == student_id]

    if teacher_id:
        by_name, roster = _teacher_scope(teacher_id)
        if not by_name:
            # 既有决策：教师名下暂无班级时返回空列表（前端以此渲染空状态），保持不动
            return {"success": True, "items": [], "total": 0, "classes": []}
        if class_name and class_name not in by_name:
            raise HTTPException(status_code=403, detail="这个班级不属于当前教师。")
        rows = _visible_rows(rows, by_name, roster)
        if class_name:
            rows = [r for r in rows if str(r.get("class_name") or "") == class_name]

    if since:
        rows = [r for r in rows if str(r.get("date") or "") >= since]

    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("server_at") or "")))
    total = len(rows)
    limit = max(1, min(int(limit or MAX_LIMIT), MAX_LIMIT))
    items = rows[-limit:]

    counts: Dict[str, int] = {}
    for r in rows:
        key = str(r.get("class_name") or r.get("class_id") or "")
        counts[key] = counts.get(key, 0) + 1
    classes = [{"name": k, "count": v} for k, v in sorted(counts.items()) if k]
    return {"success": True, "items": items, "total": total, "classes": classes}


@router.get("/records/summary")
def records_summary(teacher_id: str = ""):
    """同步状态自查：每个班级已有多少条记录、多少名学生、最近一条的时间。"""
    teacher_id = (teacher_id or "").strip()
    if len(teacher_id) > 64:
        raise HTTPException(status_code=400, detail="查询参数 teacher_id 过长。")
    rows = read_table("records")
    if teacher_id:
        by_name, roster = _teacher_scope(teacher_id)
        if not by_name:
            # 既有决策：教师名下暂无班级时返回空统计，保持不动
            return {"success": True, "total": 0, "classes": []}
        rows = _visible_rows(rows, by_name, roster)

    by_class: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = str(r.get("class_name") or r.get("class_id") or "未知班级")
        slot = by_class.setdefault(key, {"class_name": key, "count": 0, "students": set(), "latest": ""})
        slot["count"] += 1
        slot["students"].add(str(r.get("student_id") or ""))
        latest = str(r.get("date") or "") + " " + str(r.get("server_at") or "")
        if latest > slot["latest"]:
            slot["latest"] = latest
    for slot in by_class.values():
        slot["students"] = len(slot["students"])
    return {"success": True, "total": len(rows), "classes": list(by_class.values())}
