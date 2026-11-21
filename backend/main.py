# -*- coding: utf-8 -*-
"""BugMind 后端 —— 错题诊断核心引擎（DeepSeek V4.1 Flash）。

对外只暴露一个诊断接口：POST /api/code-diagnose
    { "code": "...", "language": "python", "problem": "题目要求（可选）",
      "run_result": "报错信息（可选）" }
    → { "success": true, "data": { "error_type", "total_levels", "levels": {...},
                                   "direct_answer": {...} } }

职责划分：
    llm_service/  独立的大模型调用模块（可整体替换，不影响本文件的路由逻辑）
    main.py       请求校验 → 调模型 → 结果校验/兜底 → 转成前端契约

设计约束：
    - 密钥只从 .env 读取，源码中没有任何硬编码；
    - 大模型异常一律翻译成「诊断服务暂不可用，请稍后重试」，不让页面崩溃；
    - 模型返回格式不合法时走兜底，仍然返回完整三级结构。
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Mapping, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from homework_service import ensure_dirs as ensure_homework_dirs
from homework_service import router as homework_router
from homework_service.ai_grading import router as ai_grading_router
from homework_service import storage_info as homework_storage_info
from class_service import router as class_router
from friend_service import router as friend_router
from record_service import router as record_router
from auth_service import router as auth_router
from llm_service import (
    FRIENDLY_ERROR_MESSAGE,
    LLMServiceError,
    active_model,
    build_user_prompt,
    chat,
    get_settings,
    resolve_model,
)
from llm_service.parser import (
    LLMParseError,
    fallback_levels,
    is_no_bug,
    parse_levels,
    parse_reason_fields,
)
from llm_service.prompts import LEVEL1_MAX_CHARS, SYSTEM_PROMPT, normalize_language
from llm_service.variant import (
    VARIANT_SYSTEM_PROMPT,
    build_variant_prompt,
    parse_variant,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("bugmind.api")

CFG = get_settings()


# ==================== 应用初始化 ====================
@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("=" * 68)
    logger.info("BugMind 诊断服务启动 | 模型: %s", CFG.model)
    logger.info("接口地址: %s", CFG.api_url)
    if CFG.env_files:
        logger.info("已加载 .env: %s", " , ".join(CFG.env_files))
    else:
        logger.warning("未找到 .env 文件，请复制 .env.example 为 .env 并填写 DEEPSEEK_API_KEY")
    if CFG.has_key:
        logger.info("DEEPSEEK_API_KEY: 已配置（%s）", CFG.masked_key)
        # 启动即校正模型名：配置的模型若不被上游支持，自动挑一个可用同族模型
        resolved = await resolve_model()
        if resolved != CFG.model:
            logger.warning("模型名已自动纠正：%s → %s（改 .env 的 DEEPSEEK_MODEL 可固定）", CFG.model, resolved)
        else:
            logger.info("模型可用性检查通过：%s", resolved)
    else:
        logger.warning("DEEPSEEK_API_KEY: 未配置 —— 诊断接口将返回降级提示")
    # 目录初始化与存储自检：失败时输出可操作的友好提示（沿用原有启动流程，不吞错误）
    try:
        ensure_homework_dirs()
        info = homework_storage_info()
    except Exception:
        logger.exception(
            "存储目录初始化/自检失败：请检查 %s 目录是否可写、data 下 JSON 是否损坏",
            os.getcwd(),
        )
        raise
    logger.info("作业体系存储: %s | 数据目录 %s | 已有 作业%d/提交%d/流水%d 条",
                info["backend"], info["data_dir"],
                info["tables"].get("homeworks", 0),
                info["tables"].get("homework_submissions", 0),
                info["tables"].get("point_awards", 0))
    logger.info("班级体系: %d 个班级 / %d 条成员关联",
                info["tables"].get("classes", 0),
                info["tables"].get("user_class_rel", 0))
    logger.info("=" * 68)
    yield


app = FastAPI(
    title="BugMind AI Diagnose API",
    description="BugMind 错题诊断服务 —— DeepSeek V4.1 Flash 三级引导式诊断",
    version="4.0.0",
    lifespan=lifespan,
)

# CORS：允许本地 HTML 直接跨域调用（双击打开 file:// 也能用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 作业体系 / 积分 / 附件上传（独立模块，与诊断链路互不影响）
app.include_router(homework_router)
# AI 作业批改（参考答案校验 / 批量批改 / 进度查询，复用同一套 llm_service 与积分 key）
app.include_router(ai_grading_router)
# 班级体系（班级码邀请制 + 一人多班）
app.include_router(class_router)
# 好友体系（申请 / 收件箱 / 同意拒绝 / 好友列表）
app.include_router(friend_router)
# 错题记录同步（学生上传 / 教师下拉；解决跨浏览器、跨设备不同步）
app.include_router(record_router)
# 账号服务（登录 / 注册 / 找回密码 / 改密码；解决换浏览器、清缓存后账号与密码丢失）
app.include_router(auth_router)

# 作业附件静态目录：http://127.0.0.1:8000/uploads/xxx.pdf
ensure_homework_dirs()
app.mount("/uploads", StaticFiles(directory=str(homework_storage_info()["upload_dir"])), name="uploads")


# 全局兜底异常处理：任何未捕获异常统一返回 500 友好文案并记录完整堆栈。
# 只兜底 Exception：HTTPException / RequestValidationError 仍走框架既有处理
# （4xx 与 422 的行为与文案保持不变），不会因本处理器被改写。
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "未处理异常 | 方法=%s | 路径=%s | 异常类型=%s",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "服务内部错误，请稍后重试。"})


# ==================== 请求模型 ====================
class DiagnoseRequest(BaseModel):
    """诊断请求。字段全部可选，在处理函数里做统一规范化与友好校验。

    兼容两套命名：
        新契约：code / language / problem
        旧契约：source_code / command / run_result
    """

    # 新契约
    code: Optional[str] = None
    language: Optional[str] = None
    problem: Optional[str] = None

    # 兼容别名
    source_code: Optional[str] = None
    content: Optional[str] = None
    lang: Optional[str] = None
    problem_statement: Optional[str] = None
    requirement: Optional[str] = None

    # 可选上下文
    run_result: Optional[str] = None
    error_message: Optional[str] = None
    command: Optional[str] = None


class VariantRequest(BaseModel):
    """冲分变式题生成请求（AI 诊断模式下由前端在诊断成功后调用）。

    只传知识点与错误类型等最小上下文，不传学生个人信息。
    """

    knowledge_point: Optional[str] = None
    error_type: Optional[str] = None
    code: Optional[str] = None
    language: Optional[str] = None


def _pick(*values: Optional[str]) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clip_text(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]


_ERROR_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z]*(?:Error|Exception|Warning|Interrupt))\b"
)


def extract_error_type(run_result: str) -> str:
    """从报错文本里取异常类型名，取不到就返回「智能诊断」。"""
    if not run_result:
        return "智能诊断"
    matches = _ERROR_NAME_RE.findall(run_result)
    if matches:
        return matches[-1]
    return "智能诊断"


# ==================== 响应组装（前端契约） ====================
def _normalize_code(text: str) -> str:
    """比较代码是否“实质相同”：忽略行尾空格、空行与首尾空白。"""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line.strip()).strip()


def _drop_unchanged_fix(levels: Mapping[str, Mapping[str, str]], original_code: str) -> int:
    """模型有时把学生原始代码原样当成某级 fix_code（等于没改）→ 清空该级代码。

    前端在 fix_code 为空时会隐藏【帮忙改错】按钮，只展示讲解，避免误导。
    """
    target = _normalize_code(original_code)
    if not target:
        return 0
    dropped = 0
    for entry in levels.values():
        fix = (entry.get("fix_code") or "").strip()
        if fix and _normalize_code(fix) == target:
            entry["fix_code"] = ""
            entry["changes"] = []
            dropped += 1
    return dropped


def _has_effective_fix(levels: Mapping[str, Mapping[str, str]], original_code: str) -> bool:
    """三级里是否存在「与学生原文实质不同」的修正代码（空行/缩进差异不算）。"""
    target = _normalize_code(original_code)
    for key in ("2", "3"):
        fix = ((levels.get(key) or {}).get("fix_code") or "").strip()
        if fix and _normalize_code(fix) != target:
            return True
    return False


def _line_hunks(original: str, fixed: str):
    """按行做最小 diff，返回 (原文行列表, 修正行列表, 非 equal 的操作块)。"""
    def _lines(text: str):
        return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    a, b = _lines(original), _lines(fixed)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return a, b, [op for op in matcher.get_opcodes() if op[0] != "equal"]


def _fixed_changed_lines(original: str, fixed: str) -> List[int]:
    """返回「修正后代码」里相对原文真正改动过的行号（1 起），供提示语定位。"""
    try:
        _a, _b, hunks = _line_hunks(original, fixed)
    except Exception:  # noqa: BLE001 - 提示语定位失败不能影响主流程
        return []
    out: List[int] = []
    for tag, _i1, _i2, j1, j2 in hunks:
        if tag in ("replace", "insert") and j2 > j1:
            out.extend(range(j1 + 1, j2 + 1))
    return out


def make_partial_fix(original: str, fixed: str, hunks_to_apply: int = 1):
    """从「完整修正」里只落地前 N 处改动，生成第 2 级要展示的「部分修正」。

    教学目的：二级只示范修好一处，剩下的错误原样保留，逼学生自己动手。
    返回 (部分修正代码, 剩余待改的行号列表)；拆不出「部分」（只有一处改动）时返回 ("", [行号])。
    """
    if not fixed or _normalize_code(fixed) == _normalize_code(original):
        return "", []

    a, b, hunks = _line_hunks(original, fixed)
    # 只认真改动：纯空行 / 纯缩进调整不算「示范修好一处」，否则前端标绿时一个绿框都出不来
    hunks = [op for op in hunks if _hunk_is_meaningful(a, b, op)]
    if not hunks:
        return "", []
    if len(hunks) <= hunks_to_apply:
        # 只有一处问题，拆不出「部分」——宁愿不给代码，也不给完整答案
        return "", [_hunk_first_line(hunks[0])]

    out = list(a)
    # 倒序落地，保证前面的下标不被后面的替换影响
    for tag, i1, i2, j1, j2 in reversed(hunks[:hunks_to_apply]):
        out[i1:i2] = b[j1:j2] if tag != "delete" else []
    remaining_lines = [_hunk_first_line(op) for op in hunks[hunks_to_apply:]]
    partial = "\n".join(out)
    if _normalize_code(partial) == _normalize_code(original):
        # 兜底：落地后与学生原文实质相同，宁可不给，也不给学生看「没改」的假示范
        return "", remaining_lines
    return partial, remaining_lines


def _hunk_is_meaningful(a, b, op) -> bool:
    """判断一个 diff 块是不是「真改动」：忽略空白后仍有内容差异才算。

    模型经常顺手整理空行/缩进，若把这种块当成第 1 处改动落地，学生看到的
    「部分修正」会和原文一模一样（等于没演示），所以这里要把纯空白块过滤掉。
    """
    _, i1, i2, j1, j2 = op
    old = [line.strip() for line in a[i1:i2] if line.strip()]
    new = [line.strip() for line in b[j1:j2] if line.strip()]
    return old != new


def _hunk_first_line(op) -> int:
    """把 diff 操作块换算成原文里的 1-based 行号（插入型改动取插入点后一行）。"""
    _, i1, _, _, _ = op
    return i1 + 1


def _lines_text(nums) -> str:
    """行号列表 → 「第 3 行」「第 3、7 行」这样的中文描述。"""
    uniq = sorted({int(n) for n in nums if n})
    if not uniq:
        return ""
    head = "、".join(str(n) for n in uniq[:3])
    if len(uniq) > 3:
        head += f" 等 {len(uniq)}"
    return f"第 {head} 行"


def _trim_level1(text: str) -> str:
    """一级讲解兜底裁剪：提示词要求 ≤30 字，模型偶尔写超，这里做最后一次收敛。"""
    text = (text or "").strip()
    if len(text) <= LEVEL1_MAX_CHARS:
        return text
    return text[: LEVEL1_MAX_CHARS - 1].rstrip("，。、；：,.;: ") + "…"


def build_frontend_payload(
    levels: Mapping[str, Mapping[str, str]],
    *,
    error_type: str,
    plain_reason: str = "",
    professional_reason: str = "",
    level2_partial: bool = False,
    level2_note: str = "",
) -> Dict[str, Any]:
    """把三级讲解数据转成前端既有的渲染契约（页面布局/样式一行不用改）。"""
    normalized: Dict[str, Dict[str, Any]] = {}
    total = 0
    for idx in range(1, 4):
        entry = levels.get(str(idx)) or {}
        hint = (entry.get("hint_text") or "").strip()
        fix = (entry.get("fix_code") or "").strip()
        if not hint and not fix:
            continue
        total += 1
        item: Dict[str, Any] = {
            "hint_text": _trim_level1(hint) if total == 1 else hint,
            "fix_code": fix,
            "changes": list(entry.get("changes") or []),  # 「改哪一行/改了什么/为什么」批注
            "is_last": False,  # 下面按实际层级数标记
        }
        # 第 2 级是「部分修正」示范：前端据此换标题并加一条「剩下的你自己改」提示
        if total == 2:
            item["partial"] = bool(level2_partial)
            item["partial_note"] = (level2_note or "").strip()
        normalized[str(total)] = item

    if total:
        normalized[str(total)]["is_last"] = True

    # 「直接给答案」沿用第三级（最小改动、全部修复）的代码，不额外向上游索要字段
    direct_answer = None
    last = normalized.get(str(total)) if total else None
    if last and last.get("fix_code"):
        direct_answer = {
            "hint_text": "已直接给出完整正确代码，跳过阶梯提示，适合快速对照答案。",
            "fix_code": last["fix_code"],
        }

    return {
        "error_type": error_type,
        # 本引擎的 3 级是「同一问题的三级引导」，不是 3 个独立 bug
        "bug_count": 1 if total else 0,
        "total_levels": total,
        "levels": normalized,
        "direct_answer": direct_answer,
        # 错误原因双栏：大白话 + 专业解释（前端双栏展示，空串自动隐藏）
        "plain_reason": (plain_reason or "").strip(),
        "professional_reason": (professional_reason or "").strip(),
    }


# ==================== 路由 ====================
@app.get("/")
async def root():
    return {
        "message": "BugMind 诊断服务",
        "version": "4.0.0",
        "model": CFG.model,
        "endpoint": "/api/code-diagnose",
    }


@app.get("/health")
async def health_check():
    """健康检查：确认服务活着、模型名与 Key 是否就绪。"""
    return {
        "status": "healthy" if CFG.has_key else "missing_api_key",
        "model": CFG.model,
        "model_active": active_model(),
        "api_url": CFG.api_url,
        "api_key": CFG.masked_key or "未配置",
        "env_files": list(CFG.env_files),
        "homework_storage": homework_storage_info(),
    }


@app.post("/api/code-diagnose")
async def diagnose_code(request: DiagnoseRequest):
    """学生提交代码 → 调用大模型 → 返回三级引导式诊断结果。"""
    started = time.perf_counter()

    # ① 入参规范化 + 友好校验
    code = _pick(request.code, request.source_code, request.content)
    if not code:
        raise HTTPException(status_code=400, detail="代码内容不能为空，请粘贴需要诊断的代码。")
    if len(code) > CFG.max_code_length:
        raise HTTPException(
            status_code=400,
            detail=f"代码长度超过限制（最多 {CFG.max_code_length} 字符）。",
        )

    language = _pick(request.language, request.lang) or "python"
    problem = _clip_text(
        _pick(request.problem, request.problem_statement, request.requirement),
        CFG.max_problem_length,
    )
    run_result = _clip_text(
        _pick(request.run_result, request.error_message),
        CFG.max_run_result_length,
    )

    if not CFG.has_key:
        logger.error("诊断被拒绝：DEEPSEEK_API_KEY 未配置（请检查 .env）")
        raise HTTPException(status_code=503, detail=FRIENDLY_ERROR_MESSAGE)

    # ② 调用大模型（唯一的网络出口在 llm_service.deepseek_client）
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(code, language, problem, run_result),
        },
    ]
    try:
        raw_text = await chat(messages)
    except LLMServiceError as exc:
        # 超时 / 上游报错 / 连接失败：统一友好提示，前端会自动降级本地诊断
        logger.error("大模型调用失败(code=%s): %s", exc.status_code, exc.detail)
        raise HTTPException(status_code=exc.status_code, detail=exc.friendly) from None
    except Exception as exc:  # 兜底：任何未预期异常都不能让接口 500 裸奔
        logger.exception("大模型调用出现未预期异常: %s", exc)
        raise HTTPException(status_code=503, detail=FRIENDLY_ERROR_MESSAGE) from None

    # ③ 结果校验：解析失败走兜底，保证前端三级结构永远完整
    degraded = False
    try:
        levels = parse_levels(raw_text)
    except LLMParseError as exc:
        degraded = True
        logger.warning("返回内容不符合契约(%s)，已启用兜底。原文前 300 字: %s", exc, raw_text[:300])
        levels = fallback_levels(raw_text)
    except Exception as exc:  # 解析器自身的意外错误同样兜底
        degraded = True
        logger.exception("解析器异常(%s)，已启用兜底", exc)
        levels = fallback_levels("")

    # 代码完全正确：三级全空 → 前端展示「无 bug」态
    reasons = parse_reason_fields(raw_text)
    level2_partial = False
    level2_note = ""
    if not is_no_bug(levels):
        error_type = extract_error_type(run_result)
        # 模型偶尔把学生原始代码原样当成某级 fix_code（等于没改）→ 清空，前端自动隐藏【帮忙改错】
        dropped = _drop_unchanged_fix(levels, code)
        if dropped:
            logger.warning("已清空 %s 个与原代码相同的 fix_code（等于未修改）", dropped)

        # 模型这一轮等于没改：带着「必须给出实际改动」的强约束重试一次，尽量一次就成功
        if not _has_effective_fix(levels, code):
            logger.warning("大模型未给出有效改动，自动重试一次")
            retry_messages = messages + [
                {"role": "assistant", "content": (raw_text or "")[:4000]},
                {
                    "role": "user",
                    "content": (
                        "你刚才返回的 fix_code 和学生原始代码完全相同，等于一个字都没改。\n"
                        "请重新输出完整 JSON：level3.fix_code 必须与原代码有实质差异（按最小改动修好全部错误），"
                        "level2.fix_code 只改第一处，两者都要给出 changes 批注数组；"
                        "绝不允许原样返回学生代码，也不允许在 explain 里谎称已经改好。"
                    ),
                },
            ]
            try:
                retry_text = await chat(retry_messages)
                retry_levels = parse_levels(retry_text)
                if not is_no_bug(retry_levels):
                    _drop_unchanged_fix(retry_levels, code)
                    if _has_effective_fix(retry_levels, code):
                        levels = retry_levels
                        raw_text = retry_text
                        reasons = parse_reason_fields(retry_text)
                        logger.info("重试成功：已拿到有效改动")
                    else:
                        logger.warning("重试仍未拿到有效改动")
            except Exception as exc:  # noqa: BLE001 - 重试失败保持第一轮结果
                logger.warning("重试失败：%s", exc)

        # 第 2 级只给「部分修正」：拿第 3 级的完整修正做行级 diff，只落地第一处改动。
        # 不依赖模型自觉，避免它一次性把全部错误改完（那等于直接给答案）。
        lv2 = dict(levels.get("2") or {})
        lv3 = levels.get("3") or {}
        full_fix = (lv3.get("fix_code") or "").strip() or (lv2.get("fix_code") or "").strip()
        if full_fix:
            partial_code, remaining_lines = make_partial_fix(code, full_fix)
            where = _lines_text(remaining_lines)
            if partial_code:
                lv2["fix_code"] = partial_code
                lv2["changes"] = []   # 部分修正由后端按原文重算，模型给的行号已经对不上
                level2_partial = True
                # 第一处改动有时会连带相邻几行（例如补 global 的同时调整默认参数），
                # 把实际改动行数写进提示，避免「说只改一处、画面里却改了 4 行」的落差。
                _a, _b, _mn = _line_hunks(code, partial_code)
                _cnt = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in _mn)
                _extra = f"（这一处连带改了 {_cnt} 行）" if _cnt > 2 else ""
                level2_note = (
                    f"第 2 级只示范修好第 1 处{_extra}，"
                    f"{('还剩' + where) if where else '其余地方'}需要你自己动手改；"
                    "改完点【下一级】对照完整修正。"
                )
            else:
                # 只有一处问题，拆不出「部分」：宁愿不给代码，也不给完整答案
                lv2["fix_code"] = ""
                lv2["changes"] = []
                if not (lv3.get("fix_code") or "").strip():
                    # 模型这一轮根本没给出任何有效修改：不装样子，直接说清楚
                    level2_note = "这一轮 AI 没有给出有效修改（代码与原文相同），请点【开始诊断】重新诊断。"
                else:
                    _chg = _fixed_changed_lines(code, full_fix)
                    _spot = _lines_text(_chg)
                    if len(_chg) > 1:
                        # 单个改动块里连带了好几行：说清楚「集中在一段」，别让学生以为是两处
                        level2_note = (
                            f"这段代码要改的地方集中在同一段（{_spot}），拆成「只改一处」就失去示范意义了，"
                            "第 2 级不提供半成品代码：照着上面的提示自己改，改完点【下一级】对照完整修正。"
                        )
                    else:
                        level2_note = (
                            f"这段代码只有 1 处问题{('（' + _spot + '）') if _spot else ''}，"
                            "第 2 级不提供完整代码：照着上面的提示自己改，改完点【下一级】核对。"
                        )
            levels["2"] = lv2
    else:
        error_type = "无错误"
        reasons = {"plain_reason": "", "professional_reason": ""}

    data = build_frontend_payload(
        levels,
        error_type=error_type,
        plain_reason=reasons.get("plain_reason", ""),
        professional_reason=reasons.get("professional_reason", ""),
        level2_partial=level2_partial,
        level2_note=level2_note,
    )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "诊断完成 | 语言=%s | 代码 %d 字 | 层级=%s | 兜底=%s | 耗时=%sms",
        normalize_language(language),
        len(code),
        data["total_levels"],
        "是" if degraded else "否",
        elapsed_ms,
    )

    return {
        "success": True,
        "source": "llm_fallback" if degraded else "llm",
        "model": active_model(),
        "language": normalize_language(language),
        "elapsed_ms": elapsed_ms,
        "degraded": degraded,
        "data": data,
    }


# ==================== 冲分变式练习（独立于诊断链路） ====================
@app.post("/api/variant")
async def generate_variant(request: VariantRequest):
    """按知识点生成 1 道同源变式题（判题规则随题下发，前端本地判题）。

    与 /api/code-diagnose 完全解耦：不共用提示词、不共用解析器，
    诊断接口的三级讲解契约不受任何影响。
    """
    started = time.perf_counter()
    kp = _clip_text(_pick(request.knowledge_point), 60)
    if not kp:
        raise HTTPException(status_code=400, detail="缺少知识点参数，无法生成变式题。")
    language = _pick(request.language) or "python"
    error_type = _clip_text(_pick(request.error_type), 60)
    code = _clip_text(request.code or "", min(CFG.max_code_length, 1500))

    if not CFG.has_key:
        logger.error("变式题生成被拒绝：DEEPSEEK_API_KEY 未配置（请检查 .env）")
        raise HTTPException(status_code=503, detail=FRIENDLY_ERROR_MESSAGE)

    messages = [
        {"role": "system", "content": VARIANT_SYSTEM_PROMPT},
        {"role": "user", "content": build_variant_prompt(kp, error_type, code, language)},
    ]
    try:
        raw_text = await chat(messages, temperature=0.5)
    except LLMServiceError as exc:
        logger.error("变式题生成失败(code=%s): %s", exc.status_code, exc.detail)
        raise HTTPException(status_code=exc.status_code, detail=exc.friendly) from None
    except Exception as exc:
        logger.exception("变式题生成出现未预期异常: %s", exc)
        raise HTTPException(status_code=503, detail=FRIENDLY_ERROR_MESSAGE) from None

    try:
        variant = parse_variant(raw_text)
    except ValueError as exc:
        logger.warning("变式题返回不符合契约(%s)，原文前 300 字: %s", exc, raw_text[:300])
        raise HTTPException(status_code=502, detail="变式题生成失败，请重试。") from None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "变式题生成完成 | 知识点=%s | 难度=%s | 规则=%d/%d | 耗时=%sms",
        kp,
        variant["difficulty"],
        len(variant["checks"]["must"]),
        len(variant["checks"]["must_not"]),
        elapsed_ms,
    )
    return {
        "success": True,
        "source": "llm",
        "model": active_model(),
        "knowledge_point": kp,
        "elapsed_ms": elapsed_ms,
        "data": variant,
    }


# ==================== 启动入口 ====================
if __name__ == "__main__":
    import uvicorn

    # PORT 环境变量非法时回退默认端口并给出提示，避免启动即崩溃
    try:
        port = int(os.getenv("PORT", "8000"))
    except ValueError:
        logger.warning("PORT 环境变量非法（%r），已回退默认端口 8000", os.getenv("PORT"))
        port = 8000
    try:
        uvicorn.run(app, host="0.0.0.0", port=port)
    except OSError as exc:
        # 端口被占用等系统级错误：给出可操作提示后按原样抛出（不改变退出行为）
        logger.error(
            "服务启动失败：端口 %d 可能已被占用（%s）。可设置 PORT 环境变量换端口后重试。",
            port,
            exc,
        )
        raise