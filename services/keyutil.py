"""Normalize secrets pasted from chat, email, or docs."""


def normalize_secret(key: str) -> str:
    if not key:
        return ""
    s = key.replace("\ufeff", "").replace("\u200b", "").replace("\u00a0", " ")
    s = "".join(s.splitlines()).strip()
    return s
