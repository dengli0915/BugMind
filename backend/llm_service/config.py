# -*- coding: utf-8 -*-
"""运行配置：统一从 .env 读取，密钥绝不硬编码、绝不写进日志。

.env 搜索顺序（先找到的先加载，已存在的环境变量优先）：
    1. 项目根目录/.env        （前端、后端两个文件夹的同级目录，推荐放这里）
    2. 后端/.env               （与 main.py 同级，兼容旧部署）
    3. 当前工作目录/.env
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 后端代码目录：.../后端
BASE_DIR: Path = Path(__file__).resolve().parent.parent
# 项目根目录：.../Desktop（前端、后端 在该目录下并列）
ROOT_DIR: Path = BASE_DIR.parent

_ENV_SEARCH_PATHS: Tuple[Path, ...] = (
    ROOT_DIR / ".env",
    BASE_DIR / ".env",
    Path.cwd() / ".env",
)


def load_env() -> Tuple[Path, ...]:
    """加载 .env，返回实际加载到的文件路径（用于启动日志排查）。"""
    loaded = []
    seen = set()
    for path in _ENV_SEARCH_PATHS:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        load_dotenv(path, override=False)
        loaded.append(path)
    return tuple(loaded)


LOADED_ENV_FILES: Tuple[Path, ...] = load_env()


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name, "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# ---------- 启动提示（只打日志提醒部署者，不改变任何读取逻辑） ----------
if not _env_str("DEEPSEEK_API_KEY", ""):
    if LOADED_ENV_FILES:
        logger.warning(
            "已加载 .env（%s），但缺少 DEEPSEEK_API_KEY：大模型诊断将返回降级提示，请填写后重启服务。",
            "、".join(str(p) for p in LOADED_ENV_FILES),
        )
    else:
        logger.warning(
            "未找到 .env 文件且未配置 DEEPSEEK_API_KEY 环境变量（已搜索：%s）："
            "大模型诊断将返回降级提示，请创建 .env 后重启服务。",
            "、".join(str(p) for p in _ENV_SEARCH_PATHS),
        )


@dataclass(frozen=True)
class Settings:
    """大模型诊断服务的全部可调参数。"""

    # ===== DeepSeek 接入 =====
    api_key: str = ""
    model: str = "deepseek-v4.1-flash"
    api_url: str = "https://api.deepseek.com/v1/chat/completions"
    timeout: float = 60.0
    max_tokens: int = 4096
    temperature: float = 0.3
    json_mode: bool = True

    # ===== 入参限制 =====
    max_code_length: int = 8000
    max_problem_length: int = 2000
    max_run_result_length: int = 2000

    # ===== 服务 =====
    port: int = 8000

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def masked_key(self) -> str:
        """脱敏后的 Key，仅供日志/健康检查展示。"""
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "****"
        return f"{self.api_key[:4]}****{self.api_key[-4:]}"

    @property
    def env_files(self) -> Tuple[str, ...]:
        return tuple(str(p) for p in LOADED_ENV_FILES)


def get_settings() -> Settings:
    """读取一次环境变量并缓存为不可变配置对象。"""
    return Settings(
        api_key=_env_str("DEEPSEEK_API_KEY", ""),
        model=_env_str("DEEPSEEK_MODEL", "deepseek-v4.1-flash"),
        api_url=_env_str("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"),
        timeout=_env_float("API_TIMEOUT", 60.0),
        max_tokens=_env_int("LLM_MAX_TOKENS", 4096),
        temperature=_env_float("LLM_TEMPERATURE", 0.3),
        json_mode=_env_bool("LLM_JSON_MODE", True),
        max_code_length=_env_int("MAX_CODE_LENGTH", 8000),
        max_problem_length=_env_int("MAX_PROBLEM_LENGTH", 2000),
        max_run_result_length=_env_int("MAX_ERROR_LENGTH", 2000),
        port=_env_int("PORT", 8000),
    )
