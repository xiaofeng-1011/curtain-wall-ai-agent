"""方案变更后的规范核查：变更内容 → 可能涉及的规范要求 → 相关依据 → 所需参数 → 逐项结果。

逐项结果五类，分别给原方案和修改方案：
- 已核查符合 / 已核查不符合：只来自经过审核、效力确认的可机判规则（evidence.evaluation）。
- 存在潜在冲突：变更朝不利方向发展并触发需复核的条件（例如板块变大、立柱间距变大、线重降低），
  只说明风险和触发条件，不冒充已确定违规。
- 暂时无法判断：缺少参数、资料或验算时列出所缺内容。
- 不适用：说明原因（例如单价变化不涉及规范核查）。

内置 PDF 库只提供检索证据，不等于可自动判定的规则库；模型提出的规则只作为待审核草稿。
不从少量检查推导整个工程“全部合规”或“结构安全”。
"""
from __future__ import annotations

from decimal import Decimal

from domain.contracts import RuleStatus, dec, now_utc, sha256_of

COMPLIANT, NONCOMPLIANT, POTENTIAL, UNDETERMINED, NOT_APPLICABLE = (
    "compliant", "noncompliant", "potential_conflict", "undetermined", "not_applicable")
STATUS_TEXT = {COMPLIANT: "已核查符合", NONCOMPLIANT: "已核查不符合", POTENTIAL: "存在潜在冲突",
               UNDETERMINED: "暂时无法判断", NOT_APPLICABLE: "不适用"}

GRID = {"grid_width_m", "grid_height_m"}
COLUMN_SECTION = {"column_profile_id", "column_mass_override", "column.linear_mass"}
BEAM_SECTION = {"beam_profile_id", "beam_mass_override", "beam.linear_mass"}
PRICE = {"column_price_override", "beam_price_override", "price_basis_id", "column.unit_price", "beam.unit_price"}
FACADE = {"width_m", "height_m"}

# 参数名都是软件目前不收集的验算输入；列出它们是为了说明“缺什么”，不会被补造。
TOPICS = [
    {"id": "panel", "title": "玻璃面板强度与挠度（板块尺寸变化）", "fields": GRID,
     "query": "玻璃幕墙 玻璃 厚度 挠度 风荷载 计算",
     "params": ["风荷载标准值（或基本风压、地面粗糙度、建筑高度）", "玻璃种类、厚度及中空/夹层构成", "面板支承方式（四边/两边/点支承）"]},
    {"id": "column", "title": "立柱强度与挠度（立柱间距或截面变化）", "fields": {"grid_width_m"} | COLUMN_SECTION,
     "query": "立柱 截面 承载力 挠度 验算",
     "params": ["风荷载标准值", "立柱计算跨度与支承方式（层高、连接位置）", "立柱截面惯性矩、截面抵抗矩", "型材牌号与状态（强度设计值）"]},
    {"id": "beam", "title": "横梁强度与挠度（横梁跨度或截面变化）", "fields": GRID | BEAM_SECTION,
     "query": "横梁 挠度 承载力 计算",
     "params": ["风荷载标准值", "面板自重（玻璃厚度）", "横梁截面惯性矩、截面抵抗矩", "型材牌号与状态"]},
    {"id": "profile", "title": "铝合金型材截面壁厚等构造要求（型材变化）", "fields": COLUMN_SECTION | BEAM_SECTION,
     "query": "铝合金型材 立柱 横梁 截面 壁厚",
     "params": ["截面主要受力部位壁厚（开口/闭口部位）", "型材牌号、状态与表面处理"]},
    {"id": "connection", "title": "立柱与主体结构连接（单根立柱受荷变化）", "fields": {"grid_width_m"} | FACADE,
     "query": "立柱 主体结构 连接 螺栓 预埋件",
     "params": ["连接件、螺栓规格与数量", "预埋件或后置埋件做法", "主体结构类型"]},
    {"id": "layout", "title": "立面分格与玻璃尺度、加工运输条件", "fields": GRID,
     "query": "幕墙 分格 立面 设计",
     "params": ["建筑立面效果要求", "玻璃原片与加工、运输、吊装尺寸限制"]},
]


def _value(snapshot: dict, field: str):
    if "." in field:
        comp, key = field.split(".")
        return ((snapshot.get("resolved_profiles") or {}).get(comp) or {}).get(key)
    return ((snapshot.get("parameters") or {}).get(field) or {}).get("value")


def _metric(run: dict, role: str, key: str):
    for mod in ("geometry", "length", "mass", "cost"):
        m = (((run.get("results") or {}).get(role) or {}).get(mod) or {}).get("metrics", {}).get(key)
        if m and m.get("value") is not None:
            return dec(m["value"])
    return None


def _trend(run: dict, topic_id: str, changed: set[str]) -> list[str]:
    """修改方案相对原方案朝需复核方向的变化（只陈述程序计算出的事实）。"""
    out = []
    pa, pb = _metric(run, "baseline", "panel_area_m2"), _metric(run, "candidate", "panel_area_m2")
    ca, cb = _metric(run, "baseline", "column_count"), _metric(run, "candidate", "column_count")
    snaps = run.get("snapshots") or {}
    gw = [_value(snaps.get(r) or {}, "grid_width_m") for r in ("baseline", "candidate")]
    gh = [_value(snaps.get(r) or {}, "grid_height_m") for r in ("baseline", "candidate")]
    lm = {c: [_value(snaps.get(r) or {}, f"{c}.linear_mass") for r in ("baseline", "candidate")] for c in ("column", "beam")}

    def bigger(a, b):
        return a is not None and b is not None and dec(b) > dec(a)

    if topic_id in ("panel", "layout") and pa is not None and pb is not None and pb > pa:
        out.append(f"单块面板面积由 {pa.normalize():f} m² 增至 {pb.normalize():f} m²")
    if topic_id in ("column", "connection") and bigger(gw[0], gw[1]):
        out.append(f"立柱间距（分格宽）由 {gw[0]} m 增至 {gw[1]} m，单根立柱受荷宽度增大")
    if topic_id == "column" and ca is not None and cb is not None and cb < ca:
        out.append(f"立柱轴线数由 {ca.normalize():f} 条减至 {cb.normalize():f} 条")
    if topic_id == "beam" and (bigger(gw[0], gw[1]) or bigger(gh[0], gh[1])):
        out.append("横梁跨度（分格宽）或承担的板块高度增大")
    for comp, label in (("column", "立柱"), ("beam", "横梁")):
        a, b = lm[comp]
        if topic_id in (comp, "profile") and a is not None and b is not None and dec(b) < dec(a):
            out.append(f"{label}线重由 {a} kg/m 降至 {b} kg/m（线重不能代表截面性能，需核对截面参数）")
    return out


def _rule_items(run: dict) -> list[dict]:
    """已配置规则的逐项结果；正式审核规则才可给出“符合／不符合”。"""
    items = {}
    for role in ("baseline", "candidate"):
        for r in ((run.get("rule_results") or {}).get(role) or {}).get("rule_results", []):
            demo = r.get("evidence_type") == "demo_rule"
            st = r.get("status")
            if demo:
                status = UNDETERMINED
                text = f"演示规则：{r.get('status_text')}（只验证流程，不是规范结论）"
            elif st == RuleStatus.CONDITION_MET.value:
                status, text = COMPLIANT, r.get("reason")
            elif st == RuleStatus.CONDITION_NOT_MET.value:
                status, text = NONCOMPLIANT, f"{r.get('condition')}；{r.get('reason')}"
            elif st == RuleStatus.NOT_APPLICABLE.value:
                status, text = NOT_APPLICABLE, r.get("reason")
            else:
                status, text = UNDETERMINED, r.get("reason")
            item = items.setdefault(r["rule_id"], {
                "id": "rule:" + r["rule_id"], "title": r.get("title") or r["rule_id"], "kind": "demo_rule" if demo else "rule",
                "source": "已配置规则" + ("（演示）" if demo else "（经审核、效力确认）"), "params": [], "evidence": [],
                "results": {}})
            item["results"][role] = {"status": status, "status_text": STATUS_TEXT[status], "detail": text,
                                     "actual_values": r.get("actual_values"), "rule_status": st}
    return list(items.values())


def check_changes(run: dict, *, retriever=None, collection_ids=None, library=None) -> dict:
    """生成本次运行的变更核查。证据只来自已登记资料并经过来源校验。"""
    from evidence.standard_meta import citation_note
    from evidence.standard_retrieval import validate_evidence
    comp = run.get("comparison") or {}
    changed = {c["field"] for c in comp.get("changed_fields") or []}
    changed_labels = [c["label"] for c in comp.get("changed_fields") or []]
    two = bool((run.get("results") or {}).get("candidate"))
    items, evidence_version = [], {"collections": list(collection_ids or []), "index_version": None, "revisions": []}
    for topic in TOPICS:
        if two and not (topic["fields"] & changed):
            continue
        hits = []
        if retriever is not None and collection_ids:
            res = retriever.search(topic["query"], collection_ids, top_k=3)
            evidence_version["index_version"] = res.get("index_version")
            hits = [h for h in res["evidence"] if library is None or validate_evidence(library, h, collection_ids)]
            evidence_version["revisions"] += [f"{h['collection_id']}@{h['revision']}" for h in hits]
        trend = _trend(run, topic["id"], changed) if two else []
        missing = list(topic["params"])
        base_detail = "缺少验算参数：" + "；".join(missing)
        results = {"baseline": {"status": UNDETERMINED, "status_text": STATUS_TEXT[UNDETERMINED], "detail": base_detail}}
        if two:
            if trend:
                results["candidate"] = {"status": POTENTIAL, "status_text": STATUS_TEXT[POTENTIAL],
                                        "detail": "；".join(trend) + "。这是需复核的变化方向，不等于已确定违规；" + base_detail}
            else:
                results["candidate"] = {"status": UNDETERMINED, "status_text": STATUS_TEXT[UNDETERMINED], "detail": base_detail}
        items.append({"id": topic["id"], "title": topic["title"], "kind": "topic",
                      "source": "变更影响识别（程序规则）+ 资料检索", "trigger": "、".join(
                          c["label"] for c in comp.get("changed_fields") or [] if c["field"] in topic["fields"]) or "当前方案",
                      "params": missing, "evidence": [{k: h[k] for k in ("evidence_id", "collection_id", "doc_id", "revision",
                                                                          "content_hash", "index_version", "sources", "text",
                                                                          "standard_code", "version", "clause_candidate",
                                                                          "section", "title", "filename", "quality",
                                                                          "numeric_risk", "answer_eligible", "current_status",
                                                                          "review_status", "sample_review_status", "category",
                                                                          "ordinal", "context") if k in h}
                                                      for h in hits],
                      "notes": [citation_note(i + 1, h, retriever.mandatory_clauses(h["collection_id"], h["doc_id"])
                                              if retriever is not None else None) for i, h in enumerate(hits)],
                      "results": results})
    price_only = two and changed and changed <= PRICE
    if two and changed & PRICE:
        items.append({"id": "price", "title": "材料单价或价格基准变化", "kind": "topic", "source": "程序规则",
                      "trigger": "、".join(c["label"] for c in comp.get("changed_fields") or [] if c["field"] in PRICE),
                      "params": [], "evidence": [], "notes": [],
                      "results": {r: {"status": NOT_APPLICABLE, "status_text": STATUS_TEXT[NOT_APPLICABLE],
                                      "detail": "单价只影响材料金额，不涉及规范核查项"} for r in ("baseline", "candidate")}})
    items += _rule_items(run)
    counts = {}
    for it in items:
        for role, r in it["results"].items():
            counts.setdefault(role, {k: 0 for k in STATUS_TEXT})[r["status"]] += 1
    data = {"created_at": now_utc(), "run_id": run.get("run_id"), "changed_fields": changed_labels, "items": items,
            "counts": counts, "price_only": bool(price_only), "evidence_version": evidence_version,
            "statement": ("逐项结果只针对所列单项；没有逐项核查到的内容不代表符合要求，"
                          "不能据此判断工程整体合规或结构安全。内置规范库是检索资料，不是已审核的自动判定规则库。")}
    data["check_hash"] = sha256_of({k: v for k, v in data.items() if k != "created_at"})
    return data


def summary_lines(check: dict) -> list[str]:
    lines = []
    for it in check.get("items", []):
        r = it["results"]
        if "candidate" in r:
            a, b = r["baseline"]["status_text"], r["candidate"]["status_text"]
            change = "（修改后变化）" if r["baseline"]["status"] != r["candidate"]["status"] else ""
            lines.append(f"{it['title']}：原方案 {a} → 修改方案 {b}{change}")
        else:
            lines.append(f"{it['title']}：{r['baseline']['status_text']}")
    return lines


def _d(v):
    return Decimal(str(v))
