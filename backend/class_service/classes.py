# -*- coding: utf-8 -*-
"""班级数据层：班级码生成、班级 CRUD、成员关联、历史数据迁移。

表结构（与需求一致，落在 homework_service.store 的本地 JSON 表里，
日后 Supabase 建好 classes / user_class_rel 两张表时只需替换 store 层）：

    classes:          class_id, name, teacher_id, teacher_name, code, created_at, source
    user_class_rel:   user_id, class_id, user_name, joined_at   （user_id + class_id 唯一）
"""

from __future__ import annotations

import random
import string
from typing import Any, Dict, List, Optional

from homework_service.store import mutate, new_id, now_str, read_table

CLASS_TABLES = ("classes", "user_class_rel")

# 去掉 I / O / 0 / 1，避免口头或手抄班级码时认错
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 6

_MAX_CLASSES_PER_TEACHER = 50
_MAX_CLASSES_PER_USER = 20
# 名称长度上限：与路由层校验保持一致，防止异常长名称撑爆 JSON 存储
_MAX_NAME_LEN = 40


# ---------------------------------------------------------------- 班级码
def _existing_codes(rows: List[Dict[str, Any]]) -> set:
    return {str(r.get("code") or "").upper() for r in rows if r.get("code")}


def generate_class_code(existing: Optional[set] = None) -> str:
    """生成全局唯一的 6 位数字字母混合班级码。"""
    existing = existing if existing is not None else _existing_codes(read_table("classes"))
    for _ in range(200):
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in existing:
            return code
    # 极端情况：随机空间撞满，退化到时间戳后缀保证唯一
    while True:
        code = ("C" + str(int(random.random() * 1e9))[-5:]).upper()
        if code not in existing:
            return code


# ---------------------------------------------------------------- 查询
def _main_tid(row: Dict[str, Any]) -> str:
    """班级的主讲教师 id。

    老数据（本次改造前创建）没有 main_teacher_id，主讲就是 teacher_id；
    改造后的班级以 main_teacher_id 为准，池里未认领的班级为空串。
    """
    return str(row.get("main_teacher_id") or row.get("teacher_id") or "")


def _scope_of(row: Dict[str, Any]) -> str:
    """班级可见范围：pool=班级池（全平台教师可见）/ private=私密班。"""
    scope = str(row.get("scope") or "").strip().lower()
    if scope in ("pool", "private"):
        return scope
    # 老数据没有 scope：跟着主讲走，有主讲视为私密班，没主讲（异常）当池班
    return "private" if _main_tid(row) else "pool"


def _public_class(row: Dict[str, Any], student_count: int = 0) -> Dict[str, Any]:
    main_id = _main_tid(row)
    return {
        "class_id": row.get("class_id"),
        "name": row.get("name"),
        "teacher_id": row.get("teacher_id"),
        "teacher_name": row.get("teacher_name"),
        "code": row.get("code"),
        "created_at": row.get("created_at"),
        "source": row.get("source") or "manual",
        "student_count": student_count,
        # ---- 班级池相关（V30）----
        "scope": _scope_of(row),
        "is_pool": _scope_of(row) == "pool",
        "main_teacher_id": main_id,
        "main_teacher_name": str(row.get("main_teacher_name") or row.get("teacher_name") or ""),
        "claimed": bool(main_id),
        "owner_id": str(row.get("owner_id") or ""),
        "owner_name": str(row.get("owner_name") or ""),
        "owner_role": str(row.get("owner_role") or ""),
        "claimed_at": row.get("claimed_at") or "",
    }


def _count_map(rels: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rels:
        cid = r.get("class_id")
        if cid:
            out[cid] = out.get(cid, 0) + 1
    return out


def class_by_id(class_id: str) -> Optional[Dict[str, Any]]:
    for row in read_table("classes"):
        if row.get("class_id") == class_id:
            return row
    return None


def class_by_name(name: str, teacher_id: str = "") -> Optional[Dict[str, Any]]:
    """按班级名查班级（可选限定教师）。同名多班时返回最早创建的那个。"""
    name = (name or "").strip()
    if not name:
        return None
    teacher_id = (teacher_id or "").strip()
    hit = [
        r
        for r in read_table("classes")
        if str(r.get("name") or "") == name
        and (not teacher_id or str(r.get("teacher_id") or "") == teacher_id)
    ]
    hit.sort(key=lambda r: str(r.get("created_at") or ""))
    return hit[0] if hit else None


def resolve_class(class_id: str = "", class_name: str = "") -> Optional[Dict[str, Any]]:
    """把前端传来的「班级 ID 或班级名」统一解析成班级行（兼容老数据只存班级名）。"""
    row = class_by_id(class_id) if class_id else None
    if row:
        return row
    if class_id:
        row = class_by_name(class_id)
        if row:
            return row
    if class_name:
        return class_by_name(class_name)
    return None


def class_scope_of_user(user_id: str) -> Dict[str, Any]:
    """学生的班级归属：返回 {ids:[...], names:[...]}（用于按班级过滤业务数据）。"""
    user_id = (user_id or "").strip()
    ids: List[str] = []
    names: List[str] = []
    if not user_id:
        return {"ids": ids, "names": names}
    rels = [r for r in read_table("user_class_rel") if str(r.get("user_id") or "") == user_id]
    by_id = {r.get("class_id"): r for r in read_table("classes")}
    for rel in rels:
        cid = rel.get("class_id")
        if not cid:
            continue
        ids.append(str(cid))
        row = by_id.get(cid)
        if row and row.get("name"):
            names.append(str(row.get("name")))
    return {"ids": ids, "names": names}


def class_by_code(code: str) -> Optional[Dict[str, Any]]:
    code = (code or "").strip().upper()
    if not code:
        return None
    for row in read_table("classes"):
        if str(row.get("code") or "").upper() == code:
            return row
    return None


def list_class_summary(rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    rows = rows if rows is not None else read_table("classes")
    counts = _count_map(read_table("user_class_rel"))
    return [_public_class(r, counts.get(r.get("class_id"), 0)) for r in rows]


def classes_of_teacher(teacher_id: str) -> List[Dict[str, Any]]:
    """教师【担任主讲】的班级（含老数据按 teacher_id 认定的班）。"""
    teacher_id = (teacher_id or "").strip()
    rows = [r for r in read_table("classes") if _main_tid(r) == teacher_id]
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return list_class_summary(rows)


def pool_classes() -> List[Dict[str, Any]]:
    """班级池：全部 scope=pool 的班级（已认领 / 未认领都在，前端按 claimed 区分）。"""
    rows = [r for r in read_table("classes") if _scope_of(r) == "pool"]
    rows.sort(key=lambda r: (bool(_main_tid(r)), str(r.get("created_at") or "")))
    return list_class_summary(rows)


def can_delete_class(row: Dict[str, Any], user_id: str) -> bool:
    """删除权限：只有【创建者】能删自己的班。

    - 有 owner_id（教师或学生建班时记录）：必须是本人；
    - 没有 owner_id 的老数据：创建者就是该班的主讲（teacher_id），且不能是平台预置的池班；
    - 平台预置 / 迁移建的池班（source=pool 且无 owner）一律不可删，避免误删公共班级。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return False
    owner = str(row.get("owner_id") or "").strip()
    if owner:
        return owner == user_id
    source = str(row.get("source") or "").strip().lower()
    if source == "pool":
        return False
    main_id = _main_tid(row)
    return bool(main_id) and main_id == user_id


def classes_visible_to_teacher(teacher_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """教师端「我的班级」页数据 = 我当主讲的班 + 班级池（只读）。"""
    teacher_id = (teacher_id or "").strip()
    mine = classes_of_teacher(teacher_id)
    mine_ids = {r.get("class_id") for r in mine}
    pool = [r for r in pool_classes() if r.get("class_id") not in mine_ids]
    rows = {r.get("class_id"): r for r in read_table("classes")}
    for r in mine:
        r["is_mine"] = True
        r["claimable"] = False
        r["can_delete"] = can_delete_class(rows.get(r.get("class_id")) or {}, teacher_id)
    for r in pool:
        r["is_mine"] = False
        # 池里已被人认领的，只能看不能抢
        r["claimable"] = not r.get("claimed")
        r["can_delete"] = can_delete_class(rows.get(r.get("class_id")) or {}, teacher_id)
    return {"mine": mine, "pool": pool}


def classes_of_user(user_id: str) -> List[Dict[str, Any]]:
    user_id = (user_id or "").strip()
    rels = [r for r in read_table("user_class_rel") if str(r.get("user_id") or "") == user_id]
    by_id = {r.get("class_id"): r for r in read_table("classes")}
    out: List[Dict[str, Any]] = []
    for rel in rels:
        row = by_id.get(rel.get("class_id"))
        if row:
            item = _public_class(row, 0)
            item["joined_at"] = rel.get("joined_at")
            out.append(item)
    out.sort(key=lambda r: str(r.get("joined_at") or ""))
    return out


def students_of_class(class_id: str) -> List[Dict[str, Any]]:
    rels = [r for r in read_table("user_class_rel") if r.get("class_id") == class_id]
    rels.sort(key=lambda r: str(r.get("joined_at") or ""))
    return [
        {
            "user_id": r.get("user_id"),
            "user_name": r.get("user_name") or "",
            "joined_at": r.get("joined_at"),
            "class_id": class_id,
        }
        for r in rels
    ]


def is_member(user_id: str, class_id: str) -> bool:
    for r in read_table("user_class_rel"):
        if r.get("user_id") == user_id and r.get("class_id") == class_id:
            return True
    return False


# ---------------------------------------------------------------- 写入
def create_class(
    name: str,
    teacher_id: str,
    teacher_name: str = "",
    scope: str = "private",
    owner_id: str = "",
    owner_name: str = "",
    owner_role: str = "teacher",
) -> Dict[str, Any]:
    """建班。

    scope="private"：私密班，创建教师即主讲，只有本人与班内学生可见；
    scope="pool"   ：投放到班级池，主讲留空，任何教师都能认领；
                     教师自己投放的班 owner 记为自己，方便识别来源。
    owner_role 记录是谁建的（teacher / student），学生建班一律进池等老师认领。
    """
    name = (name or "").strip()
    teacher_id = (teacher_id or "").strip()
    scope = "pool" if str(scope or "").strip().lower() == "pool" else "private"
    owner_id = (owner_id or teacher_id or "").strip()
    owner_name = (owner_name or teacher_name or "").strip()[:_MAX_NAME_LEN]
    owner_role = "student" if str(owner_role or "") == "student" else "teacher"
    # 服务层兜底校验（路由层已拦，这里防其他调用方直接打数据层）
    if not name:
        raise ValueError("请填写班级名称。")
    if len(name) > _MAX_NAME_LEN:
        raise ValueError("班级名称不能超过 %d 个字。" % _MAX_NAME_LEN)
    if not owner_id:
        raise ValueError("缺少创建者标识，请重新登录后再试。")
    teacher_name = (teacher_name or "").strip()[:_MAX_NAME_LEN]
    if scope == "private" and not teacher_id:
        raise ValueError("私密班级缺少教师标识，请重新登录后再试。")

    mine = [
        r
        for r in read_table("classes")
        if _main_tid(r) == teacher_id or (owner_id and str(r.get("owner_id") or "") == owner_id)
    ]
    if len(mine) >= _MAX_CLASSES_PER_TEACHER:
        raise ValueError("每人最多创建/认领 %d 个班级" % _MAX_CLASSES_PER_TEACHER)
    if any(str(r.get("name") or "") == name for r in mine):
        raise ValueError("你已经创建过名为「%s」的班级" % name)

    def _do(rows):
        # 锁内复查：并发建班时也不会出现同名
        if any(
            str(r.get("name") or "") == name
            and (_main_tid(r) == teacher_id or str(r.get("owner_id") or "") == owner_id)
            for r in rows
        ):
            raise ValueError("你已经创建过名为「%s」的班级" % name)
        if scope == "pool" and any(
            str(r.get("name") or "") == name and _scope_of(r) == "pool" for r in rows
        ):
            raise ValueError("班级池里已经有名为「%s」的班级了" % name)
        code = generate_class_code(_existing_codes(rows))
        row = {
            "class_id": new_id("C"),
            "name": name,
            "teacher_id": teacher_id if scope == "private" else "",
            "teacher_name": teacher_name if scope == "private" else "",
            "main_teacher_id": teacher_id if scope == "private" else "",
            "main_teacher_name": teacher_name if scope == "private" else "",
            "scope": scope,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "owner_role": owner_role,
            "claimed_at": now_str() if scope == "private" else "",
            "code": code,
            "created_at": now_str(),
            "source": "manual" if owner_role == "teacher" else "student",
        }
        rows.append(row)
        return row

    return _public_class(mutate("classes", _do), 0)


def claim_class(class_id: str, teacher_id: str, teacher_name: str = "") -> Dict[str, Any]:
    """认领班级池里的班：原子操作，一个班同一时间只能有一个主讲。"""
    class_id = (class_id or "").strip()
    teacher_id = (teacher_id or "").strip()
    teacher_name = (teacher_name or "").strip()[:_MAX_NAME_LEN]
    if not class_id or not teacher_id:
        raise ValueError("缺少班级或教师标识，请重新登录后再试。")

    def _do(rows):
        for row in rows:
            if row.get("class_id") != class_id:
                continue
            if _scope_of(row) != "pool":
                raise ValueError("这个班级是私密班，不能认领。")
            current = _main_tid(row)
            if current and current != teacher_id:
                raise ValueError(
                    "这个班已经有主讲老师了（%s），不能重复认领。"
                    % (str(row.get("main_teacher_name") or row.get("teacher_name") or "其他老师"))
                )
            if current == teacher_id:
                return row  # 已经是自己的班，幂等
            row["main_teacher_id"] = teacher_id
            row["main_teacher_name"] = teacher_name
            row["claimed_at"] = now_str()
            # 老字段同步一份，兼容按 teacher_id 查库的老接口
            row["teacher_id"] = teacher_id
            row["teacher_name"] = teacher_name
            return row
        raise ValueError("班级不存在或已被删除。")

    return _public_class(mutate("classes", _do), 0)


def release_class(class_id: str, teacher_id: str) -> Dict[str, Any]:
    """放弃主讲：班级回到池里（无人认领），其他人可以认领。"""
    class_id = (class_id or "").strip()
    teacher_id = (teacher_id or "").strip()

    def _do(rows):
        for row in rows:
            if row.get("class_id") != class_id:
                continue
            if _main_tid(row) != teacher_id:
                raise ValueError("只有这个班的主讲老师才能放弃。")
            row["main_teacher_id"] = ""
            row["main_teacher_name"] = ""
            row["teacher_id"] = ""
            row["teacher_name"] = ""
            row["claimed_at"] = ""
            row["scope"] = "pool"
            return row
        raise ValueError("班级不存在或已被删除。")

    return _public_class(mutate("classes", _do), 0)


def set_main_teacher(class_id: str, teacher_id: str, teacher_name: str = "") -> bool:
    """直接把班级指定给某位教师（迁移脚本用，不做认领校验）。"""
    def _do(rows):
        for row in rows:
            if row.get("class_id") != class_id:
                continue
            row["main_teacher_id"] = teacher_id
            row["main_teacher_name"] = teacher_name
            row["teacher_id"] = teacher_id
            row["teacher_name"] = teacher_name
            row["claimed_at"] = now_str() if teacher_id else ""
            return True
        return False

    return bool(mutate("classes", _do))


def add_member(user_id: str, class_id: str, user_name: str = "") -> bool:
    """加入班级；已在班里返回 False（由调用方给出友好提示）。"""
    user_id = (user_id or "").strip()
    # 空标识不落库：避免写入 class_id/user_id 为空的脏关联
    if not user_id or not class_id:
        return False
    if is_member(user_id, class_id):
        return False

    def _do(rows):
        if any(r.get("user_id") == user_id and r.get("class_id") == class_id for r in rows):
            return False
        mine = [r for r in rows if r.get("user_id") == user_id]
        if len(mine) >= _MAX_CLASSES_PER_USER:
            raise ValueError("每位同学最多加入 %d 个班级" % _MAX_CLASSES_PER_USER)
        rows.append(
            {
                "user_id": user_id,
                "class_id": class_id,
                "user_name": (user_name or "").strip()[:_MAX_NAME_LEN],
                "joined_at": now_str(),
            }
        )
        return True

    return bool(mutate("user_class_rel", _do))


def remove_member(user_id: str, class_id: str) -> bool:
    def _do(rows):
        before = len(rows)
        rows[:] = [r for r in rows if not (r.get("user_id") == user_id and r.get("class_id") == class_id)]
        return len(rows) != before

    return bool(mutate("user_class_rel", _do))


def delete_class(class_id: str, teacher_id: str) -> bool:
    """删除班级（仅主讲教师本人），同时清空成员关联。"""
    removed = mutate("classes", lambda rows: _remove_class(rows, class_id, teacher_id))
    if removed:
        def _purge(rows):
            rows[:] = [r for r in rows if r.get("class_id") != class_id]
            return None

        mutate("user_class_rel", _purge)
    return bool(removed)


def delete_by_owner(class_id: str, user_id: str) -> Dict[str, Any]:
    """只有创建者能删除自己的班级；返回被删除的班级行，不满足条件抛 ValueError。

    与 delete_class 的区别：不看「谁是主讲」，只看「谁建的」，所以老师不能删别人
    创建（自己后来认领）的班，也删不掉平台预置 / 迁移生成的班级池班级。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        raise ValueError("缺少账号信息，无法删除班级。")
    row = class_by_id(class_id)
    if not row:
        raise ValueError("班级不存在或已被删除。")
    if not can_delete_class(row, user_id):
        if str(row.get("owner_id") or "").strip():
            raise ValueError("这个班级不是你创建的，只有创建者才能删除。")
        raise ValueError("这是平台预置的班级，不能删除。")
    delete_class_any(class_id)
    return row


def _remove_class(rows: List[Dict[str, Any]], class_id: str, teacher_id: str) -> bool:
    for i, row in enumerate(rows):
        if row.get("class_id") != class_id:
            continue
        if _main_tid(row) == teacher_id or str(row.get("teacher_id") or "") == teacher_id:
            rows.pop(i)
            return True
    return False


def delete_class_any(class_id: str) -> bool:
    """无条件删除班级（迁移脚本 / 运维清理用，不做归属校验）。"""
    removed = mutate("classes", lambda rows: _remove_any_class(rows, class_id))
    if removed:
        def _purge(rows):
            rows[:] = [r for r in rows if r.get("class_id") != class_id]
            return None

        mutate("user_class_rel", _purge)
    return bool(removed)


def _remove_any_class(rows: List[Dict[str, Any]], class_id: str) -> bool:
    for i, row in enumerate(rows):
        if row.get("class_id") == class_id:
            rows.pop(i)
            return True
    return False


def sync_legacy(
    classes: List[Dict[str, Any]], memberships: List[Dict[str, Any]]
) -> Dict[str, int]:
    """历史数据迁移：把前端 users 表里的 classId / teacherClass 落成班级与关联。

    - classes:     [{name, teacher_id, teacher_name}]     （教师自己的班级，按名称去重）
    - memberships: [{user_id, user_name, class_name}]     （学生 → 班级名）
    幂等：重复调用不会产生重复班级或重复关联。
    """
    created_classes = 0
    created_links = 0
    builtin = 0

    def _do_classes(rows):
        nonlocal created_classes, builtin
        existing_codes = _existing_codes(rows)
        for item in classes or []:
            name = str(item.get("name") or "").strip()
            teacher_id = str(item.get("teacher_id") or "").strip()
            if not name or not teacher_id:
                continue
            hit = next(
                (
                    r
                    for r in rows
                    if str(r.get("name") or "") == name
                    and str(r.get("teacher_id") or "") == teacher_id
                ),
                None,
            )
            if hit:
                builtin += 1
                continue
            code = generate_class_code(existing_codes)
            existing_codes.add(code)
            rows.append(
                {
                    "class_id": new_id("C"),
                    "name": name,
                    "teacher_id": teacher_id,
                    "teacher_name": str(item.get("teacher_name") or "").strip(),
                    "code": code,
                    "created_at": now_str(),
                    "source": "legacy",
                }
            )
            created_classes += 1
        return None

    mutate("classes", _do_classes)

    # class_name → 班级行列表（同名可能存在多个教师建的班：历史学生只有班级名，
    # 无法判断归属，全部落位，保证每个同名班都能看到这批历史学生）
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for row in sorted(read_table("classes"), key=lambda r: str(r.get("created_at") or "")):
        by_name.setdefault(str(row.get("name") or ""), []).append(row)

    def _do_rel(rows):
        nonlocal created_links
        keys = {(r.get("user_id"), r.get("class_id")) for r in rows}
        for item in memberships or []:
            user_id = str(item.get("user_id") or "").strip()
            class_name = str(item.get("class_name") or "").strip()
            if not user_id or not class_name:
                continue
            for cls in by_name.get(class_name) or []:
                key = (user_id, cls.get("class_id"))
                if key in keys:
                    continue
                rows.append(
                    {
                        "user_id": user_id,
                        "class_id": cls.get("class_id"),
                        "user_name": str(item.get("user_name") or "").strip(),
                        "joined_at": now_str(),
                    }
                )
                keys.add(key)
                created_links += 1
        return None

    mutate("user_class_rel", _do_rel)
    return {
        "classes_created": created_classes,
        "classes_existing": builtin,
        "links_created": created_links,
        "total_classes": len(read_table("classes")),
        "total_links": len(read_table("user_class_rel")),
    }
