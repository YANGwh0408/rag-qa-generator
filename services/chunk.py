"""Fixed-size text chunking with overlap, and Markdown heading-based sections."""

from __future__ import annotations

def line_atx_heading_level(line: str) -> int | None:
    """
    若本行为 ATX 标题则返回 # 的个数，否则 None。
    兼容：行首最多 3 个空格/制表、1～6 个 #；# 后可有空白再接标题，或紧接非 # 字符（如「##第二节」）。
    """
    s = line.rstrip("\r\n")
    if not s:
        return None
    i = 0
    while i < len(s) and i < 3 and s[i] in " \t":
        i += 1
    if i >= len(s) or s[i] != "#":
        return None
    h0 = i
    while i < len(s) and s[i] == "#":
        i += 1
    level = i - h0
    if not (1 <= level <= 6):
        return None
    if i < len(s) and s[i] == "#":
        return None
    rest = s[i:]
    if not rest:
        return level
    if rest[0] in " \t":
        return level
    if rest[0] != "#":
        return level
    return None


def text_has_atx_heading_at_level(text: str, level: int) -> bool:
    """正文中是否至少有一行是指定级别的 ATX 标题（用于内容嗅探）。"""
    want = max(1, min(6, int(level)))
    for line in text.splitlines():
        if line_atx_heading_level(line) == want:
            return True
    return False


def chunk_markdown_by_heading(
    text: str,
    split_level: int,
    max_chunk_chars: int,
    overlap: int,
) -> list[str]:
    """
    按指定级别的 ATX 标题（# 的个数）切成多段；单段仍超过 max_chunk_chars 时再走固定长度切分。
    若全文没有任何该级标题，则退化为整篇走 chunk_text。
    """
    text = text.strip()
    if not text:
        return []
    split_level = max(1, min(6, int(split_level)))
    if max_chunk_chars <= 0:
        return [text]

    lines = text.splitlines(keepends=True)
    parts: list[str] = []
    buf: list[str] = []
    saw_split_heading = False

    for line in lines:
        lvl = line_atx_heading_level(line)
        if lvl == split_level:
            saw_split_heading = True
            if buf:
                piece = "".join(buf).strip()
                if piece:
                    parts.extend(chunk_text(piece, chunk_size=max_chunk_chars, overlap=overlap))
            buf = [line]
        else:
            buf.append(line)

    if buf:
        piece = "".join(buf).strip()
        if piece:
            parts.extend(chunk_text(piece, chunk_size=max_chunk_chars, overlap=overlap))

    if not parts:
        return chunk_text(text, chunk_size=max_chunk_chars, overlap=overlap)
    if not saw_split_heading:
        return chunk_text(text, chunk_size=max_chunk_chars, overlap=overlap)
    return [c for c in parts if c]


def chunk_text(text: str, chunk_size: int = 3500, overlap: int = 400) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if chunk_size <= 0:
        return [text]
    overlap = max(0, min(overlap, chunk_size - 1))
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = end - overlap
    return [c for c in chunks if c]
