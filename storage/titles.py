"""对话标题只取自首条输入，与工程项目名称相互独立。"""
import re
from pathlib import Path


def auto_title(text: str = "", filenames: list[str] | None = None) -> str:
    if text.strip():
        if "\t" in text and "\n" in text:
            return "方案表格导入"
        value = " ".join(text.split()).removeprefix("请").strip(" ，,。.!！?？")
        return value[:28].rstrip("，,。.!！?？") or "新对话"
    if filenames:
        stem = re.sub(r"[_\s]+", " ", Path(filenames[0]).stem).strip() or "文件"
        suffix = "等文件" if len(filenames) > 1 else ""
        purpose = "导入" if Path(filenames[0]).suffix.lower() in (".xlsx", ".xlsm", ".csv", ".xls") else "查阅"
        return f"{stem[:20]}{suffix} · {purpose}"
    return "新对话"


def backfill_title(conv: dict) -> bool:
    if conv.get("title_source") == "manual" or conv.get("title") not in (None, "", "新对话"):
        return False
    for message in conv.get("messages", []):
        if message.get("role") != "user":
            continue
        names = message.get("filenames") or []
        if not names and (message.get("note") or "").startswith("附件："):
            names = message["note"][3:].split("、")
        title = auto_title(message.get("text") or "", names)
        if title != "新对话":
            conv.update(title=title, title_source="auto")
            return True
    return False
