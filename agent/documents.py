"""用户资料库：上传或指定文件夹 → 提取文字层 → 关键词检索（可选 LLM 理解）。

支持格式：PDF、TXT、MD、DOCX、XLSX/XLSM、PPTX、CSV、JSON、HTML/HTM、XML、LOG，
以及常见代码文件（py/js/ts/java/c/cpp/h/sh/bat/sql/yml/yaml/ini/toml/conf）。
图片（png/jpg 等）需要 OCR/视觉模型，当前返回明确提示，不静默跳过。

资料一律标为 pending／unreviewed：来源、版本现行性与适用性未核实，不进入正式规范库、不参与判定。
扫描件（无文字层）如实标记为“需 OCR”，当前不可检索。
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from domain.contracts import new_id, now_utc

STD_RE = re.compile(
    r"((?:GB|GB/T|GB_T|GBT|JGJ|JGJ/T|JGJ_T|JG/T|JG_T|JGT|JG|JC/T|JC_T|DB\d+/T|DB\d+_T|DGTJ|DG-TJ|T_CECS|T/CECS)"
    r"[\s\-_／/]*[\d.]+(?:[\-—–]\d{4})?)", re.IGNORECASE)

# 支持直接读取的纯文本/结构化/代码格式
TEXT_EXTS = (".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".log",
             ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".sh", ".bat", ".sql",
             ".yml", ".yaml", ".ini", ".toml", ".conf")
# Office 文档
OFFICE_EXTS = (".docx", ".xlsx", ".xlsm", ".pptx")
# 图片（走本地视觉模型理解，需 Ollama + llava/qwen2.5-vl）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".bmp": "image/bmp", ".gif": "image/gif", ".webp": "image/webp"}
SUPPORTED = (".pdf",) + TEXT_EXTS + OFFICE_EXTS + IMAGE_EXTS


def _title(stem: str) -> tuple[str, str | None]:
    s = re.sub(r"^\d+_", "", stem).strip()
    m = STD_RE.search(s)
    num = m.group(1).replace("_", "/").strip() if m else None
    title = (s[:m.start()] + s[m.end():]).strip(" _-（）()") if m else s
    return title or s, num


def _chunk(text: str, size: int = 1500) -> list[str]:
    """把长文本切成接近 size 的“页”。"""
    chunks = [c for c in re.split(r"\n\s*\n", text) if c.strip()]
    pages, cur = [], ""
    for c in chunks:
        cur += c + "\n\n"
        if len(cur) > size:
            pages.append(cur)
            cur = ""
    if cur.strip():
        pages.append(cur)
    return pages or ([text] if text.strip() else [])


def _extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"【工作表：{ws.title}】")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                texts.append(shape.text.strip())
        if texts:
            parts.append(f"【第 {i} 页】" + "\n".join(texts))
    return "\n\n".join(parts)


def extract_pages(filename: str, data: bytes) -> tuple[list[str], int, str | None]:
    """返回（每页文字, 总页数, 错误）。"""
    low = filename.lower()
    if low.endswith(".pdf"):
        try:
            import fitz
            d = fitz.open(stream=data, filetype="pdf")
            return [d[i].get_text().strip() for i in range(d.page_count)], d.page_count, None
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"PDF 无法读取（{type(exc).__name__}）"
    if low.endswith(TEXT_EXTS):
        try:
            text = data.decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"文本无法读取（{type(exc).__name__}）"
        pages = _chunk(text)
        return pages, len(pages), None
    if low.endswith(".docx"):
        try:
            text = _extract_docx(data)
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"Word 文档无法读取（{type(exc).__name__}）"
        pages = _chunk(text)
        return pages, len(pages), None
    if low.endswith((".xlsx", ".xlsm")):
        try:
            text = _extract_xlsx(data)
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"Excel 无法读取（{type(exc).__name__}）"
        pages = _chunk(text)
        return pages, len(pages), None
    if low.endswith(".pptx"):
        try:
            text = _extract_pptx(data)
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"PPT 无法读取（{type(exc).__name__}）"
        pages = _chunk(text)
        return pages, len(pages), None
    if low.endswith(IMAGE_EXTS):
        try:
            from agent import extraction
            suffix = Path(filename).suffix.lower()
            text = extraction.describe_image(data, IMAGE_MIME.get(suffix, "image/png"))
            pages = _chunk(text)
            return pages, len(pages), None
        except Exception as exc:  # noqa: BLE001
            return [], 0, (f"图片需要本地视觉模型（Ollama + llava/qwen2.5-vl），当前未配置或不可用（{type(exc).__name__}）。"
                           "请先安装 Ollama 并执行 `ollama pull llava`，或另行人工查看。")
    return [], 0, "不支持的资料格式（支持 PDF、TXT、MD、DOCX、XLSX、PPTX、CSV、JSON、HTML、XML、常见代码文件及图片）"


def add_document(ws, filename: str, data: bytes, *, origin: str = "上传") -> dict:
    pages, n, err = extract_pages(filename, data)
    if err:
        return {"ok": False, "filename": filename, "error": err}
    sha = hashlib.sha256(data).hexdigest()
    title, num = _title(Path(filename).stem)
    doc_id = "DOC-" + sha[:10]
    good = [(i + 1, re.sub(r"[ \t]+", " ", p)) for i, p in enumerate(pages) if p.strip()]
    doc = {"doc_id": doc_id, "filename": filename, "file": filename, "title": title,
           "standard_number_from_filename": num, "sha256": sha, "page_count": n, "text_pages": len(good),
           "needs_ocr": len(good) < max(1, n // 2), "source_status": "pending", "source_type": "public_document",
           "review_status": "unreviewed", "effect_status": "unknown", "origin": origin, "created_at": now_utc(),
           "notes": "用户提供的资料；来源合法性、版本现行性与条款适用性未审核，仅供人工查阅"}
    saved = ws.add_document(doc, [{"doc_id": doc_id, "page": p, "text": t} for p, t in good])
    return {"ok": True, "doc": saved, "duplicate": bool(saved.get("duplicate"))}


def add_folder(ws, folder: str, *, skip_dirs=("排除的候选文件",)) -> dict:
    root = Path(folder).expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"文件夹不存在：{folder}"}
    if (root / "检查清单" / "逐文件清单.csv").exists() or any(
            (parent / "检查清单" / "逐文件清单.csv").exists() for parent in root.parents):
        return {"ok": False, "error": "这是OCR成果包或其子目录，请使用管理规范库中的专用导入，避免报告、缓存及重复文本进入资料库。"}
    added, dup, failed = 0, 0, []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in SUPPORTED or any(s in f.parts for s in skip_dirs):
            continue
        r = add_document(ws, f.relative_to(root).as_posix(), f.read_bytes(), origin=f"文件夹 {root.name}")
        if not r["ok"]:
            failed.append(f"{f.name}：{r['error']}")
        elif r["duplicate"]:
            dup += 1
        else:
            added += 1
    return {"ok": True, "added": added, "duplicates": dup, "failed": failed}


def new_doc_id() -> str:
    return new_id("DOC")


def answer_document_query(question: str, candidates: list[dict], model_mode: str = "auto") -> str | None:
    """用模型基于检索到的资料片段生成回答（“理解”层）。

    模型不可用、未配置或调用失败时返回 None，由调用方回退为只展示检索片段。
    严格约束：只依据给定片段回答，片段没有的不编造，明确说“资料中未提及”。
    """
    if model_mode == "fallback":
        return None
    try:
        from agent import extraction
        if not extraction.model_available():
            return None
    except Exception:  # noqa: BLE001
        return None

    excerpts = []
    for c in candidates[:6]:
        title = c.get("title") or c.get("source_ref") or "资料"
        text = (c.get("exact_text") or "").strip()
        if text:
            excerpts.append(f"[{title}] {text}")
    if not excerpts:
        return None

    system = ("你是资料阅读助手。只依据下面给出的资料片段回答用户问题，简明扼要、直说结论。"
              "片段中没有的内容不要编造，明确回答“资料中未提及”。不要声称资料当前有效，不给合规或安全结论。")
    user = f"用户问题：{question}\n\n资料片段：\n" + "\n---\n".join(excerpts)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    try:
        out = extraction.call_model_json(system, user, schema, max_tokens=800)
        ans = (out.get("answer") or "").strip()
        return ans or None
    except Exception:  # noqa: BLE001
        return None
