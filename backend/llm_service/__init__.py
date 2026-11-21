# -*- coding: utf-8 -*-
"""BugMind 大模型诊断服务包。

本包是「错题诊断核心引擎」的独立实现，与 FastAPI 路由、前端契约完全解耦：
    config.py           —— .env 读取与运行参数（密钥只从环境变量来）
    deepseek_client.py  —— DeepSeek V4.1 Flash 的 HTTPS 调用（唯一的网络出口）
    prompts.py          —— 系统提示词（三级引导）+ 用户消息组装
    parser.py           —— 大模型返回结果校验、结构化与兜底

替换模型时只需改 .env 或替换 deepseek_client.chat 的实现，业务代码零改动。
"""

from .config import get_settings, Settings
from .deepseek_client import (
    LLMServiceError,
    LLMTimeoutError,
    LLMUpstreamError,
    LLMConfigError,
    FRIENDLY_ERROR_MESSAGE,
    active_model,
    chat,
    resolve_model,
)
from .parser import parse_levels, fallback_levels, parse_reason_fields, LEVEL_KEYS
from .prompts import SYSTEM_PROMPT, build_user_prompt

__all__ = [
    "get_settings",
    "Settings",
    "LLMServiceError",
    "LLMTimeoutError",
    "LLMUpstreamError",
    "LLMConfigError",
    "FRIENDLY_ERROR_MESSAGE",
    "active_model",
    "chat",
    "resolve_model",
    "parse_levels",
    "fallback_levels",
    "parse_reason_fields",
    "LEVEL_KEYS",
    "SYSTEM_PROMPT",
    "build_user_prompt",
]
