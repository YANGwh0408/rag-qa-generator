"""Orchestrate extract → chunk → LLM QA generation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from services.chunk import chunk_markdown_by_heading, chunk_text, text_has_atx_heading_at_level
from services.extract import extract_document_bytes
from services.llm_client import InvalidApiKeyError, generate_qa_json


# 六段可配置文案的占位键（与 MERGED 模板中 {task_form} 等一致）
MERGED_SEGMENT_KEYS: tuple[str, ...] = (
    "task_form",
    "question_difficulty",
    "answer_length",
    "language_style",
    "quantity_strategy",
    "content_coverage",
)

DEFAULT_MERGED_SEGMENTS: dict[str, str] = {
    "task_form": "标准知识型问答，重点提取文档中的核心事实与可检索表述。",
    "question_difficulty": "以基础理解为主：优先围绕“是什么/指什么”“有哪些/包含什么”“条件/限制是什么”“如何/流程是什么”“适用范围/注意事项是什么”等可自洽问题提问；不做片段外推理与场景臆测。",
    "answer_length": "通常单条答案保持在 30～150 字之间，信息极少时以忠实为先、可更短；保证逻辑连贯、可入库检索。禁止为凑长度扩写。",
    "language_style": "正式书面且严谨：术语、缩写、字段名等等与原文保持一致；表达客观、避免口语与夸张修饰。",
    "quantity_strategy": "数量由模型结合本片段信息自行决定：内容丰富可适当增加，内容贫乏则宁缺毋滥，禁止灌水重复。",
    "content_coverage": "优先提取高频检索点与核心定义，兼顾重要补充信息，尽量全覆盖所有信息。",
}

DEFAULT_MERGED_PROMPT_TEMPLATE = """
# Role
你是一个极度严谨的 RAG（Retrieval-Augmented Generation）数据工程专家。你的任务是从文本片段中提取高质量的中文问答对，用于优化知识库检索效果。

# 核心目标
根据下方文档片段，生成适合知识库检索的高质量问答对。生成结果必须准确、自洽、可检索，并严格符合输出格式要求。

# 硬性要求（不可违背）
1. 问题必须自洽 (Self-contained)
- 严禁使用“它、其、该、此、上文、该方案、此产品、上述内容”等任何依赖上下文的指代词。
- 问题必须包含明确主体名称（如具体产品名、技术术语、组织名称、流程名称等）。
- 确保任何人在不看原始片段的情况下，仅能凭问题就完全理解提问对象和意图。

2. 答案必须忠实 (Faithful)
- 答案必须完全由提供的文档片段支撑。禁止引入外部知识或主观推断。
- 严禁编造事实。若片段中包含具体数字、阈值、版本、时间、范围、单位、步骤顺序，答案必须精准还原。
- 字数与信息量：若原文信息量极小，优先保证答案忠实、不臆测；不受人为“字数下限”束缚，严禁为凑字数而引入片段中不存在的推论或铺垫。

3. 逻辑与搜索优化
- 在构造问题时，必须保留并准确拼写原文中的专业术语、缩写词、产品名、字段名等。
- 严禁语义重复：若两个问题核心考点相同，仅保留最直观、关键词最明确的一个。

# 本轮任务配置（由用户自定义，须与上文硬性要求一并遵守；若冲突，以更本轮任务配置为准）

## 任务形式
{task_form}

## 问题难度
{question_difficulty}

## 答案长度
{answer_length}

## 语言风格
{language_style}

## 数量策略
{quantity_strategy}

## 内容覆盖
{content_coverage}

# 任务上下文
- 当前轮次：第 {round} 轮
- 本片段应生成多少组问答：完全以上方「## 数量策略」中的文字为准。

# 文档片段（Text Segment）
---
{text}
---

# 执行流程
1. 识别核心知识点：扫描片段，提取具有检索价值的实体与概念。
2. 构造初稿：按「本轮任务配置」生成不重复的中文问答对。
3. 去指代化：检查并消除所有上下文依赖表达。
4. 校验忠实度：核对每个答案在片段中是否有明确依据；无法支撑则不生成该条。
5. 格式封装：输出合法 JSON 数组；若 question 或 answer 中含双引号、换行、反斜杠等，须正确转义。

# 输出禁令
- 仅输出一个 JSON 数组，不要任何其它文字。
- 禁止 Markdown 代码围栏、解释、标题、Q/A 纯文本。
- 若片段不包含任何有效核心知识点，直接输出空数组 []，严禁解释原因。

# 输出格式要求
[{"question":"……","answer":"……"},{"question":"……","answer":"……"}]
"""

LARGE_DOC_CHAR_THRESHOLD = 120_000


@dataclass
class SourceChunk:
    source_label: str
    chunk_index: int
    text: str


# 单次阅读预览最多展示的块数。与 run_generation / run_generation_stream 使用同一 sources_to_chunks 列表：
# 第 i 个 SourceChunk（i 从 0 计）的 .text 即 _build_merged_prompt(..., text=sc.text) 里填入「文档片段」的完整原文；
# 预览接口仅返回前 CHUNK_PREVIEW_DISPLAY_LIMIT 条，避免响应过大。
CHUNK_PREVIEW_DISPLAY_LIMIT = 6


def build_sources(
    document_files: list[tuple[str, bytes]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """从上传文件解析文本。Return (sources, per-file errors)."""
    out: list[tuple[str, str]] = []
    errs: list[str] = []
    for name, data in document_files:
        try:
            text = extract_document_bytes(name, data)
            if text:
                out.append((name, text))
            else:
                errs.append(f"{name}：未解析到文本")
        except Exception as e:
            errs.append(f"{name}：{e}")
    return out, errs


def _looks_like_markdown_source(label: str) -> bool:
    """上传文件名以 .md / .markdown 结尾时视为 Markdown 源（取 basename，兼容带路径的上传名）。"""
    s = (label or "").strip()
    if not s:
        return False
    name = Path(s.replace("\\", "/")).name.lower()
    name = name.split("?", 1)[0].split("#", 1)[0]
    return name.endswith(".md") or name.endswith(".markdown")


def sources_to_chunks(
    sources: list[tuple[str, str]],
    chunk_size: int,
    overlap: int,
    *,
    markdown_heading_split: bool = False,
    markdown_heading_level: int = 2,
) -> tuple[list[SourceChunk], list[str]]:
    """
    按文档自适应片段大小（超大文档自动用更小 chunk）；可选对 .md 按 ATX 标题切分。
    返回的 chunks 顺序即生成管线中的处理顺序；每项 text 与发给大模型的该段原文一致。
    """
    chunks: list[SourceChunk] = []
    logs: list[str] = []
    level = max(1, min(6, int(markdown_heading_level)))
    for label, text in sources:
        n = len(text)
        cs = chunk_size
        if n > LARGE_DOC_CHAR_THRESHOLD:
            cs = max(900, min(chunk_size, 2600))
            logs.append(f"「{label}」约 {n} 字，已自动使用片段长度 {cs} 以稳定调用与切片。")
        ov = max(0, min(overlap, cs - 1)) if cs > 1 else 0
        # 开启按标题切分时：.md/.markdown 名，或正文里确有该级 ATX 标题（避免文件名异常时误走纯字数切）
        use_md = markdown_heading_split and (
            _looks_like_markdown_source(label) or text_has_atx_heading_at_level(text, level)
        )
        if use_md:
            pieces = chunk_markdown_by_heading(
                text, split_level=level, max_chunk_chars=cs, overlap=ov
            )
            logs.append(
                f"「{label}」已按 Markdown {level} 级标题（# 个数）切分，共 {len(pieces)} 段；"
                f"无该级标题或单节过长时仍按「单次阅读多少字」再切。"
            )
        else:
            pieces = chunk_text(text, chunk_size=cs, overlap=ov)
        for i, piece in enumerate(pieces):
            chunks.append(SourceChunk(source_label=label, chunk_index=i, text=piece))
    return chunks, logs


def _build_merged_prompt(
    template: str,
    *,
    text: str,
    round_idx: int,
    task_form: str,
    question_difficulty: str,
    answer_length: str,
    language_style: str,
    quantity_strategy: str,
    content_coverage: str,
) -> str:
    """单条用户消息：合并后的全文。用 replace 填充，避免文档片段中的花括号破坏 str.format。"""
    tpl = (template or "").strip() or DEFAULT_MERGED_PROMPT_TEMPLATE
    segs = {
        "task_form": (task_form or "").strip() or DEFAULT_MERGED_SEGMENTS["task_form"],
        "question_difficulty": (question_difficulty or "").strip() or DEFAULT_MERGED_SEGMENTS["question_difficulty"],
        "answer_length": (answer_length or "").strip() or DEFAULT_MERGED_SEGMENTS["answer_length"],
        "language_style": (language_style or "").strip() or DEFAULT_MERGED_SEGMENTS["language_style"],
        "quantity_strategy": (quantity_strategy or "").strip() or DEFAULT_MERGED_SEGMENTS["quantity_strategy"],
        "content_coverage": (content_coverage or "").strip() or DEFAULT_MERGED_SEGMENTS["content_coverage"],
    }
    out = tpl.replace("{round}", str(round_idx + 1))
    # 旧版模板若仍含 {pairs}，不再由服务端填数字，避免与「数量策略」双轨
    if "{pairs}" in out:
        out = out.replace("{pairs}", "（已由「数量策略」统一约定，此处不再单独给出数字）")
    for k, v in segs.items():
        out = out.replace("{" + k + "}", v)
    out = out.replace("{text}", text)
    return out


@dataclass(frozen=True)
class _GenWorkItem:
    """单次模型调用任务（一个片段 × 一轮 × 一个模型）。"""

    step: int
    sort_key: tuple[int, int, int]
    model_index: int
    label: str
    model_id: str
    model: dict[str, Any]
    chunk: SourceChunk
    round_idx: int


def _prompt_segment_kwargs(
    *,
    merged_prompt_template: str,
    task_form: str,
    question_difficulty: str,
    answer_length: str,
    language_style: str,
    quantity_strategy: str,
    content_coverage: str,
) -> dict[str, str]:
    return {
        "merged_prompt_template": merged_prompt_template,
        "task_form": task_form,
        "question_difficulty": question_difficulty,
        "answer_length": answer_length,
        "language_style": language_style,
        "quantity_strategy": quantity_strategy,
        "content_coverage": content_coverage,
    }


def _build_work_items(
    *,
    chunks: list[SourceChunk],
    loops: int,
    models: list[dict[str, Any]],
) -> list[_GenWorkItem]:
    items: list[_GenWorkItem] = []
    step = 0
    for mi, m in enumerate(models):
        label = str(m.get("label") or m.get("model") or f"模型{mi + 1}")
        mid = str(m.get("model") or "").strip()
        for ci, sc in enumerate(chunks):
            for round_idx in range(loops):
                step += 1
                items.append(
                    _GenWorkItem(
                        step=step,
                        sort_key=(mi, ci, round_idx),
                        model_index=mi,
                        label=label,
                        model_id=mid,
                        model=m,
                        chunk=sc,
                        round_idx=round_idx,
                    )
                )
    return items


def _work_item_detail(item: _GenWorkItem, *, loops: int) -> str:
    sc = item.chunk
    return (
        f"{item.label} · {sc.source_label} · 块 {sc.chunk_index} · "
        f"第 {item.round_idx + 1}/{loops} 轮"
    )


def _execute_work_item(
    item: _GenWorkItem,
    *,
    temperature: float,
    max_tokens: int,
    max_retries: int,
    **prompt_kwargs: str,
) -> tuple[list[dict], None]:
    sc = item.chunk
    merged = _build_merged_prompt(
        prompt_kwargs.get("merged_prompt_template", ""),
        text=sc.text,
        round_idx=item.round_idx,
        task_form=prompt_kwargs["task_form"],
        question_difficulty=prompt_kwargs["question_difficulty"],
        answer_length=prompt_kwargs["answer_length"],
        language_style=prompt_kwargs["language_style"],
        quantity_strategy=prompt_kwargs["quantity_strategy"],
        content_coverage=prompt_kwargs["content_coverage"],
    )
    pairs = generate_qa_json(
        provider=str(item.model["provider"]),
        api_key=str(item.model["api_key"]),
        model=item.model_id,
        base_url=item.model.get("base_url"),
        system_prompt="",
        user_prompt=merged,
        temperature=temperature,
        max_tokens=max_tokens,
        compat_auth=str(item.model.get("compat_auth") or "bearer"),
        center_protocol=str(item.model.get("center_protocol") or "openai"),
        max_retries=max_retries,
    )
    rows = [
        {
            "question": p["question"],
            "answer": p["answer"],
            "source": sc.source_label,
            "chunk_index": sc.chunk_index,
            "round": item.round_idx + 1,
            "pair_index": j,
            "model_label": item.label,
            "model_id": item.model_id,
        }
        for j, p in enumerate(pairs)
    ]
    return rows, None


def run_generation(
    *,
    document_files: list[tuple[str, bytes]],
    chunk_size: int,
    chunk_overlap: int,
    markdown_heading_split: bool = False,
    markdown_heading_level: int = 2,
    loop_count: int,
    models: list[dict[str, Any]],
    merged_prompt_template: str,
    task_form: str,
    question_difficulty: str,
    answer_length: str,
    language_style: str,
    quantity_strategy: str,
    content_coverage: str,
    temperature: float,
    max_tokens: int = 4096,
    max_retries: int = 3,
    parallel_enabled: bool = False,
    parallel_concurrency: int = 5,
) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    sources, src_errs = build_sources(document_files)
    errors.extend(src_errs)
    if not sources:
        return [], errors + ["未得到有效文本：请检查已上传文件格式与内容。"]

    chunks, chunk_logs = sources_to_chunks(
        sources,
        chunk_size,
        chunk_overlap,
        markdown_heading_split=markdown_heading_split,
        markdown_heading_level=markdown_heading_level,
    )
    errors.extend(chunk_logs)
    if not chunks:
        return [], errors + ["切分后没有有效片段。"]

    loops = max(1, loop_count)
    prompt_kwargs = _prompt_segment_kwargs(
        merged_prompt_template=merged_prompt_template,
        task_form=task_form,
        question_difficulty=question_difficulty,
        answer_length=answer_length,
        language_style=language_style,
        quantity_strategy=quantity_strategy,
        content_coverage=content_coverage,
    )
    work_items = _build_work_items(chunks=chunks, loops=loops, models=models)
    call_kwargs = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "max_retries": max_retries,
        **prompt_kwargs,
    }

    if parallel_enabled and parallel_concurrency > 1 and len(work_items) > 1:
        workers = min(parallel_concurrency, len(work_items))
        ordered: list[tuple[tuple[int, int, int], list[dict]]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_execute_work_item, item, **call_kwargs): item for item in work_items}
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    rows, _ = fut.result()
                    ordered.append((item.sort_key, rows))
                except InvalidApiKeyError as e:
                    errors.append(str(e))
                    for pending in futures:
                        pending.cancel()
                    flat = [row for _, rows in sorted(ordered, key=lambda x: x[0]) for row in rows]
                    return flat, errors
                except Exception as e:
                    errors.append(f"{_work_item_detail(item, loops=loops)}：{e!s}")
        results = [row for _, rows in sorted(ordered, key=lambda x: x[0]) for row in rows]
        return results, list(dict.fromkeys(errors))

    results: list[dict] = []
    for item in work_items:
        try:
            rows, _ = _execute_work_item(item, **call_kwargs)
            results.extend(rows)
        except InvalidApiKeyError as e:
            errors.append(str(e))
            return results, errors
        except Exception as e:
            errors.append(f"{_work_item_detail(item, loops=loops)}：{e!s}")

    return results, list(dict.fromkeys(errors))


def run_generation_stream(
    *,
    document_files: list[tuple[str, bytes]],
    chunk_size: int,
    chunk_overlap: int,
    markdown_heading_split: bool = False,
    markdown_heading_level: int = 2,
    loop_count: int,
    models: list[dict[str, Any]],
    merged_prompt_template: str,
    task_form: str,
    question_difficulty: str,
    answer_length: str,
    language_style: str,
    quantity_strategy: str,
    content_coverage: str,
    temperature: float,
    max_tokens: int = 4096,
    max_retries: int = 3,
    parallel_enabled: bool = False,
    parallel_concurrency: int = 5,
) -> Iterator[dict[str, Any]]:
    errors: list[str] = []
    sources, src_errs = build_sources(document_files)
    for e in src_errs:
        yield {"type": "log", "level": "warn", "message": e}
    errors.extend(src_errs)

    if not sources:
        yield {
            "type": "done",
            "ok": False,
            "message": "未得到有效文本：请检查已上传文件。",
            "items": [],
            "errors": errors + ["未得到有效文本。"],
            "count": 0,
            "aborted": True,
            "model_count": 0,
        }
        return

    chunks, chunk_logs = sources_to_chunks(
        sources,
        chunk_size,
        chunk_overlap,
        markdown_heading_split=markdown_heading_split,
        markdown_heading_level=markdown_heading_level,
    )
    for line in chunk_logs:
        yield {"type": "log", "level": "info", "message": line}
    errors.extend(chunk_logs)

    if not chunks:
        yield {
            "type": "done",
            "ok": False,
            "message": "切分后没有有效片段。",
            "items": [],
            "errors": errors + ["切分后没有有效片段。"],
            "count": 0,
            "aborted": True,
            "model_count": 0,
        }
        return

    loops = max(1, loop_count)
    n_models = max(1, len(models))
    total_steps = len(chunks) * loops * n_models
    use_parallel = parallel_enabled and parallel_concurrency > 1 and total_steps > 1
    workers = min(parallel_concurrency, total_steps) if use_parallel else 1
    prompt_kwargs = _prompt_segment_kwargs(
        merged_prompt_template=merged_prompt_template,
        task_form=task_form,
        question_difficulty=question_difficulty,
        answer_length=answer_length,
        language_style=language_style,
        quantity_strategy=quantity_strategy,
        content_coverage=content_coverage,
    )
    work_items = _build_work_items(chunks=chunks, loops=loops, models=models)
    call_kwargs = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "max_retries": max_retries,
        **prompt_kwargs,
    }

    yield {
        "type": "start",
        "total_steps": total_steps,
        "chunk_count": len(chunks),
        "loop_count": loops,
        "model_count": n_models,
        "parallel_enabled": use_parallel,
        "parallel_requested": parallel_enabled,
        "parallel_concurrency": workers if use_parallel else 1,
        "parallel_concurrency_requested": parallel_concurrency,
    }

    if use_parallel:
        yield {
            "type": "log",
            "level": "info",
            "message": (
                f"多线程已生效：ThreadPoolExecutor max_workers={workers}，"
                f"已同时提交 {len(work_items)} 个 API 任务并行执行（完成顺序可能与切片顺序不同）"
            ),
        }
    elif parallel_enabled:
        reason = "并发数 ≤ 1" if parallel_concurrency <= 1 else f"本次仅 {total_steps} 次调用"
        yield {
            "type": "log",
            "level": "info",
            "message": f"已开启多线程选项，但{reason}，仍按单线程顺序执行",
        }

    results: list[dict] = []
    ordered_rows: list[tuple[tuple[int, int, int], list[dict]]] = []

    def _append_rows(item: _GenWorkItem, rows: list[dict]) -> None:
        if use_parallel:
            ordered_rows.append((item.sort_key, rows))
            flat = [row for _, chunk_rows in sorted(ordered_rows, key=lambda x: x[0]) for row in chunk_rows]
            results.clear()
            results.extend(flat)
        else:
            results.extend(rows)

    if use_parallel:
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_execute_work_item, item, **call_kwargs): item for item in work_items}
            for fut in as_completed(futures):
                item = futures[fut]
                detail_base = _work_item_detail(item, loops=loops)
                completed += 1
                yield {
                    "type": "progress",
                    "step": completed,
                    "total_steps": total_steps,
                    "detail": detail_base,
                }
                try:
                    rows, _ = fut.result()
                except InvalidApiKeyError as e:
                    err_msg = str(e)
                    errors.append(err_msg)
                    yield {"type": "log", "level": "error", "message": f"[{item.label}] 密钥无效：{err_msg}"}
                    for pending in futures:
                        pending.cancel()
                    yield {
                        "type": "done",
                        "ok": len(results) > 0,
                        "message": err_msg,
                        "items": results,
                        "errors": errors,
                        "count": len(results),
                        "aborted": True,
                        "model_count": n_models,
                    }
                    return
                except Exception as e:
                    err_line = f"{detail_base}：{e!s}"
                    errors.append(err_line)
                    yield {"type": "log", "level": "warn", "message": err_line}
                    yield {"type": "warn", "step": completed, "message": f"{detail_base}：已跳过，继续。"}
                    continue
                _append_rows(item, rows)
                yield {
                    "type": "log",
                    "level": "info",
                    "message": f"{detail_base} 完成，新增 {len(rows)} 组（至多 {max_retries + 1} 次尝试含退避）",
                }
                yield {
                    "type": "step_ok",
                    "step": completed,
                    "added": len(rows),
                    "total_qa": len(results),
                    "model_count": n_models,
                }
    else:
        for item in work_items:
            detail_base = _work_item_detail(item, loops=loops)
            yield {
                "type": "progress",
                "step": item.step,
                "total_steps": total_steps,
                "detail": detail_base,
            }
            yield {
                "type": "log",
                "level": "info",
                "message": f"{detail_base}（至多 {max_retries + 1} 次尝试含退避）",
            }
            try:
                rows, _ = _execute_work_item(item, **call_kwargs)
            except InvalidApiKeyError as e:
                err_msg = str(e)
                errors.append(err_msg)
                yield {"type": "log", "level": "error", "message": f"[{item.label}] 密钥无效：{err_msg}"}
                yield {
                    "type": "done",
                    "ok": len(results) > 0,
                    "message": err_msg,
                    "items": results,
                    "errors": errors,
                    "count": len(results),
                    "aborted": True,
                    "model_count": n_models,
                }
                return
            except Exception as e:
                err_line = f"{detail_base}：{e!s}"
                errors.append(err_line)
                yield {"type": "log", "level": "warn", "message": err_line}
                yield {"type": "warn", "step": item.step, "message": f"{detail_base}：已跳过，继续。"}
                continue
            _append_rows(item, rows)
            yield {
                "type": "step_ok",
                "step": item.step,
                "added": len(rows),
                "total_qa": len(results),
                "model_count": n_models,
            }

    uniq_errs = list(dict.fromkeys(errors))
    yield {
        "type": "done",
        "ok": len(results) > 0,
        "message": ("未生成任何问答，请查看日志与错误。" if not results and uniq_errs else None),
        "items": results,
        "errors": uniq_errs,
        "count": len(results),
        "aborted": False,
        "model_count": n_models,
    }
