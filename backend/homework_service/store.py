# -*- coding: utf-8 -*-
"""作业体系的后端存储层（本地 JSON，可整体替换为 Supabase）。

为什么不是直接写 Supabase：
    秒哒表代理只放行已建好的白名单表（users / records / quizzes / retry /
    practices / pkMatches），GET/PUT `homeworks` 一律返回
    {"error":"unknown table"}，也无法通过接口建表。所以作业数据先落在后端本地，
    这里把「读表 / 写表」收敛成两个函数：Supabase 建好表之后，只要把
    _load / _save 换成整表 HTTP 读写，上层业务零改动。

落盘位置：
    后端/data/homeworks.json          作业主表
    后端/data/homework_submissions.json  提交记录表
    后端/data/point_awards.json      积分入账流水（去重依据）
    后端/uploads/                    作业附件
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent

# 部署适配（只影响路径来源，不影响任何业务逻辑）：
#   默认仍是「后端/data」「后端/uploads」，与本地开发完全一致；
#   生产环境如把数据放在独立数据盘（例如 /data/bugmind），只要在 .env 里设置
#       BUGMIND_DATA_DIR=/data/bugmind/data
#       BUGMIND_UPLOAD_DIR=/data/bugmind/uploads
#   即可整体搬家。systemd 单元的 ReadWritePaths 需同步放行这两个目录。
DATA_DIR = Path(os.environ.get("BUGMIND_DATA_DIR") or (BASE_DIR / "data")).resolve()
UPLOAD_DIR = Path(os.environ.get("BUGMIND_UPLOAD_DIR") or (BASE_DIR / "uploads")).resolve()

_LOCK = threading.RLock()

# 表名 → 默认值（空表）
# classes / user_class_rel 由 class_service 使用，共用同一套原子读写
_TABLES = ("homeworks", "homework_submissions", "point_awards", "classes", "user_class_rel", "friend_requests", "friendships", "records", "users")


def ensure_dirs() -> None:
    """确保数据目录与附件目录存在（启动时调用一次）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _path(table: str) -> Path:
    if table not in _TABLES:
        raise ValueError(f"未知数据表: {table}")
    return DATA_DIR / f"{table}.json"


def _load(table: str) -> List[Dict[str, Any]]:
    path = _path(table)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (json.JSONDecodeError, OSError):
        data = None
    if isinstance(data, list):
        return data
    # 文件损坏或内容不是表数组（例如意外写成了对象）：先备份原文件再返回空表，
    # 避免后续 mutate 用空表覆盖原文件造成静默数据丢失
    try:
        path.replace(path.with_suffix(f".broken.{int(time.time())}.json"))
    except OSError:
        pass
    return []


def _save(table: str, rows: List[Dict[str, Any]]) -> None:
    ensure_dirs()
    path = _path(table)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(rows, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    try:
        tmp.replace(path)  # 原子替换，避免写一半断电导致整表损坏
    except OSError:
        # Windows 下目标文件可能被杀毒/索引短暂占用：稍候重试一次；
        # 仍失败则原样抛出（绝不静默吞掉，避免内存中的修改悄悄丢失）
        time.sleep(0.05)
        tmp.replace(path)


def read_table(table: str) -> List[Dict[str, Any]]:
    with _LOCK:
        return _load(table)


def write_table(table: str, rows: List[Dict[str, Any]]) -> None:
    with _LOCK:
        _save(table, rows)


def mutate(table: str, fn: Callable[[List[Dict[str, Any]]], Any]) -> Any:
    """在锁内完成「读 → 改 → 写」，fn 的返回值原样透出。"""
    with _LOCK:
        rows = _load(table)
        result = fn(rows)
        _save(table, rows)
        return result


def new_id(prefix: str) -> str:
    """生成业务 ID：前缀 + 毫秒时间戳 + 4 位随机串。"""
    return f"{prefix}{int(time.time() * 1000)}{uuid.uuid4().hex[:4]}"


def now_str() -> str:
    """统一时间格式：YYYY-MM-DD HH:MM:SS（前端可直接展示/排序）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def storage_info() -> Dict[str, Any]:
    """给 /health 用的存储自查信息。"""
    return {
        "backend": "local-json",
        "data_dir": str(DATA_DIR),
        "upload_dir": str(UPLOAD_DIR),
        "tables": {t: len(_load(t)) for t in _TABLES},
    }
