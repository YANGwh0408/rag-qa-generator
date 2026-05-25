const $ = (sel) => document.querySelector(sel);

const THEME_KEY = "qa-tool-theme";

const DOC_EXT = /\.(pdf|docx|md|markdown|txt|text)$/i;

const state = {
  files: [],
  abortController: null,
};

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function syncModelRowUi(row) {
  if (!row) return;
  const prov = row.querySelector('[data-field="provider"]')?.value;
  const proto = row.querySelector('[data-field="center_protocol"]')?.value || "openai";
  const isCompat = prov === "openai_compatible";
  const baseEl = row.querySelector(".model-base-row");
  const protoEl = row.querySelector(".model-protocol-row");
  const authEl = row.querySelector(".model-compat-auth-row");
  if (baseEl) baseEl.hidden = !isCompat;
  if (protoEl) protoEl.hidden = !isCompat;
  if (authEl) authEl.hidden = !isCompat || proto !== "openai";
}

function updateRemoveModelButtons() {
  const rows = document.querySelectorAll("[data-model-row]");
  rows.forEach((r) => {
    const btn = r.querySelector(".btn-remove-model");
    if (btn) btn.disabled = rows.length <= 1;
  });
}

function addModelRow() {
  const list = $("#models-list");
  const first = list?.querySelector("[data-model-row]");
  if (!list || !first) return;
  const clone = first.cloneNode(true);
  clone.querySelectorAll("input").forEach((el) => {
    if (el.type === "password" || el.type === "text") el.value = "";
  });
  const bu = clone.querySelector('[data-field="base_url"]');
  if (bu) bu.value = "";
  const midIn = clone.querySelector('[data-field="model"]');
  if (midIn) midIn.placeholder = "留空则与主模型（第 1 条）相同";
  const keyIn = clone.querySelector('[data-field="api_key"]');
  if (keyIn) keyIn.placeholder = "留空则与主模型（第 1 条）相同";
  const prov = clone.querySelector('[data-field="provider"]');
  if (prov) prov.value = "openai_compatible";
  list.appendChild(clone);
  syncModelRowUi(clone);
  updateRemoveModelButtons();
}

function removeModelRow(row) {
  const list = $("#models-list");
  if (!list || !row) return;
  const rows = list.querySelectorAll("[data-model-row]");
  if (rows.length <= 1) return;
  row.remove();
  updateRemoveModelButtons();
}

function getMarkdownHeadingLevel() {
  const r = document.querySelector('input[name="markdown_heading_level"]:checked');
  return Math.max(1, Math.min(6, Number(r?.value) || 2));
}

function initModelsList() {
  const list = $("#models-list");
  if (!list) return;
  list.querySelectorAll("[data-model-row]").forEach(syncModelRowUi);
  list.addEventListener("change", (e) => {
    const t = e.target;
    if (!t.matches?.('[data-field="provider"], [data-field="center_protocol"]')) return;
    syncModelRowUi(t.closest("[data-model-row]"));
  });
  list.addEventListener("click", (e) => {
    const rm = e.target.closest?.(".btn-remove-model");
    if (rm) removeModelRow(rm.closest("[data-model-row]"));
  });
  $("#btn-add-model")?.addEventListener("click", addModelRow);
  updateRemoveModelButtons();
}

function collectModels() {
  return Array.from(document.querySelectorAll("[data-model-row]")).map((row) => ({
    label: row.querySelector('[data-field="label"]')?.value?.trim() || "",
    provider: row.querySelector('[data-field="provider"]')?.value || "openai_compatible",
    model: row.querySelector('[data-field="model"]')?.value?.trim() || "",
    api_key: normalizePastedKey(row.querySelector('[data-field="api_key"]')?.value || ""),
    base_url: row.querySelector('[data-field="base_url"]')?.value?.trim() || "",
    compat_auth: row.querySelector('[data-field="compat_auth"]')?.value || "bearer",
    center_protocol: row.querySelector('[data-field="center_protocol"]')?.value || "openai",
  }));
}

function fileKey(f) {
  return `${f.name}:${f.size}:${f.lastModified}`;
}

function setDocumentTransferPanel(visible, opts = {}) {
  const panel = $("#document-transfer-panel");
  const bar = $("#document-transfer-bar");
  const hint = $("#document-transfer-hint");
  const title = $("#document-transfer-title");
  const badge = $("#document-transfer-badge");
  if (!panel || !bar || !hint) return;
  panel.hidden = !visible;
  if (!visible) {
    bar.classList.remove("indeterminate");
    bar.style.width = "0%";
    if (badge) {
      badge.textContent = "";
      badge.className = "document-transfer-badge";
    }
    return;
  }
  if (title && opts.title != null) title.textContent = opts.title;
  if (opts.hint != null) hint.textContent = opts.hint;
  if (badge) {
    badge.textContent = opts.badge || "";
    badge.className = "document-transfer-badge" + (opts.badgeClass ? ` ${opts.badgeClass}` : "");
  }
  if (opts.indeterminate) {
    bar.classList.add("indeterminate");
    bar.style.width = "100%";
  } else {
    bar.classList.remove("indeterminate");
    const p = opts.pct != null ? Math.min(100, Math.max(0, opts.pct)) : 0;
    bar.style.width = p + "%";
  }
}

function runLocalPickProgress(addedCount) {
  if (addedCount <= 0) return;
  setDocumentTransferPanel(true, {
    title: "本地文件",
    hint: `已选择 ${addedCount} 个文件加入列表（尚未发往服务器，预览/生成时会显示实际上传进度）`,
    pct: 0,
    badge: "读取中…",
  });
  let t = 0;
  const id = setInterval(() => {
    t += 0.15;
    const p = Math.min(100, Math.round(t * 100));
    setDocumentTransferPanel(true, {
      title: "本地文件",
      hint: "正在整理文件列表…",
      pct: p,
      badge: p < 100 ? "处理中" : "完成",
      badgeClass: p >= 100 ? "is-done" : "",
    });
    if (p >= 100) {
      clearInterval(id);
      setTimeout(() => setDocumentTransferPanel(false), 900);
    }
  }, 40);
}

/** 预览：XHR + 上传进度，返回解析后的 JSON */
function postFormDataJsonWithProgress(url, formData) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        setDocumentTransferPanel(true, {
          title: "上传到服务器（预览）",
          hint: `已上传 ${formatBytes(e.loaded)} / ${formatBytes(e.total)}`,
          pct,
          badge: `${pct}%`,
        });
      } else {
        setDocumentTransferPanel(true, {
          title: "上传到服务器（预览）",
          hint: "正在上传…",
          indeterminate: true,
          badge: "上传中",
        });
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response);
        return;
      }
      let msg = `HTTP ${xhr.status}`;
      try {
        if (typeof xhr.response === "object" && xhr.response?.error) msg = xhr.response.error;
        else if (xhr.responseText) msg = xhr.responseText.slice(0, 500);
      } catch (_) {}
      reject(new Error(msg));
    };
    xhr.onerror = () => reject(new Error("网络错误"));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));
    xhr.send(formData);
  });
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

/**
 * 流式生成：XHR 支持 upload 进度 + 增量解析 NDJSON（可 abort）
 */
function streamGenerateNdjsonWithXhr(url, formData, onRecord, signal) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    let buf = "";
    let processed = 0;

    const onAbort = () => {
      xhr.abort();
    };
    if (signal) {
      if (signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      signal.addEventListener("abort", onAbort);
    }

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 95);
        setDocumentTransferPanel(true, {
          title: "上传到服务器（生成）",
          hint: `已上传 ${formatBytes(e.loaded)} / ${formatBytes(e.total)}`,
          pct,
          badge: `${Math.round((e.loaded / e.total) * 100)}%`,
        });
      } else {
        setDocumentTransferPanel(true, {
          title: "上传到服务器（生成）",
          hint: "正在上传请求体…",
          indeterminate: true,
          badge: "上传中",
        });
      }
    };
    xhr.upload.addEventListener("load", () => {
      setDocumentTransferPanel(true, {
        title: "服务器处理中",
        hint: "已提交，正在流式接收生成进度…",
        pct: 100,
        badge: "已连接",
        badgeClass: "is-done",
      });
      setTimeout(() => setDocumentTransferPanel(false), 600);
    });

    const pump = () => {
      const text = xhr.responseText || "";
      if (text.length <= processed) return;
      buf += text.slice(processed);
      processed = text.length;
      const lines = buf.split("\n");
      buf = lines.pop() ?? "";
      for (const line of lines) {
        const s = line.trim();
        if (!s) continue;
        try {
          onRecord(JSON.parse(s));
        } catch (e) {
          console.warn("skip ndjson", s, e);
        }
      }
    };

    const iv = setInterval(() => {
      if (xhr.readyState >= 3) pump();
    }, 80);

    xhr.onreadystatechange = () => {
      if (xhr.readyState >= 3) pump();
    };

    xhr.onload = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
      clearInterval(iv);
      pump();
      const tail = buf.trim();
      if (tail) {
        try {
          onRecord(JSON.parse(tail));
        } catch (_) {}
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error((xhr.responseText || "").slice(0, 800) || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
      clearInterval(iv);
      reject(new Error("网络错误"));
    };
    xhr.onabort = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
      clearInterval(iv);
      reject(new DOMException("Aborted", "AbortError"));
    };

    xhr.send(formData);
  });
}

function showPreviewStatus(type, text) {
  const wrap = $("#preview-result-wrap");
  const el = $("#preview-status");
  if (wrap && text) wrap.hidden = false;
  if (el) {
    const err = type === "error";
    const ok = type === "ok";
    el.className =
      "preview-panel-body" +
      (err ? " preview-panel-body--error" : ok ? " preview-panel-body--ok" : "");
    el.textContent = text || "";
    el.hidden = !text;
  }
}

function closePreviewPanel() {
  const wrap = $("#preview-result-wrap");
  const el = $("#preview-status");
  if (wrap) wrap.hidden = true;
  if (el) {
    el.className = "preview-panel-body";
    el.textContent = "";
    el.hidden = true;
  }
}

function showChunkPreviewStatus(type, text) {
  const wrap = $("#chunk-preview-result-wrap");
  const el = $("#chunk-preview-status");
  if (wrap) wrap.hidden = false;
  if (el) {
    const err = type === "error";
    const ok = type === "ok";
    el.className =
      "preview-panel-body" +
      (err ? " preview-panel-body--error" : ok ? " preview-panel-body--ok" : "");
    el.textContent = text || "";
    el.hidden = !text;
  }
}

function closeChunkPreviewPanel() {
  const wrap = $("#chunk-preview-result-wrap");
  const el = $("#chunk-preview-status");
  if (wrap) wrap.hidden = true;
  if (el) {
    el.className = "preview-panel-body";
    el.textContent = "";
    el.hidden = true;
  }
}

function getStoredTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === "dark" || v === "light") return v;
  } catch (_) {}
  return "light";
}

function setStoredTheme(mode) {
  try {
    localStorage.setItem(THEME_KEY, mode);
  } catch (_) {}
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "dark") {
    root.setAttribute("data-theme", "dark");
  } else {
    root.removeAttribute("data-theme");
  }
  const btn = $("#btn-theme");
  if (!btn) return;
  const icon = btn.querySelector(".btn-theme-icon");
  const label = btn.querySelector(".btn-theme-label");
  if (mode === "dark") {
    if (icon) icon.textContent = "☀️";
    if (label) label.textContent = "浅色模式";
    btn.setAttribute("title", "切换为浅色外观");
    btn.setAttribute("aria-label", "切换为浅色外观");
  } else {
    if (icon) icon.textContent = "🌙";
    if (label) label.textContent = "深色模式";
    btn.setAttribute("title", "切换为深色外观");
    btn.setAttribute("aria-label", "切换为深色外观");
  }
}

function initTheme() {
  applyTheme(getStoredTheme());
  $("#btn-theme")?.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    setStoredTheme(next);
    applyTheme(next);
  });
}

function showToast(message, ms = 3200, variant = "default") {
  const host = $("#toast-host");
  if (!host) return;
  const el = document.createElement("div");
  let cls = "toast";
  if (variant === "success") cls += " toast-success";
  else if (variant === "warn") cls += " toast-warn";
  el.className = cls;
  el.setAttribute("role", "status");
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.3s ease";
    setTimeout(() => el.remove(), 350);
  }, ms);
}

function addFiles(fileList) {
  const arr = Array.from(fileList || []);
  const picked = arr.filter((f) => DOC_EXT.test(f.name));
  const skipped = arr.length - picked.length;
  const seen = new Set(state.files.map(fileKey));
  let added = 0;
  for (const f of picked) {
    const k = fileKey(f);
    if (seen.has(k)) continue;
    seen.add(k);
    state.files.push(f);
    added++;
  }
  renderUploadedPanel();
  if (added > 0) {
    runLocalPickProgress(added);
    showToast(`✓ 上传成功：已添加 ${added} 个文档`, 3000, "success");
  } else if (picked.length > 0 && added === 0) {
    showToast("所选文件已在列表中（未重复添加）", 2600, "warn");
  }
  if (skipped > 0) {
    showToast(`已跳过 ${skipped} 个不支持的文件（仅 PDF / docx / md / txt）`, 4500, "warn");
  }
}

function removeFile(index) {
  if (index < 0 || index >= state.files.length) return;
  state.files.splice(index, 1);
  renderUploadedPanel();
  showToast("✓ 已从列表移除", 2200, "success");
}

function removeFileByKey(k) {
  const idx = state.files.findIndex((x) => fileKey(x) === k);
  if (idx >= 0) removeFile(idx);
}

function renderUploadedPanel() {
  const countEl = $("#uploaded-count-label");
  const emptyEl = $("#uploaded-empty");
  const listEl = $("#uploaded-list");
  const n = state.files.length;
  if (countEl) countEl.textContent = `共上传了 ${n} 个文档`;
  if (!emptyEl || !listEl) return;
  if (n === 0) {
    emptyEl.hidden = false;
    listEl.hidden = true;
    listEl.innerHTML = "";
    return;
  }
  emptyEl.hidden = true;
  listEl.hidden = false;
  listEl.innerHTML = "";
  state.files.forEach((f, i) => {
    const li = document.createElement("li");
    li.className = "uploaded-row";
    const kb = f.size / 1024;
    const sizeStr =
      f.size >= 1024 * 1024 ? `${(f.size / (1024 * 1024)).toFixed(2)} MB` : `${kb.toFixed(1)} KB`;
    const nameSpan = document.createElement("span");
    nameSpan.className = "uploaded-row-name";
    nameSpan.title = f.name;
    nameSpan.textContent = f.name;
    const metaSpan = document.createElement("span");
    metaSpan.className = "uploaded-row-meta";
    metaSpan.textContent = sizeStr;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn-file-remove";
    del.textContent = "删除";
    del.setAttribute("aria-label", `删除 ${f.name}`);
    const fk = fileKey(f);
    del.addEventListener("click", () => removeFileByKey(fk));
    li.appendChild(nameSpan);
    li.appendChild(metaSpan);
    li.appendChild(del);
    listEl.appendChild(li);
  });
}

function normalizePastedKey(raw) {
  if (!raw) return "";
  return raw
    .replace(/^\uFEFF/, "")
    .replace(/\u200b/g, "")
    .split(/\r?\n/)
    .join("")
    .trim();
}

function appendLog(line) {
  const ta = $("#run-log");
  if (!ta) return;
  const t = new Date().toLocaleTimeString();
  ta.value += `[${t}] ${line}\n`;
  ta.scrollTop = ta.scrollHeight;
}

function clearLog() {
  const ta = $("#run-log");
  if (ta) ta.value = "";
}

function getParallelConcurrency() {
  const n = Number($("#parallel_concurrency")?.value) || 5;
  return Math.max(1, Math.round(n));
}

function syncParallelUi() {
  const enabled = Boolean($("#parallel_enabled")?.checked);
  const hint = $("#parallel_mode_hint");
  const settings = $("#parallel_settings");
  if (settings) settings.hidden = !enabled;
  if (hint) {
    hint.textContent = enabled
      ? `当前：多线程，并发数 ${getParallelConcurrency()}（并发过高可能报错或导致问答不完整）`
      : "当前：单线程（按顺序逐段调用，更稳定）";
  }
}

function initParallelControls() {
  const toggle = $("#parallel_enabled");
  const concurrencyInput = $("#parallel_concurrency");

  toggle?.addEventListener("change", syncParallelUi);
  concurrencyInput?.addEventListener("input", syncParallelUi);
  concurrencyInput?.addEventListener("change", () => {
    if (concurrencyInput) {
      concurrencyInput.value = String(getParallelConcurrency());
    }
    syncParallelUi();
  });

  syncParallelUi();
}

function collectOptions() {
  return {
    chunk_size: Number($("#chunk_size").value) || 3000,
    chunk_overlap: Number($("#chunk_overlap").value) || 360,
    markdown_heading_split: Boolean($("#markdown_heading_split")?.checked),
    markdown_heading_level: getMarkdownHeadingLevel(),
    loop_count: Math.max(1, Number($("#loop_count").value) || 1),
    merged_prompt_template: "",
    task_form: $("#seg_task_form")?.value || "",
    question_difficulty: $("#seg_question_difficulty")?.value || "",
    answer_length: $("#seg_answer_length")?.value || "",
    language_style: $("#seg_language_style")?.value || "",
    quantity_strategy: $("#seg_quantity_strategy")?.value || "",
    content_coverage: $("#seg_content_coverage")?.value || "",
    models: collectModels(),
    temperature: Number($("#temperature").value) || 0.35,
    max_tokens: Math.max(256, Number($("#max_tokens")?.value) || 8192),
    max_retries: Math.max(0, Math.min(10, Number($("#max_retries")?.value) || 3)),
    parallel_enabled: Boolean($("#parallel_enabled")?.checked),
    parallel_concurrency: getParallelConcurrency(),
  };
}

function showStatus(el, type, text) {
  el.className = "status " + (type || "");
  el.textContent = text || "";
  el.hidden = !text;
}

const QA_DISPLAY_LIMIT = 100;

let lastItems = [];

function collectChunkPreviewOptions() {
  return {
    chunk_size: Number($("#chunk_size").value) || 3000,
    chunk_overlap: Number($("#chunk_overlap").value) || 360,
    markdown_heading_split: Boolean($("#markdown_heading_split")?.checked),
    markdown_heading_level: getMarkdownHeadingLevel(),
  };
}

function renderResults(items) {
  lastItems = items || [];
  const tbody = $("#result-body");
  const thead = $("#result-head");
  const heading = $("#result-heading");
  tbody.innerHTML = "";
  thead.innerHTML = "";
  if (!lastItems.length) {
    $("#result-section").hidden = true;
    $("#btn-download-json").disabled = true;
    $("#btn-download-csv").disabled = true;
    $("#btn-download-md").disabled = true;
    if (heading) heading.textContent = "结果";
    return;
  }
  $("#result-section").hidden = false;
  const total = lastItems.length;
  if (heading) {
    heading.textContent =
      total > QA_DISPLAY_LIMIT
        ? `结果（页面展示前 ${QA_DISPLAY_LIMIT} 条，共 ${total} 条）`
        : `结果（${total} 条）`;
  }
  $("#btn-download-json").disabled = false;
  $("#btn-download-csv").disabled = false;
  $("#btn-download-md").disabled = false;

  const displayRows = lastItems.slice(0, QA_DISPLAY_LIMIT);
  thead.innerHTML = `<tr><th>来源</th><th>块</th><th>轮</th><th>模型</th><th>#</th><th>问题</th><th>答案</th></tr>`;
  displayRows.forEach((row) => {
    const tr = document.createElement("tr");
    const m = row.model_label || row.model_id || "";
    tr.innerHTML = `
        <td>${escapeHtml(row.source)}</td>
        <td>${row.chunk_index}</td>
        <td>${row.round}</td>
        <td>${escapeHtml(m)}</td>
        <td>${row.pair_index ?? ""}</td>
        <td>${escapeHtml(row.question)}</td>
        <td>${escapeHtml(row.answer)}</td>`;
    tbody.appendChild(tr);
  });
}

function downloadJson() {
  if (!lastItems.length) return;
  const blob = new Blob([JSON.stringify({ items: lastItems }, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  triggerDownload(blob, "qa-export.json");
}

function downloadCsv() {
  if (!lastItems.length) return;
  const headers = [
    "source",
    "chunk_index",
    "round",
    "model_label",
    "model_id",
    "pair_index",
    "question",
    "answer",
  ];
  const lines = [headers.join(",")];
  for (const row of lastItems) {
    const r = {
      ...row,
      model_label: row.model_label ?? "",
      model_id: row.model_id ?? "",
    };
    lines.push(headers.map((h) => csvEscape(String(r[h] ?? ""))).join(","));
  }
  const blob = new Blob(["\ufeff" + lines.join("\n")], {
    type: "text/csv;charset=utf-8",
  });
  triggerDownload(blob, "qa-export.csv");
}

function downloadMd() {
  if (!lastItems.length) return;
  let md = "# 问答导出\n\n";
  md += `_导出时间 ${new Date().toISOString()} · 多模型（每行一条）_\n\n`;
  lastItems.forEach((row, i) => {
    const m = row.model_label || row.model_id || "";
    md += `## ${i + 1}. ${row.source} · 块${row.chunk_index} · 轮${row.round} · ${m}\n\n`;
    md += `**问：** ${row.question}\n\n**答：** ${row.answer}\n\n---\n\n`;
  });
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  triggerDownload(blob, "qa-export.md");
}

function csvEscape(v) {
  if (/[",\n\r]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
  return v;
}

function triggerDownload(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function setGenProgress(visible, pct, label) {
  const wrap = $("#gen-progress");
  const bar = $("#gen-progress-bar");
  const lab = $("#gen-progress-label");
  if (!wrap || !bar || !lab) return;
  wrap.hidden = !visible;
  if (pct != null) bar.style.width = Math.min(100, Math.max(0, pct)) + "%";
  if (label != null) lab.textContent = label;
}

async function checkServer() {
  const banner = $("#server-banner");
  if (!banner) return;
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    if (!r.ok) throw new Error();
    banner.hidden = true;
  } catch {
    banner.hidden = false;
  }
}

async function loadDefaults() {
  try {
    const r = await fetch("/api/defaults");
    const d = await r.json();
    const segMap = [
      ["task_form", "seg_task_form"],
      ["question_difficulty", "seg_question_difficulty"],
      ["answer_length", "seg_answer_length"],
      ["language_style", "seg_language_style"],
      ["quantity_strategy", "seg_quantity_strategy"],
      ["content_coverage", "seg_content_coverage"],
    ];
    for (const [key, id] of segMap) {
      const el = $(`#${id}`);
      if (el && typeof d[key] === "string") el.value = d[key];
    }
    const ro = $("#merged_prompt_readonly");
    if (ro && typeof d.merged_prompt_template === "string") {
      ro.textContent = d.merged_prompt_template;
    }
  } catch (_) {}
}

function previewMaxCharsForRequest() {
  const fullSel = $("#preview_full");
  const v = fullSel && fullSel.value !== "0" ? Number(fullSel.value) : 0;
  if (v > 0) return Math.min(v, 500000);
  const n = Number($("#preview_max_chars")?.value) || 50000;
  return Math.min(Math.max(1000, n), 500000);
}

async function previewText() {
  closePreviewPanel();
  closeChunkPreviewPanel();
  const status = $("#preview-status");
  if (status) {
    status.textContent = "";
    status.hidden = true;
  }
  const fd = new FormData();
  fd.append("max_chars", String(previewMaxCharsForRequest()));
  state.files.forEach((f) => fd.append("files", f));

  try {
    const data = await postFormDataJsonWithProgress("/api/preview-text", fd);
    if (!data.ok) {
      setDocumentTransferPanel(true, {
        title: "预览失败",
        hint: data.error || "未知错误",
        pct: 100,
        badge: "失败",
        badgeClass: "is-err",
      });
      setTimeout(() => setDocumentTransferPanel(false), 2500);
      showPreviewStatus("error", data.error || "预览失败");
      return;
    }
    setDocumentTransferPanel(true, {
      title: "预览",
      hint: "文本提取完成",
      pct: 100,
      badge: "成功",
      badgeClass: "is-done",
    });
    setTimeout(() => setDocumentTransferPanel(false), 1600);

    if (data.warnings && data.warnings.length) {
      appendLog("预览警告：\n" + data.warnings.join("\n"));
    }
    const parts = data.sources.map((s) => {
      const truncated = s.truncated === true || (typeof s.chars === "number" && s.preview && s.chars > s.preview.length);
      const head = truncated
        ? `【${s.source}】全文共 ${s.chars} 字（以下为前 ${(s.preview || "").length} 字）`
        : `【${s.source}】全文共 ${s.chars} 字（已完整显示）`;
      return `${head}\n${s.preview || ""}\n`;
    });
    const extractHead =
      "【此处为已上传文件按上方字数上限截断后的预览，用于检查解析/编码是否正常；不是切片后送给大模型的正文。要看模型实际读到的切块，请用「切片与调用」里的单次阅读预览。】\n\n";
    const modeLine = "【来源：当前已上传的本地文件】\n\n";
    showPreviewStatus("ok", extractHead + modeLine + parts.join("\n---\n"));
    $("#preview-result-wrap")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    showToast("✓ 预览完成", 2200, "success");
  } catch (e) {
    const msg = e && e.name === "AbortError" ? "已取消" : String(e);
    setDocumentTransferPanel(true, {
      title: "预览失败",
      hint: msg,
      pct: 100,
      badge: "失败",
      badgeClass: "is-err",
    });
    setTimeout(() => setDocumentTransferPanel(false), 2800);
    showPreviewStatus("error", msg);
  }
}

async function previewChunks() {
  showChunkPreviewStatus("ok", "正在按当前参数请求切片预览…");
  const fd = new FormData();
  fd.append("options_json", JSON.stringify(collectChunkPreviewOptions()));
  state.files.forEach((f) => fd.append("files", f));

  try {
    const data = await postFormDataJsonWithProgress("/api/preview-chunks", fd);
    if (!data.ok) {
      setDocumentTransferPanel(true, {
        title: "单次阅读预览失败",
        hint: data.error || "未知错误",
        pct: 100,
        badge: "失败",
        badgeClass: "is-err",
      });
      setTimeout(() => setDocumentTransferPanel(false), 2500);
      showChunkPreviewStatus("error", data.error || "预览失败");
      return;
    }
    setDocumentTransferPanel(true, {
      title: "单次阅读预览",
      hint: "切片完成",
      pct: 100,
      badge: "成功",
      badgeClass: "is-done",
    });
    setTimeout(() => setDocumentTransferPanel(false), 1600);

    if (data.warnings && data.warnings.length) {
      appendLog("单次阅读预览警告：\n" + data.warnings.join("\n"));
    }
    if (data.logs && data.logs.length) {
      appendLog("单次阅读预览说明：\n" + data.logs.join("\n"));
    }
    const total = data.total_chunks ?? (data.chunks || []).length;
    const lim = data.preview_limit ?? 6;
    const chunks = data.chunks || [];
    const nshow = Math.min(lim, chunks.length);
    const head = `【全文共 ${total} 块。以下第 1～${nshow} 块正文，与「开始生成」时按同一顺序送入大模型的前 ${nshow} 段「文档片段」内容一致（同一提取结果与「切片与调用」参数；接口与生成共用 sources_to_chunks；此处仅展示前 ${lim} 块）。】\n\n`;
    const parts = chunks.map((c, i) => {
      const src = c.source || "";
      const idx = c.chunk_index != null ? c.chunk_index : i;
      const n = typeof c.chars === "number" ? c.chars : (c.text || "").length;
      return `──────── 第 ${i + 1} 块 · 来源 ${src} · 片段序号 ${idx} · 约 ${n} 字 ────────\n${c.text || ""}`;
    });
    const modeLine = "【来源：当前已上传的本地文件】\n\n";
    showChunkPreviewStatus("ok", modeLine + head + parts.join("\n\n"));
    $("#chunk-preview-result-wrap")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    showToast("✓ 已在本区下方展示切片预览", 2200, "success");
  } catch (e) {
    const msg = e && e.name === "AbortError" ? "已取消" : String(e);
    setDocumentTransferPanel(true, {
      title: "单次阅读预览失败",
      hint: msg,
      pct: 100,
      badge: "失败",
      badgeClass: "is-err",
    });
    setTimeout(() => setDocumentTransferPanel(false), 2800);
    showChunkPreviewStatus("error", msg);
  }
}

async function generate() {
  const status = $("#gen-status");
  const btn = $("#btn-generate");
  const btnStop = $("#btn-stop-generate");
  state.abortController = null;
  showStatus(status, "", "");
  renderResults([]);
  setGenProgress(false, 0, "");
  setDocumentTransferPanel(false);
  clearLog();

  const opts = collectOptions();
  if (!opts.models.length) {
    showStatus(status, "error", "请至少配置一个模型。");
    return;
  }
  const m0 = opts.models[0];
  const rootsBase = (m0.base_url || "").trim();
  for (let i = 0; i < opts.models.length; i++) {
    const m = opts.models[i];
    const effKey = (m.api_key || "").trim() || (i > 0 ? (m0.api_key || "").trim() : "");
    const effModel = (m.model || "").trim() || (i > 0 ? (m0.model || "").trim() : "");
    const effBase = (m.base_url || "").trim() || (i > 0 ? rootsBase : "");
    if (!effKey) {
      showStatus(
        status,
        "error",
        i === 0
          ? "第 1 个模型请填写 API Key。"
          : `第 ${i + 1} 个模型请填写 API Key，或确保第 1 条已填写以供沿用。`,
      );
      return;
    }
    if (!effModel) {
      showStatus(
        status,
        "error",
        i === 0
          ? "第 1 个模型请填写模型 ID。"
          : `第 ${i + 1} 个模型请填写模型 ID，或确保第 1 条已填写以供沿用。`,
      );
      return;
    }
    if (m.provider === "openai_compatible") {
      if (!effBase) {
        showStatus(status, "error", `第 ${i + 1} 个模型为兼容模式时需填写 Base URL（或与第一条共用）。`);
        return;
      }
    }
  }
  if (!state.files.length) {
    showStatus(status, "error", "请至少上传一个文档。");
    return;
  }

  const fd = new FormData();
  fd.append("options_json", JSON.stringify(opts));
  state.files.forEach((f) => fd.append("files", f));

  btn.disabled = true;
  if (btnStop) btnStop.disabled = false;
  setGenProgress(true, 2, "正在上传并连接服务器…");
  appendLog("任务已提交");
  showStatus(status, "ok", "生成进行中；可随时点「停止生成」。失败片段会跳过并保留已有结果。");

  let finalDone = null;
  let streamTotal = 1;
  let requestedParallel = false;
  state.abortController = new AbortController();
  const signal = state.abortController.signal;

  const onStreamEvent = (ev) => {
    if (ev.type === "start") {
      streamTotal = Math.max(1, ev.total_steps || 1);
      const mc = ev.model_count || 1;
      requestedParallel = Boolean(ev.parallel_requested);
      const modeText = ev.parallel_enabled
        ? `多线程并行（ThreadPoolExecutor，并发 ${ev.parallel_concurrency || 1}）`
        : requestedParallel
          ? "单线程串行（已开多线程选项但未满足并行条件）"
          : "单线程串行";
      appendLog(
        `开始：共 ${ev.total_steps} 次调用（${modeText}）；顺序为按模型从上到下，每个模型先跑完 ${ev.chunk_count} 片段 × ${ev.loop_count} 轮，再换下一模型（${mc} 个模型）`
      );
      if (requestedParallel && ev.parallel_concurrency_requested) {
        appendLog(`[info] 用户设置并发数：${ev.parallel_concurrency_requested}`);
      }
      setGenProgress(
        true,
        5,
        `共 ${ev.total_steps} 步 · ${modeText}（每模型 ${ev.chunk_count} 片段 × ${ev.loop_count} 轮）`
      );
    } else if (ev.type === "progress") {
      streamTotal = Math.max(streamTotal, ev.total_steps || 1);
      const pct = (ev.step / streamTotal) * 100;
      setGenProgress(true, pct, `第 ${ev.step} / ${streamTotal} 步\n${ev.detail}`);
    } else if (ev.type === "log") {
      appendLog(`[${ev.level || "info"}] ${ev.message}`);
    } else if (ev.type === "retry") {
      appendLog(`重试 ${ev.attempt}：${ev.message}`);
    } else if (ev.type === "step_ok") {
      const pct = (ev.step / streamTotal) * 100;
      setGenProgress(
        true,
        pct,
        `第 ${ev.step} 步完成：新增 ${ev.added} 条，累计 ${ev.total_qa} 条`
      );
    } else if (ev.type === "warn") {
      appendLog(`警告：${ev.message}`);
      setGenProgress(true, null, `警告：${ev.message}`);
    } else if (ev.type === "done") {
      finalDone = ev;
    }
  };

  try {
    await streamGenerateNdjsonWithXhr("/api/generate-stream", fd, onStreamEvent, signal);

    if (!finalDone) {
      showStatus(status, "error", "未收到完成数据（连接可能中断）。");
      setGenProgress(false, 0, "");
      appendLog("流结束但未收到 done 事件");
      return;
    }

    setGenProgress(true, 100, "已完成");
    if (!finalDone.ok) {
      let msg = finalDone.message || "未能成功完成全部步骤。";
      if (finalDone.errors && finalDone.errors.length) {
        msg += "\n\n" + finalDone.errors.slice(0, 12).join("\n");
        if (finalDone.errors.length > 12) msg += "\n…";
      }
      showStatus(status, finalDone.count > 0 ? "ok" : "error", msg);
      renderResults(finalDone.items || []);
      appendLog(`结束（部分失败）：累计 ${finalDone.count} 条`);
      if (finalDone.aborted) appendLog("流程因密钥错误等原因中止");
    } else {
      let msg = `完成：共 ${finalDone.count} 条问答。`;
      if (finalDone.errors && finalDone.errors.length) {
        msg += `\n\n有 ${finalDone.errors.length} 条警告/历史信息。`;
      }
      showStatus(status, "ok", msg);
      renderResults(finalDone.items || []);
      appendLog(`成功结束：${finalDone.count} 条`);
    }
    showToast(`✓ 生成结束：${finalDone.count} 条`, 3200, "success");
  } catch (e) {
    if (e && e.name === "AbortError") {
      showStatus(
        status,
        "ok",
        finalDone
          ? `已停止生成。已保留当前 ${finalDone.count} 条结果，可导出或更换文件后重试。`
          : "已停止生成。尚未收到有效结果，可更换文件或参数后重试。"
      );
      appendLog("用户点击了「停止生成」");
      if (finalDone && (finalDone.items || []).length) {
        renderResults(finalDone.items);
        showToast("已停止：已保留部分结果", 2800, "warn");
      } else {
        showToast("已停止生成", 2200, "warn");
      }
    } else {
      const err = String(e);
      showStatus(
        status,
        "error",
        err.includes("Failed to fetch") || err.includes("NetworkError") || err.includes("Load failed")
          ? "无法连接服务器。请确认 uvicorn 已启动且非 file:// 打开页面。"
          : err
      );
      appendLog(`异常：${err}`);
      setGenProgress(false, 0, "");
    }
  } finally {
    state.abortController = null;
    btn.disabled = false;
    if (btnStop) btnStop.disabled = true;
    setDocumentTransferPanel(false);
    setTimeout(() => setGenProgress(false, 0, ""), 800);
  }
}

function initParticles() {
  const c = document.getElementById("particles-canvas");
  if (!c || !c.getContext) return;
  const ctx = c.getContext("2d");
  if (!ctx) return;
  const parts = [];
  const n = 56;

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    c.width = Math.floor(innerWidth * dpr);
    c.height = Math.floor(innerHeight * dpr);
    c.style.width = `${innerWidth}px`;
    c.style.height = `${innerHeight}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function spawn() {
    parts.length = 0;
    for (let i = 0; i < n; i++) {
      parts.push({
        x: Math.random() * innerWidth,
        y: Math.random() * innerHeight,
        r: 0.4 + Math.random() * 1.6,
        vx: (Math.random() - 0.5) * 0.25,
        vy: (Math.random() - 0.5) * 0.25,
        o: 0.15 + Math.random() * 0.35,
      });
    }
  }

  resize();
  spawn();
  window.addEventListener("resize", () => {
    resize();
    spawn();
  });

  function tick() {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    const col = dark ? "160, 190, 255" : "0, 113, 227";
    for (const p of parts) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < 0) p.x = innerWidth;
      if (p.x > innerWidth) p.x = 0;
      if (p.y < 0) p.y = innerHeight;
      if (p.y > innerHeight) p.y = 0;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${col},${p.o})`;
      ctx.fill();
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function init() {
  initTheme();
  initParticles();

  const dz = $("#dropzone");
  const input = $("#file-input");

  dz.addEventListener("click", () => input.click());
  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("drag");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  input.addEventListener("change", () => {
    addFiles(input.files);
    input.value = "";
  });

  $("#btn-preview").addEventListener("click", previewText);
  $("#btn-preview-chunks")?.addEventListener("click", previewChunks);
  $("#btn-chunk-preview-close")?.addEventListener("click", closeChunkPreviewPanel);
  $("#btn-preview-close")?.addEventListener("click", closePreviewPanel);
  $("#btn-generate").addEventListener("click", generate);
  $("#btn-stop-generate")?.addEventListener("click", () => state.abortController?.abort());
  $("#btn-download-json").addEventListener("click", downloadJson);
  $("#btn-download-csv").addEventListener("click", downloadCsv);
  $("#btn-download-md").addEventListener("click", downloadMd);
  $("#btn-clear-log").addEventListener("click", clearLog);

  checkServer();
  initModelsList();
  initParallelControls();
  loadDefaults();
  renderUploadedPanel();
}

document.addEventListener("DOMContentLoaded", init);
