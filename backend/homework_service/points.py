# -*- coding: utf-8 -*-
"""统一积分规则与入账流水。

规则（与前端 awardPoints() 完全一致，前端离线时用同一套公式兜底）：

    错题重练 / 学习路线：
        基础分  简单 5 / 中等 8 / 困难 12
        段位系数 青铜1.0 白银0.9 黄金0.8 铂金0.7 钻石0.6 大师0.5 王者0.4
        实得 = round(基础分 × 系数)，最少 1 分
    作业评分：
        0-100 分按 5 分作业分 = 1 冲分积分折算，范围 0-20，四舍五入

去重：同一学生 + 同一 key 只入账一次（key 由调用方给出，例如
"rt:<题目>" / "road:<知识点>:v2" / "hw:<作业ID>"）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import store

DIFFICULTY_BASE = {"simple": 5, "mid": 8, "hard": 12}
RANK_COEF = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4]
RANK_NAMES = ["青铜", "白银", "黄金", "铂金", "钻石", "超凡大师", "王者"]

# 段位体系（与前端 rankParts() 一一对应）：
#   坚毅青铜 → 凌霜白银 → 鎏金黄金 → 曜石铂金 → 星辉钻石 → 超凡大师 → 最强王者
#   前 6 个大段位 × 5 个小段位 × 100 分 = 3000 分封顶，之后进入「最强王者」，积分无上限
RANK_FULL_NAMES = ["坚毅青铜", "凌霜白银", "鎏金黄金", "曜石铂金", "星辉钻石", "超凡大师"]
RANK_KING_NAME = "最强王者"
RANK_SUBS = ["Ⅴ", "Ⅳ", "Ⅲ", "Ⅱ", "Ⅰ"]  # 从低到高
RANK_SUB_SPAN = 100
RANK_BIG_SPAN = 500
RANK_WANGZHE = 3000
_DIFF_ALIAS = {
    "simple": "simple", "easy": "simple", "简单": "simple", "easy_level": "simple",
    "mid": "mid", "medium": "mid", "中等": "mid", "middle": "mid",
    "hard": "hard", "困难": "hard", "difficult": "hard",
}
SCENE_LABEL = {
    "retry": "错题重练",
    "road": "学习路线",
    "homework": "作业评分",
    "variant": "冲分变式",
    "late": "作业逾期扣分",
}


def normalize_difficulty(value: Optional[str]) -> str:
    return _DIFF_ALIAS.get(str(value or "").strip().lower(), "mid")


def _round_half_up(value: float) -> int:
    """四舍五入（Python 内置 round 是银行家舍入，这里要和前端 Math.round 对齐）。"""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def rank_of(score: Any) -> Dict[str, Any]:
    """总积分 → 段位（大段位 / 小段位 / 小段积分 / 文案）。

    与前端 rankParts() 完全一致的换算，供后端接口回显与上报使用。
    """
    try:
        total = int(round(float(score)))
    except (TypeError, ValueError):
        total = 0
    total = max(0, total)
    if total >= RANK_WANGZHE:
        return {
            "king": True,
            "index": 6,
            "name": RANK_KING_NAME,
            "sub": "",
            "sub_index": -1,
            "sub_points": total,
            "total": total,
            "label": "%s %d分" % (RANK_KING_NAME, total),
        }
    big = total // RANK_BIG_SPAN          # 0..5
    rest = total - big * RANK_BIG_SPAN    # 0..499
    sub_idx = rest // RANK_SUB_SPAN       # 0..4 → Ⅴ..Ⅰ
    sub_points = rest - sub_idx * RANK_SUB_SPAN  # 0..99
    name = RANK_FULL_NAMES[big]
    return {
        "king": False,
        "index": big,
        "name": name,
        "sub": RANK_SUBS[sub_idx],
        "sub_index": sub_idx,
        "sub_points": sub_points,
        "total": total,
        "label": "%s%s %d分" % (name, RANK_SUBS[sub_idx], sub_points),
    }


def rank_index_of(score: Any) -> int:
    return int(rank_of(score)["index"])


def rank_sort_key(score: Any) -> Tuple[int, int, int, int]:
    """排行榜排序键（升序 tuple，排序时用 reverse=True 即为榜单顺序）。

    规则与产品规格一致：
      - 最强王者统一排在最前（big=99），按其累计总积分降序；
      - 其余先比大段位（0..5 越高越强），再比小段位（Ⅴ..Ⅰ → 0..4），最后比小段积分（0..99）。
    这样「跨段位累计积分」不会造成错乱：499 分（青铜Ⅰ 99）永远排在 500 分（白银Ⅴ 0）之后。
    """
    parts = rank_of(score)
    total = int(parts["total"])
    if parts["king"]:
        return (99, 0, 0, total)
    return (int(parts["index"]), int(parts["sub_index"]), int(parts["sub_points"]), 0)


def rank_compare(score_a: Any, score_b: Any) -> int:
    """比较两个总积分的段位高低：返回 -1（a 更强）/ 0（同级同分）/ 1（b 更强）。"""
    ka, kb = rank_sort_key(score_a), rank_sort_key(score_b)
    if ka > kb:
        return -1
    if ka < kb:
        return 1
    return 0


def sort_by_rank(items: Any, score_of: Any) -> list:
    """按段位规则给学生列表排序（榜单通用），score_of(item) 取总积分。"""
    rows = list(items or [])
    rows.sort(key=lambda it: rank_sort_key(score_of(it)), reverse=True)
    return rows


def difficulty_points(difficulty: Optional[str], rank_index: int = 0) -> Dict[str, Any]:
    """按难度 + 段位系数算分，返回 {points, base, coef, rank_index}。"""
    diff = normalize_difficulty(difficulty)
    idx = max(0, min(int(rank_index or 0), len(RANK_COEF) - 1))
    base = DIFFICULTY_BASE[diff]
    coef = RANK_COEF[idx]
    points = max(1, _round_half_up(base * coef))
    return {"points": points, "base": base, "coef": coef, "rank_index": idx, "difficulty": diff}


def homework_points(score: Any) -> int:
    """作业分 0-100 → 冲分积分 0-20（5 分作业分 = 1 冲分积分）。"""
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(100.0, value))
    return max(0, min(20, _round_half_up(value / 5.0)))


def find_award(student_id: str, key: str) -> Optional[Dict[str, Any]]:
    for row in store.read_table("point_awards"):
        if str(row.get("student_id")) == str(student_id) and str(row.get("key")) == str(key):
            return row
    return None


def award(
    *,
    student_id: str,
    scene: str,
    key: str,
    points: int,
    base: int = 0,
    coef: float = 1.0,
    detail: str = "",
) -> Dict[str, Any]:
    """记一笔积分流水；相同 (student_id, key) 只入账一次。

    查重与写入放在同一个 store.mutate（同一把锁）里完成，避免并发场景下
    两个请求同时通过查重、各写一行导致重复入账。返回结构与旧实现完全一致。
    """
    key = str(key or "").strip()
    if not key:
        return {"deduped": False, "points": points, "awarded_at": store.now_str()}

    def _do(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        for row in rows:
            if str(row.get("student_id")) == str(student_id) and str(row.get("key")) == str(key):
                return {
                    "deduped": True,
                    "points": 0,
                    "already_points": row.get("points", 0),
                    "awarded_at": row.get("awarded_at", ""),
                }
        row = {
            "id": store.new_id("pa"),
            "student_id": str(student_id),
            "scene": scene,
            "scene_label": SCENE_LABEL.get(scene, scene),
            "key": key,
            "points": int(points),
            "base": int(base),
            "coef": coef,
            "detail": detail,
            "awarded_at": store.now_str(),
        }
        rows.append(row)
        return {"deduped": False, "points": int(points), "awarded_at": row["awarded_at"]}

    return store.mutate("point_awards", _do)


def ledger_for(student_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    rows = [r for r in store.read_table("point_awards") if str(r.get("student_id")) == str(student_id)]
    rows.sort(key=lambda r: str(r.get("awarded_at", "")), reverse=True)
    return rows[:limit]
