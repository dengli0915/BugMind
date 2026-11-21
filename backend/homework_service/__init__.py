# -*- coding: utf-8 -*-
"""作业联动体系 + 统一积分 + 附件上传（与诊断链路完全解耦）。

    store.py   —— 本地 JSON 存储（原子写 + 锁），可整体替换为 Supabase
    points.py  —— 统一积分规则与入账流水（去重）
    router.py  —— FastAPI 路由（/api/homeworks、/api/upload、/api/points/*）
"""

from .store import DATA_DIR, UPLOAD_DIR, ensure_dirs, storage_info
from .router import router
from .points import (
    RANK_FULL_NAMES,
    RANK_KING_NAME,
    RANK_SUBS,
    RANK_WANGZHE,
    rank_compare,
    rank_of,
    rank_sort_key,
    sort_by_rank,
)

__all__ = [
    "router",
    "ensure_dirs",
    "storage_info",
    "DATA_DIR",
    "UPLOAD_DIR",
    "RANK_FULL_NAMES",
    "RANK_KING_NAME",
    "RANK_SUBS",
    "RANK_WANGZHE",
    "rank_of",
    "rank_sort_key",
    "rank_compare",
    "sort_by_rank",
]
