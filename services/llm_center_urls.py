"""Resolve LLM Center (ModelBest) and generic gateway URLs per company docs."""

from __future__ import annotations

from urllib.parse import quote


def _norm(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def openai_chat_completions_url(base_url: str) -> str:
    """
    ModelBest: POST .../llm/openai/v1/chat/completions
    SDK base_url: https://.../llm/openai/v1
    """
    u = _norm(base_url)
    low = u.lower()
    if low.endswith("/chat/completions"):
        return u
    if low.endswith("/openai/v1"):
        return f"{u}/chat/completions"
    if low.endswith("/llm"):
        return f"{u}/openai/v1/chat/completions"
    if low.endswith("/v1"):
        return f"{u}/chat/completions"
    return f"{u}/v1/chat/completions"


def anthropic_messages_url(base_url: str) -> str:
    """SDK base: https://.../llm/anthropic → POST .../v1/messages"""
    u = _norm(base_url)
    low = u.lower()
    if low.endswith("/v1/messages"):
        return u
    if low.endswith("/anthropic/v1"):
        return f"{u}/messages"
    if low.endswith("/anthropic"):
        return f"{u}/v1/messages"
    if low.endswith("/llm"):
        return f"{u}/anthropic/v1/messages"
    return f"{u}/anthropic/v1/messages"


def google_generate_content_url(base_url: str, model: str) -> str:
    """POST .../llm/google/v1beta/models/{model}:generateContent"""
    u = _norm(base_url)
    low = u.lower()
    m = quote(model.strip(), safe="")
    if ":generatecontent" in low and "/models/" in low:
        return u
    if low.endswith("/llm"):
        return f"{u}/google/v1beta/models/{m}:generateContent"
    if low.endswith("/google"):
        return f"{u}/v1beta/models/{m}:generateContent"
    return f"{u}/google/v1beta/models/{m}:generateContent"
