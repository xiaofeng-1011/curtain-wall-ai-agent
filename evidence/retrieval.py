"""TL13 retrieve_evidence：元数据过滤 + 关键词检索（受约束 RAG 的检索部分）。

集合：
- demo：演示规则库（synthetic）
- formal：正式规范／企业规则库（P0 为空，返回覆盖缺口）
- reference：参考资料库（未审核，来自本地 PDF 文字层），只供人工查阅定位，不参与判定，不计入正式覆盖

必查引用（pinned）精确获取，不受 top_k 截断；关键词只影响补充结果排序。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from domain.contracts import Code, Issue, ToolResponse, now_utc, sha256_of

REFERENCE_INDEX = Path(__file__).resolve().parents[1] / "data" / "reference" / "reference_index.json"
STOP = set("的了和与及或在是对按为中时其应可以不得进行并且一个关于什么哪些多少吗呢请问有")
STOP_BIGRAMS = {"要求", "规定", "是否", "怎么", "如何", "什么", "哪些", "有关", "相关", "查找", "查询", "资料", "规范"}


@lru_cache(maxsize=2)
def load_reference_index(path: str = str(REFERENCE_INDEX)) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    idx = json.loads(p.read_text(encoding="utf-8"))
    idx["_docs"] = {d["doc_id"]: d for d in idx["documents"]}
    return idx


def tokenize(q: str) -> list[str]:
    """中文按 2 字片段切分，英文数字按词；去掉停用字。"""
    q = q.lower()
    toks: list[str] = []
    for w in re.findall(r"[a-z0-9.\-/]+", q):
        if len(w) >= 2:
            toks.append(w)
    for seg in re.findall(r"[一-鿿]+", q):
        seg = "".join(ch for ch in seg if ch not in STOP)
        if len(seg) == 1:
            continue
        toks += [seg[i:i + 2] for i in range(len(seg) - 1) if seg[i:i + 2] not in STOP_BIGRAMS]
    return list(dict.fromkeys(toks))


def _score(text: str, toks: list[str]) -> tuple[int, int]:
    t = text.lower()
    hits = [k for k in toks if k in t]
    return len(hits), sum(t.count(k) for k in hits)


def _snippet(text: str, toks: list[str], width: int = 160) -> str:
    t = text.lower()
    pos = min((t.find(k) for k in toks if k in t), default=0)
    start = max(0, pos - width // 3)
    s = text[start:start + width].replace("\n", " ")
    return ("…" if start > 0 else "") + s + ("…" if start + width < len(text) else "")


def _rule_candidate(rule: dict, provenance: dict, pinned: bool, score=(0, 0)) -> dict:
    prov = provenance.get(rule.get("provenance_id")) or {}
    is_demo = rule.get("evidence_type") == "demo_rule"
    return {
        "evidence_id": rule["rule_id"], "evidence_type": rule.get("evidence_type"),
        "collection": "demo" if is_demo else "formal", "title": rule.get("title"),
        "exact_text": rule.get("exact_text"), "source_ref": rule.get("source_ref"),
        "location": rule.get("location"),
        "standard_name": rule.get("standard_name"), "standard_number": rule.get("standard_number"),
        "clause_number": rule.get("clause_number"), "effect_status": rule.get("effect_status"),
        "applicability": rule.get("applicability"), "source_status": prov.get("source_status", "pending"),
        "review_status": prov.get("review_status"), "pinned": pinned, "score": list(score),
        "qualification": "演示规则：只验证流程，不是正式规范依据" if is_demo else "需按资格检查确定是否可用于判定",
        "usable_for_determination": False if not is_demo else "demo_only",
    }


def retrieve_evidence(package: dict, query: dict, pinned_rule_ids: list[str] | None = None, top_k: int = 5,
                      reference_index: dict | None = None) -> ToolResponse:
    """query: {query_text, collection(demo/formal/reference/all), curtain_wall_type, design_stage, region, check_date}"""
    started = now_utc()
    text = (query.get("query_text") or "").strip()
    coll = query.get("collection") or "all"
    issues: list[Issue] = []
    if not text and not pinned_rule_ids:
        issues.append(Issue(Code.FIELD_MISSING, "查询文本为空", "query_text", "query"))
        return ToolResponse("TL13", "blocked", None, issues, {"started_at": started, "ended_at": now_utc()})
    toks = tokenize(text)
    provenance = package["provenance"]
    pinned = [_rule_candidate(package["rules"][r], provenance, True)
              for r in (pinned_rule_ids or []) if r in package["rules"]]
    missing_pins = [r for r in (pinned_rule_ids or []) if r not in package["rules"]]
    for r in missing_pins:
        issues.append(Issue(Code.EVIDENCE_NOT_FOUND, f"必查规则 {r} 不在库中", None, "query", blocking=False))
    pinned_ids = {c["evidence_id"] for c in pinned}

    rule_hits = []
    for rid, rule in package["rules"].items():
        c = "demo" if rule.get("evidence_type") == "demo_rule" else "formal"
        if coll not in ("all", c) or rid in pinned_ids:
            continue
        app = rule.get("applicability") or {}
        if query.get("curtain_wall_type") and app.get("curtain_wall_type") and \
                query["curtain_wall_type"] not in app["curtain_wall_type"]:
            continue
        sc = _score(" ".join(str(rule.get(k) or "") for k in ("title", "exact_text")), toks)
        if sc[0] > 0:
            rule_hits.append(_rule_candidate(rule, provenance, False, sc))
    rule_hits.sort(key=lambda x: (-x["score"][0], -x["score"][1], x["evidence_id"]))

    ref_hits = []
    ref_meta = None
    if coll in ("all", "reference"):
        idx = reference_index if reference_index is not None else load_reference_index()
        if idx is None:
            issues.append(Issue(Code.EVIDENCE_NOT_FOUND, "参考资料未登记，请在资料库上传；OCR成果请从管理规范库导入。",
                                None, "query", blocking=False))
        elif toks:
            ref_meta = {"index_version": idx.get("index_version"), "built_at": idx.get("built_at"),
                        "documents": len(idx["documents"]),
                        "needs_ocr": sum(1 for d in idx["documents"] if d.get("needs_ocr"))}
            docs = idx.get("_docs") or {d["doc_id"]: d for d in idx["documents"]}
            scored = []
            min_hits = 1 if len(toks) <= 2 else min(3, max(2, len(toks) // 3))
            for pg in idx["pages"]:
                sc = _score(pg["text"], toks)
                if sc[0] >= min_hits:
                    scored.append((sc, pg))
            scored.sort(key=lambda x: (-x[0][0], -x[0][1]))
            per_doc: dict = {}
            for sc, pg in scored:
                if per_doc.get(pg["doc_id"], 0) >= 2:
                    continue
                per_doc[pg["doc_id"]] = per_doc.get(pg["doc_id"], 0) + 1
                d = docs[pg["doc_id"]]
                ref_hits.append({
                    "evidence_id": f"{d['doc_id']}#p{pg['page']}", "evidence_type": "reference_document",
                    "collection": "reference", "title": d["title"],
                    "standard_number": None, "filename_number_hint": d.get("standard_number_from_filename"),
                    "exact_text": _snippet(pg["text"], toks), "source_ref": d["file"],
                    "location": f"第 {pg['page']} 页（PDF 页码）", "effect_status": "unknown",
                    "source_status": "pending", "review_status": "unreviewed", "pinned": False,
                    "score": list(sc), "content_hash": d.get("sha256"),
                    "qualification": "未审核参考资料：来源、版本现行性与适用性均未核实，仅供人工查阅，不参与判定",
                    "usable_for_determination": False,
                })
                if len(ref_hits) >= top_k:
                    break

    supplementary = (rule_hits + ref_hits)[:top_k] if coll == "all" else (rule_hits or ref_hits)[:top_k]
    formal_count = sum(1 for r in package["rules"].values() if r["collection"] == "formal_clauses")
    gaps = []
    if formal_count == 0:
        gaps.append("正式规范库为空（正式条款覆盖 0）：当前知识库无可用于正式判断的可靠条款，需人工核实。")
    if not supplementary and not pinned:
        gaps.append("未检索到相关资料；这不表示不存在相关要求。")
    if ref_hits or coll in ("all", "reference"):
        gaps.append("参考资料库中 PDF 多为扫描件，未做 OCR 的文件无法被关键词检索到。")
    data = {
        "query_context": {k: query.get(k) for k in ("query_text", "collection", "curtain_wall_type", "design_stage",
                                                    "region", "check_date")},
        "tokens": toks, "pinned": pinned, "candidates": supplementary,
        "retrieval_mode": "metadata_filter+keyword_bigram", "coverage_gaps": gaps,
        "formal_clause_count": formal_count, "reference_index": ref_meta,
        "statement": "检索结果是候选资料，尚未对方案判定。",
    }
    data["result_hash"] = sha256_of(data)
    return ToolResponse("TL13", "ok", data, issues, {"started_at": started, "ended_at": now_utc()},
                        input_hash=sha256_of({"q": data["query_context"], "pins": pinned_rule_ids, "k": top_k}))
