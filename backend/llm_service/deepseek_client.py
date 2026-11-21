# -*- coding: utf-8 -*-
"""DeepSeek V4.1 Flash 调用模块（唯一的网络出口）。

职责边界：
    - 只做「组装请求 → 发 HTTPS → 取回文本」；
    - 不做任何业务判断、不拼提示词、不解析业务结构（交给 prompts.py / parser.py）；
    - 密钥只在请求头里出现，日志与返回值一律脱敏；
    - 所有异常统一收敛为 LLMServiceError 子类，由路由层翻译成友好提示。

替换模型：改 .env 的 DEEPSEEK_MODEL / DEEPSEEK_API_URL 即可，或在此文件替换 chat()。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

# 对外统一友好提示（前端直接展示这句话，不暴露上游错误细节）
FRIENDLY_ERROR_MESSAGE = "诊断服务暂不可用，请稍后重试"

# 可重试的临时性错误码
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF = 0.8  # 秒，第二次尝试前等待


# 运行期实际使用的模型名：若配置的模型不被上游支持，这里会被自动纠正一次，
# 后续请求直接用纠正后的模型，避免每次诊断都先浪费一次失败请求。
_ACTIVE_MODEL: Optional[str] = None

# 上游 400 报错里会列出它支持的模型名，例如：
# "The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed xxx."
_SUPPORTED_MODEL_RE = re.compile(r"supported API model names are ([^.(]+)", re.IGNORECASE)


def active_model() -> str:
    """当前生效的模型名（可能是被自动纠正过的）。"""
    return _ACTIVE_MODEL or get_settings().model


def _parse_supported_models(body: str) -> List[str]:
    match = _SUPPORTED_MODEL_RE.search(body or "")
    if not match:
        return []
    return [name.strip() for name in re.split(r"[,，/]", match.group(1)) if name.strip()]


def _pick_model(supported: Sequence[str], configured: str) -> Optional[str]:
    """在上游支持的模型里挑一个最接近配置值的：完全同名 > 同族（flash/pro）> 第一个。"""
    names = [n for n in supported if n]
    if not names:
        return None
    if configured in names:
        return configured
    lowered = configured.lower()
    for family in ("flash", "pro", "chat", "reasoner"):
        if family in lowered:
            for name in names:
                if family in name.lower():
                    return name
    return names[0]


def _models_url(api_url: str) -> str:
    """由 .../chat/completions 推出 .../models。"""
    if api_url.endswith("/chat/completions"):
        return api_url[: -len("/chat/completions")] + "/models"
    return api_url.rstrip("/") + "/models"


async def resolve_model() -> str:
    """启动时校正模型名：查询上游 /models，若配置的模型不存在则自动挑一个可用同族模型。

    返回实际生效的模型名；任何失败都静默回退到配置值（不阻塞启动）。
    """
    global _ACTIVE_MODEL
    cfg = get_settings()
    if not cfg.has_key:
        return cfg.model
    try:
        timeout = httpx.Timeout(min(10.0, cfg.timeout), connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_models_url(cfg.api_url), headers=_build_headers(cfg.api_key))
        if response.status_code != 200:
            logger.warning("模型列表查询失败(%s)，沿用配置值 %s", response.status_code, cfg.model)
            return cfg.model
        names = [str(item.get("id")).strip() for item in (response.json().get("data") or []) if item.get("id")]
        picked = _pick_model(names, cfg.model)
        if picked and picked != cfg.model:
            logger.warning(
                "配置的模型 %s 上游不支持，自动改用 %s（可选: %s）",
                cfg.model,
                picked,
                ", ".join(names),
            )
            _ACTIVE_MODEL = picked
        return picked or cfg.model
    except Exception as exc:  # 网络问题等：交给真正的调用去暴露
        logger.warning("启动时未能校正模型名(%s: %s)，沿用配置值 %s", type(exc).__name__, exc, cfg.model)
        return cfg.model


class LLMServiceError(Exception):
    """大模型服务异常基类。friendly 是给前端看的文案，str(self) 是给日志看的细节。"""

    def __init__(
        self,
        detail: str,
        *,
        friendly: str = FRIENDLY_ERROR_MESSAGE,
        status_code: int = 503,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.friendly = friendly
        self.status_code = status_code


class LLMConfigError(LLMServiceError):
    """未配置密钥等本地配置问题（部署阶段错误，不该让终端用户看到细节）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=503)


class LLMTimeoutError(LLMServiceError):
    """请求上游超时。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=504)


class LLMUpstreamError(LLMServiceError):
    """上游返回错误码或返回体不可用。"""

    def __init__(self, detail: str, status_code: int = 502) -> None:
        super().__init__(detail, status_code=status_code)


def _build_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _build_payload(
    messages: Sequence[Mapping[str, str]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [dict(m) for m in messages],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        # 让上游保证返回合法 JSON；若上游不支持，_post 里会自动降级重试
        payload["response_format"] = {"type": "json_object"}
    return payload


def _extract_content(payload: Any) -> str:
    """从 Chat Completions 响应体里取正文。"""
    # 防护：上游异常时可能返回非对象/非列表结构，逐层校验避免 AttributeError 裸奔
    if not isinstance(payload, Mapping):
        raise LLMUpstreamError("上游返回体不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise LLMUpstreamError("上游返回体缺少 choices 字段")
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        # 少数模型把正文放在 reasoning_content，做一次兜底
        alt = message.get("reasoning_content")
        if isinstance(alt, str) and alt.strip():
            return alt.strip()
        raise LLMUpstreamError("上游返回内容为空")
    return content.strip()


async def _post_once(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
) -> httpx.Response:
    return await client.post(url, headers=headers, json=payload)


async def chat(
    messages: Sequence[Mapping[str, str]],
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    json_mode: Optional[bool] = None,
) -> str:
    """调用 DeepSeek，返回模型正文（已 strip）。

    参数:
        messages:   [{"role": "system"/"user", "content": "..."}]
        temperature/max_tokens/json_mode: 不传则取 .env 配置
    返回:
        模型返回的原始文本（可能是 JSON，也可能是带 markdown 的文本，交给 parser 处理）
    异常:
        LLMConfigError / LLMTimeoutError / LLMUpstreamError
    """
    cfg = get_settings()

    if not cfg.has_key:
        raise LLMConfigError(
            "未配置 DEEPSEEK_API_KEY：请在项目根目录 .env（或 后端/.env）中填写后重启服务"
        )

    use_json = cfg.json_mode if json_mode is None else bool(json_mode)
    timeout = httpx.Timeout(cfg.timeout, connect=min(10.0, cfg.timeout))
    headers = _build_headers(cfg.api_key)

    global _ACTIVE_MODEL

    last_detail = "未知错误"
    model = active_model()
    transient_attempts = 0
    model_fixed = False

    while True:
        payload = _build_payload(
            messages,
            model=model,
            temperature=cfg.temperature if temperature is None else temperature,
            max_tokens=cfg.max_tokens if max_tokens is None else max_tokens,
            json_mode=use_json,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await _post_once(client, cfg.api_url, headers, payload)
        except httpx.TimeoutException:
            transient_attempts += 1
            last_detail = f"上游请求超时（{cfg.timeout}s，第 {transient_attempts} 次）"
            logger.warning(last_detail)
            if transient_attempts < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            raise LLMTimeoutError(last_detail) from None
        except httpx.HTTPError as exc:
            transient_attempts += 1
            last_detail = f"上游连接失败: {type(exc).__name__}: {exc}"
            logger.warning(last_detail)
            if transient_attempts < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            raise LLMUpstreamError(last_detail) from None

        if response.status_code == 400:
            body = response.text or ""
            # 上游拒绝 response_format（部分部署不支持 json 模式）→ 关掉 json 模式重试
            if use_json and ("response_format" in body or "json_object" in body):
                logger.warning("上游不支持 response_format=json_object，改用纯文本模式重试")
                use_json = False
                continue
            # 模型名不被支持 → 换成上游支持的模型重试一次（运行期自愈）
            if not model_fixed:
                supported = _parse_supported_models(body)
                picked = _pick_model(supported, model)
                if picked and picked != model:
                    logger.warning("模型 %s 不被上游支持，自动改用 %s", model, picked)
                    model = picked
                    _ACTIVE_MODEL = picked
                    model_fixed = True
                    continue
            raise LLMUpstreamError(
                f"上游返回 400: {body[:300]}",
                status_code=502,
            )

        if response.status_code in _RETRY_STATUS:
            transient_attempts += 1
            last_detail = f"上游返回 {response.status_code}: {(response.text or '')[:200]}"
            logger.warning(last_detail)
            if transient_attempts < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            raise LLMUpstreamError(last_detail, status_code=502)

        if response.status_code >= 400:
            raise LLMUpstreamError(
                f"上游返回 {response.status_code}: {(response.text or '')[:300]}",
                status_code=502,
            )

        try:
            data = response.json()
        except Exception as exc:
            # 兜底捕获：除了 ValueError，response.text 二次解码也可能抛其他异常（如 DecodingError），
            # 统一转 LLMUpstreamError；detail 只进日志，用户可见的仍是 FRIENDLY_ERROR_MESSAGE
            raise LLMUpstreamError(
                f"上游返回体无法解析为 JSON（{type(exc).__name__}）", status_code=502
            ) from None

        content = _extract_content(data)
        usage = data.get("usage") or {}
        logger.info(
            "大模型调用成功 model=%s 返回长度=%s tokens=%s/%s",
            data.get("model", model),
            len(content),
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        return content
