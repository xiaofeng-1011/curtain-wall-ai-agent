"""用户资料库：上传或指定文件夹 → 提取文字层 → 关键词检索。

资料一律标为 pending／unreviewed：来源、版本现行性与适用性未核实，不进入正式规范库、不参与判定。
扫描件（无文字层）如实标记为“需 OCR”，当前不可检索。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from domain.contracts import new_id, now_utc

STD_RE = re.compile(
    r"((?:GB|GB/T|GB_T|GBT|JGJ|JGJ/T|JGJ_T|JG/T|JG_T|JGT|JG|JC/T|JC_T|DB\d+/T|DB\d+_T|DGTJ|DG-TJ|T_CECS|T/CECS)"
    r"[\s\-_／/]*[\d.]+(?:[\-—–]\d{4})?)", re.IGNORECASE)
SUPPORTED = (".pdf", ".txt", ".md")


def _title(stem: str) -> tuple[str, str | None]:
    s = re.sub(r"^\d+_", "", stem).strip()
    m = STD_RE.search(s)
    num = m.group(1).replace("_", "/").strip() if m else None
    title = (s[:m.start()] + s[m.end():]).strip(" _-（）()") if m else s
    return title or s, num


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
    if low.endswith((".txt", ".md")):
        text = data.decode("utf-8-sig", errors="replace")
        chunks = [c for c in re.split(r"\n\s*\n", text) if c.strip()]
        pages, cur = [], ""
        for c in chunks:
            cur += c + "\n\n"
            if len(cur) > 1500:
                pages.append(cur)
                cur = ""
        if cur.strip():
            pages.append(cur)
        return pages, len(pages), None
    if low.endswith((".doc", ".docx")):
        return [], 0, "Word 文档检索尚未支持（计划中），可另存为 PDF 或 TXT 后上传"
    return [], 0, "不支持的资料格式（支持 PDF、TXT、MD）"


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
