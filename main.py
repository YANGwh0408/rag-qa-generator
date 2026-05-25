"""Local web app: documents → Q&A generation for RAG knowledge bases."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from services.keyutil import normalize_secret
from services.pipeline import (
    CHUNK_PREVIEW_DISPLAY_LIMIT,
    DEFAULT_MERGED_PROMPT_TEMPLATE,
    DEFAULT_MERGED_SEGMENTS,
    build_sources,
    run_generation,
    run_generation_stream,
    sources_to_chunks,
)

STATIC = Path(__file__).resolve().parent / "static"

_DOC_SUFFIXES = (".pdf", ".docx", ".md", ".markdown", ".txt", ".text")


def _is_document_filename(name: str) -> bool:
    lower = (name or "").lower()
    return any(lower.endswith(s) for s in _DOC_SUFFIXES)


app = FastAPI(title="知识库问答生成工具", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ModelEndpoint(BaseModel):
    label: str = ""
    provider: str = "openai_compatible"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    compat_auth: str = "bearer"
    center_protocol: str = "openai"


def _default_models() -> list[ModelEndpoint]:
    return [
        ModelEndpoint(
            label="模型 1",
            provider="openai_compatible",
            base_url="https://llm-center.ali.modelbest.co/llm",
        )
    ]


class GenerateOptions(BaseModel):
    chunk_size: int = Field(3000, ge=400, le=50_000)
    chunk_overlap: int = Field(360, ge=0, le=10_000)
    markdown_heading_split: bool = False
    markdown_heading_level: int = Field(2, ge=1, le=6)
    loop_count: int = Field(1, ge=1, le=20)
    merged_prompt_template: str = ""
    task_form: str = ""
    question_difficulty: str = ""
    answer_length: str = ""
    language_style: str = ""
    quantity_strategy: str = ""
    content_coverage: str = ""
    models: list[ModelEndpoint] = Field(default_factory=_default_models, min_length=1)
    max_tokens: int = Field(8192, ge=256, le=128_000)
    temperature: float = Field(0.35, ge=0, le=2)
    max_retries: int = Field(3, ge=0, le=10)
    parallel_enabled: bool = False
    parallel_concurrency: int = Field(5, ge=1)

    @field_validator("markdown_heading_split", "parallel_enabled", mode="before")
    @classmethod
    def _coerce_bool(cls, v: object) -> bool:
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return bool(v)


class ChunkPreviewOptions(BaseModel):
    """与当前切片参数一致，预览将送给模型的文本块（前若干块）。"""

    chunk_size: int = Field(3000, ge=400, le=50_000)
    chunk_overlap: int = Field(360, ge=0, le=10_000)
    markdown_heading_split: bool = False
    markdown_heading_level: int = Field(2, ge=1, le=6)

    @field_validator("markdown_heading_split", mode="before")
    @classmethod
    def _coerce_chunk_preview_bool(cls, v: object) -> bool:
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return bool(v)


def _normalize_models_for_pipeline(opts: GenerateOptions) -> tuple[list[dict], str | None]:
    """Build kwargs for pipeline; return (models, error_message)."""
    if not opts.models:
        return [], "至少配置 1 个模型"
    m0 = opts.models[0]
    k0 = normalize_secret(m0.api_key)
    mid0 = (m0.model or "").strip()
    bu0 = (m0.base_url or "").strip()
    prov0 = (m0.provider or "openai_compatible").strip()
    if not k0:
        return [], "第 1 个模型请填写 API Key"
    if not mid0:
        return [], "第 1 个模型请填写模型 ID"
    if prov0 == "openai_compatible" and not bu0:
        return [], "第 1 个模型为兼容模式时需填写 Base URL"

    roots_base = bu0
    out: list[dict] = []
    for i, m in enumerate(opts.models):
        k = normalize_secret(m.api_key)
        if i > 0 and not k:
            k = k0
        mid = (m.model or "").strip()
        if i > 0 and not mid:
            mid = mid0
        lbl = (m.label or "").strip() or (mid or f"模型{i + 1}")
        bu_raw = (m.base_url or "").strip()
        bu = bu_raw if bu_raw else (roots_base if i > 0 else bu0)
        if not k:
            return [], f"第 {i + 1} 个模型请填写 API Key"
        if not mid:
            return [], f"第 {i + 1} 个模型请填写模型 ID"
        prov = (m.provider or "openai_compatible").strip()
        if prov == "openai_compatible":
            if not bu:
                return [], f"第 {i + 1} 个模型为兼容模式时需填写 Base URL（或与第一条共用）"
            base_url = bu
        else:
            base_url = None
        out.append(
            {
                "label": lbl,
                "provider": prov,
                "model": mid,
                "api_key": k,
                "base_url": base_url,
                "compat_auth": m.compat_auth,
                "center_protocol": m.center_protocol,
            }
        )
    return out, None


def _pipeline_common_kwargs(opts: GenerateOptions, models_norm: list[dict]) -> dict:
    return {
        "chunk_size": opts.chunk_size,
        "chunk_overlap": opts.chunk_overlap,
        "markdown_heading_split": opts.markdown_heading_split,
        "markdown_heading_level": opts.markdown_heading_level,
        "loop_count": opts.loop_count,
        "models": models_norm,
        "merged_prompt_template": opts.merged_prompt_template,
        "task_form": opts.task_form,
        "question_difficulty": opts.question_difficulty,
        "answer_length": opts.answer_length,
        "language_style": opts.language_style,
        "quantity_strategy": opts.quantity_strategy,
        "content_coverage": opts.content_coverage,
        "temperature": opts.temperature,
        "max_tokens": opts.max_tokens,
        "max_retries": opts.max_retries,
        "parallel_enabled": opts.parallel_enabled,
        "parallel_concurrency": opts.parallel_concurrency,
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/playwright-status")
def playwright_status():
    """确认「跑 uvicorn 的这一份 Python」是否已安装 playwright（与系统全局 pip 无关）。"""
    import sys

    exe = sys.executable
    try:
        import playwright

        ver = getattr(playwright, "__version__", "unknown")
    except ImportError:
        return {
            "ok": False,
            "playwright_import_ok": False,
            "python_executable": exe,
            "hint": "在此解释器下执行: pip install playwright && python -m playwright install chromium",
        }
    return {
        "ok": True,
        "playwright_import_ok": True,
        "playwright_version": ver,
        "python_executable": exe,
        "hint": "若渲染仍失败，在同一解释器下执行: python -m playwright install chromium",
    }


@app.get("/api/defaults")
def defaults():
    return {
        "merged_prompt_template": DEFAULT_MERGED_PROMPT_TEMPLATE,
        **DEFAULT_MERGED_SEGMENTS,
    }


async def _read_uploaded_documents(files: Optional[list[UploadFile]]) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for uf in files or []:
        if not uf.filename or not _is_document_filename(uf.filename):
            continue
        data = await uf.read()
        out.append((uf.filename, data))
    return out


@app.post("/api/generate")
async def generate(
    options_json: str = Form(...),
    files: Optional[list[UploadFile]] = File(None),
):
    try:
        opts = GenerateOptions.model_validate_json(options_json)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"参数解析失败: {e}"})

    models_norm, m_err = _normalize_models_for_pipeline(opts)
    if m_err:
        return JSONResponse(status_code=400, content={"ok": False, "error": m_err})

    doc_files = await _read_uploaded_documents(files)

    try:
        items, errors = await asyncio.to_thread(
            run_generation,
            document_files=doc_files,
            **_pipeline_common_kwargs(opts, models_norm),
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "errors": errors,
        "model_count": len(models_norm),
    }


def _stream_generation_chunks(
    opts: GenerateOptions,
    models_norm: list[dict],
    document_files: list[tuple[str, bytes]],
):
    def gen():
        try:
            for ev in run_generation_stream(
                document_files=document_files,
                **_pipeline_common_kwargs(opts, models_norm),
            ):
                yield (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
        except Exception as e:
            err = json.dumps(
                {
                    "type": "done",
                    "ok": False,
                    "message": f"服务端异常：{e}",
                    "items": [],
                    "errors": [str(e)],
                    "count": 0,
                    "model_count": len(models_norm),
                },
                ensure_ascii=False,
            )
            yield (err + "\n").encode("utf-8")

    return gen()


@app.post("/api/generate-stream")
async def generate_stream(
    options_json: str = Form(...),
    files: Optional[list[UploadFile]] = File(None),
):
    """NDJSON 流：progress / log / 最终 done。"""
    try:
        opts = GenerateOptions.model_validate_json(options_json)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"参数解析失败: {e}"})

    models_norm, m_err = _normalize_models_for_pipeline(opts)
    if m_err:
        return JSONResponse(status_code=400, content={"ok": False, "error": m_err})

    doc_files = await _read_uploaded_documents(files)

    return StreamingResponse(
        _stream_generation_chunks(opts, models_norm, doc_files),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/preview-text")
async def preview_text(
    files: Optional[list[UploadFile]] = File(None),
    max_chars: int = Form(50_000),
):
    max_chars = max(0, min(int(max_chars), 500_000))

    doc_files = await _read_uploaded_documents(files)

    try:
        sources, warnings = await asyncio.to_thread(build_sources, doc_files)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    previews = []
    for label, text in sources:
        limit = max_chars if max_chars > 0 else len(text)
        snippet = text[:limit] if limit < len(text) else text
        previews.append(
            {
                "source": label,
                "chars": len(text),
                "preview": snippet,
                "preview_limit": limit,
                "truncated": len(text) > len(snippet),
            }
        )
    return {"ok": True, "sources": previews, "warnings": warnings}


@app.post("/api/preview-chunks")
async def preview_chunks(
    options_json: str = Form(...),
    files: Optional[list[UploadFile]] = File(None),
):
    """
    与「开始生成」共用 build_sources + sources_to_chunks；返回的 chunks[:N].text
    即为按同一顺序送给模型的前 N 段原文（N=CHUNK_PREVIEW_DISPLAY_LIMIT）。
    """
    try:
        opts = ChunkPreviewOptions.model_validate_json(options_json)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"参数解析失败: {e}"})

    doc_files = await _read_uploaded_documents(files)

    def run() -> tuple[list[dict] | None, int, list[str], list[str], str | None]:
        sources, warnings = build_sources(doc_files)
        if not sources:
            return None, 0, warnings, [], "未得到有效文本：请检查已上传文件。"
        chunks, logs = sources_to_chunks(
            sources,
            opts.chunk_size,
            opts.chunk_overlap,
            markdown_heading_split=opts.markdown_heading_split,
            markdown_heading_level=opts.markdown_heading_level,
        )
        if not chunks:
            return None, 0, warnings, logs, "切分后没有有效片段。"
        out: list[dict] = []
        for c in chunks[:CHUNK_PREVIEW_DISPLAY_LIMIT]:
            out.append(
                {
                    "source": c.source_label,
                    "chunk_index": c.chunk_index,
                    "chars": len(c.text),
                    "text": c.text,
                }
            )
        return out, len(chunks), warnings, logs, None

    try:
        chunk_list, total_chunks, warnings, logs, err = await asyncio.to_thread(run)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    if err:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": err, "warnings": warnings, "logs": logs},
        )

    return {
        "ok": True,
        "chunks": chunk_list or [],
        "total_chunks": total_chunks,
        "preview_limit": CHUNK_PREVIEW_DISPLAY_LIMIT,
        # 与 run_generation / run_generation_stream 共用 sources_to_chunks；chunks[k].text == 该次运行中第 k 个 SourceChunk.text
        "chunks_match_generation_segments": True,
        "warnings": warnings,
        "logs": logs,
    }
