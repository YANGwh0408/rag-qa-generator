"""LLM backends: Google Gemini official, LLM Center (OpenAI / Anthropic / Google native)."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import google.generativeai as genai
import httpx

from services.llm_center_urls import (
    anthropic_messages_url,
    google_generate_content_url,
    openai_chat_completions_url,
)


class InvalidApiKeyError(ValueError):
    """Provider rejected the API key (wrong key, revoked, or wrong product)."""


class GatewayHttpError(RuntimeError):
    """LLM gateway returned HTTP 4xx/5xx (wrong URL, model, or server fault)."""


def _format_http_error(r: httpx.Response, url: str) -> str:
    raw = (r.text or "").strip()
    snippet = raw[:900]
    if len(raw) > 900:
        snippet += "…"
    try:
        j = r.json()
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code")
                if msg:
                    return f"HTTP {r.status_code}：{msg}"
            if isinstance(err, str):
                return f"HTTP {r.status_code}：{err}"
            msg = j.get("message")
            if isinstance(msg, str) and msg:
                return f"HTTP {r.status_code}：{msg}"
    except Exception:
        pass
    if snippet:
        return f"HTTP {r.status_code}：{snippet}"
    return f"HTTP {r.status_code}（无响应体） | URL: {url}"


def _openai_auth_headers(api_key: str, compat_auth: str) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = (compat_auth or "bearer").strip().lower()
    if auth == "api_key":
        headers["api-key"] = api_key
    elif auth == "x_api_key":
        headers["X-API-Key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _gateway_post(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = 120.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, headers=headers, json=body)
        if r.status_code in (401, 403):
            raise InvalidApiKeyError(
                "网关返回 401/403，请检查 API Key 是否正确、是否过期，以及 Base URL / 协议是否与文档一致。"
            )
        if not r.is_success:
            detail = _format_http_error(r, url)
            raise GatewayHttpError(
                f"{detail}\n\n"
                "排查：① Base URL 是否为文档中的 https://…/llm；"
                "②「LLM Center 协议」是否与所用模型一致（OpenAI / Claude / Gemini）；"
                "③ 模型 ID 是否从平台复制；④ 仍失败请将响应体发给网关管理员。"
            )
        return r.json()


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
    data = json.loads(raw)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise ValueError("模型返回不是 JSON 数组")
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        q = item.get("question") or item.get("q")
        a = item.get("answer") or item.get("a")
        if isinstance(q, str) and isinstance(a, str) and q.strip() and a.strip():
            out.append({"question": q.strip(), "answer": a.strip()})
    return out


def _is_gemini_key_rejected(exc: BaseException) -> bool:
    msg = str(exc)
    if "API_KEY_INVALID" in msg:
        return True
    if "API key not valid" in msg and "400" in msg:
        return True
    if "PERMISSION_DENIED" in msg and "API key" in msg:
        return True
    return False


def call_gemini(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float,
) -> str:
    genai.configure(api_key=api_key)
    m = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_prompt if system_prompt else None,
    )
    try:
        resp = m.generate_content(
            user_content,
            generation_config={"temperature": temperature},
        )
    except Exception as e:
        if _is_gemini_key_rejected(e):
            raise InvalidApiKeyError(
                "Gemini API Key 无效或被拒绝。请确认："
                "① 使用 Google AI Studio（aistudio.google.com）里「Get API key」生成的密钥，"
                "不要用 Vertex / GCP 控制台里其它类型的密钥混用；"
                "② 密钥完整复制、无多余空格或换行；"
                "③ 在 Google Cloud 中为该项目已启用 Generative Language API；"
                "④ 密钥未过期、未被删除或限制。"
            ) from e
        raise
    if not resp.text:
        raise RuntimeError("Gemini 未返回文本")
    return resp.text


def call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int = 4096,
    compat_auth: str = "bearer",
    timeout: float = 120.0,
) -> str:
    url = openai_chat_completions_url(base_url)
    headers = _openai_auth_headers(api_key, compat_auth)
    messages: list[dict[str, str]] = []
    sys_t = (system_prompt or "").strip()
    if sys_t:
        messages.append({"role": "system", "content": sys_t})
    messages.append({"role": "user", "content": user_content})
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max(256, min(int(max_tokens), 128_000)),
        "stream": False,
        "messages": messages,
    }
    data = _gateway_post(url=url, headers=headers, body=body, timeout=timeout)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI 协议响应未包含 choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not content:
        raise RuntimeError("OpenAI 协议响应未包含 message.content")
    return content if isinstance(content, str) else str(content)


def call_llm_center_anthropic(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int = 4096,
    timeout: float = 120.0,
) -> str:
    url = anthropic_messages_url(base_url)
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": api_key,
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max(256, min(int(max_tokens), 128_000)),
        "messages": [{"role": "user", "content": user_content}],
        "temperature": temperature,
    }
    if system_prompt:
        body["system"] = system_prompt
    data = _gateway_post(url=url, headers=headers, body=body, timeout=timeout)
    blocks = data.get("content") or []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Anthropic 协议响应未解析到文本")
    return text


def call_llm_center_google(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int = 4096,
    timeout: float = 120.0,
) -> str:
    url = google_generate_content_url(base_url, model)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max(256, min(int(max_tokens), 8192)),
        },
    }
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    data = _gateway_post(url=url, headers=headers, body=body, timeout=timeout)
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("Gemini 网关响应未包含 candidates")
    parts = (cands[0].get("content") or {}).get("parts") or []
    texts: list[str] = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            texts.append(p["text"])
    text = "".join(texts).strip()
    if not text:
        raise RuntimeError("Gemini 网关响应未解析到文本")
    return text


def _generate_qa_json_once(
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int = 4096,
    compat_auth: str = "bearer",
    center_protocol: str = "openai",
) -> list[dict[str, str]]:
    if provider == "gemini":
        raw = call_gemini(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_content=user_prompt,
            temperature=temperature,
        )
    elif provider == "openai_compatible":
        if not base_url:
            raise ValueError("LLM Center / 兼容模式需要填写 Base URL")
        proto = (center_protocol or "openai").strip().lower()
        if proto == "anthropic":
            raw = call_llm_center_anthropic(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_content=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        elif proto == "google":
            raw = call_llm_center_google(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_content=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            raw = call_openai_compatible(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_content=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                compat_auth=compat_auth,
            )
    else:
        raise ValueError(f"未知 provider: {provider}")
    return _parse_json_array(raw)


def generate_qa_json(
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int = 4096,
    compat_auth: str = "bearer",
    center_protocol: str = "openai",
    max_retries: int = 3,
) -> list[dict[str, str]]:
    """带退避重试；InvalidApiKeyError 不重试直接抛出。"""
    attempts = max(0, int(max_retries)) + 1
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return _generate_qa_json_once(
                provider=provider,
                api_key=api_key,
                model=model,
                base_url=base_url,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                compat_auth=compat_auth,
                center_protocol=center_protocol,
            )
        except InvalidApiKeyError:
            raise
        except Exception as e:
            last_exc = e
            if i + 1 >= attempts:
                break
            delay = min(2**i, 12.0)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
