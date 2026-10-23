"""运行引擎：冻结输入快照 → 确定性工具（几何→长度→质量→金额→比较）→ 规则逐项 → 汇总 → 不可变运行记录。

数值全部来自 calculation/*；规则状态全部来自 evidence/evaluation；完成状态由程序决定，模型不参与。
"""
from __future__ import annotations

from calculation.comparison import compare_schemes
from calculation.cost import calculate_cost
from calculation.geometry import calculate_geometry
from calculation.lengths import calculate_lengths
from calculation.mass import calculate_mass
from domain.contracts import (
    MEASUREMENT_MODEL, MODULE_TEXT, ROUNDING_POLICY, TOOL_VERSIONS, Code, ExecState, Issue, new_id, now_utc, sha256_of,
)
from evidence.checklist import resolve_checklist
from evidence.evaluation import evaluate_rules
from evidence.retrieval import retrieve_evidence
from reporting.report import build_report, review_items
from storage.snapshots import build_snapshot

from .planner import LEVELS, TASK_LEVEL, TASK_TEXT
from .projects import ROLE_NAME, effective_params, has_candidate, project_package

ASSUMPTION = ("rect_uniform_axis_v1：单个矩形等距立面；左右边柱、顶底横梁计入；按分格轴线理论长度计量，"
              "不扣柱宽和连接间隙；不计接头、损耗、玻璃、五金、加工、运输、安装；单价按未税材料价")
LEVEL_MODULES = {"area": ["geometry"], "length": ["geometry", "length"], "mass": ["geometry", "length", "mass"],
                 "cost": ["geometry", "length", "mass", "cost"]}


def target_modules(project: dict, task: str) -> list[str]:
    mods = list(LEVEL_MODULES[TASK_LEVEL.get(task, "cost")])
    if task in ("compare", "auto") and has_candidate(project):
        mods.append("comparison")
    if project.get("rules"):
        mods.append("rule_check")
    return mods


def freeze_snapshots(ws, project: dict, roles: list[str], change_reason: str = "") -> dict:
    pkg = project_package(project)
    out = {}
    for role in roles:
        snap = build_snapshot(pkg, project["project_id"], role, effective_params(project, role),
                              label=ROLE_NAME[role], change_reason=change_reason,
                              zero_price_acknowledgements=(project.get("zero_acks") or {}).get(role, []),
                              accepted_assumptions=[ASSUMPTION])
        snap["project_id"] = project["project_id"]
        snap["scheme_rev"] = project["schemes"][role]["rev"]
        out[role] = ws.save_snapshot(snap)
    return out


def run_task(ws, project: dict, *, conversation_id: str, request_id: str, task: str, model_mode: str,
             change_reason: str = "") -> dict:
    existing = ws.find_run_by_request(request_id)
    if existing:
        return existing
    roles = ["baseline"] + (["candidate"] if has_candidate(project) else [])
    pkg = project_package(project)
    snaps = freeze_snapshots(ws, project, roles, change_reason)
    tmods = target_modules(project, task)
    input_hash = sha256_of({"snaps": {r: s["content_hash"] for r, s in snaps.items()}, "task": task,
                            "tools": TOOL_VERSIONS, "rules": sha256_of(project.get("rules") or {})})
    run = {"run_id": new_id("RUN"), "request_id": request_id, "input_hash": input_hash,
           "project_id": project["project_id"], "project_name": project["name"], "conversation_id": conversation_id,
           "requested_task": task, "requested_task_label": TASK_TEXT.get(task, task), "target_modules": tmods,
           "confirmed_modules": tmods, "case_id": project["project_id"],
           "dataset_version_id": f"{project['project_id']}@{project['data_rev']}", "data_rev": project["data_rev"],
           "scheme_revs": {r: project["schemes"][r]["rev"] for r in roles}, "is_demo": False,
           "tool_versions": dict(TOOL_VERSIONS), "rounding_policy": ROUNDING_POLICY,
           "measurement_model": MEASUREMENT_MODEL, "model_mode": model_mode, "started_at": now_utc(),
           "snapshots": snaps, "results": {}, "rule_results": {}, "module_statuses": {m: "running" for m in tmods},
           "module_issues": {}, "confirmation": {"meaning": "本次使用确认，不等于专业复核或工程签审",
                                                 "assumption": ASSUMPTION}}
    statuses, issues = run["module_statuses"], run["module_issues"]

    def record(mod, resp, role=None):
        ws.log("tool_call", {"tool_id": resp.tool_id, "status": resp.status, "role": role,
                             "issues": [i.code for i in resp.issues]}, project_id=project["project_id"],
               conversation_id=conversation_id, run_id=run["run_id"])
        prefix = f"{ROLE_NAME[role]}：" if role and len(roles) > 1 else ""
        for i in resp.issues:
            d = dict(i.to_dict(), message=prefix + i.message)
            if d not in issues.setdefault(mod, []):
                issues[mod].append(d)

    def set_status(mod, st):
        order = ["done", "blocked", "error"]
        cur = statuses.get(mod)
        if cur in (None, "running") or order.index(st) > order.index(cur):
            statuses[mod] = st

    checklist = None
    if "rule_check" in tmods:
        cl = resolve_checklist(pkg, project["project_id"])
        checklist = cl.data
        run["checklist"] = checklist
    for role in roles:
        snap, res = snaps[role], {}
        run["results"][role] = res
        try:
            g = calculate_geometry(snap)
            res["geometry"] = g.data
            record("geometry", g, role)
            # 只算面积时，分格缺失不影响面积结果
            if TASK_LEVEL.get(task) == "area" and "facade_area_m2" in g.data["metrics"]:
                set_status("geometry", "done")
            else:
                set_status("geometry", "done" if g.status == "ok" else "blocked")
            if "length" in tmods:
                ln = calculate_lengths(snap, g.data)
                res["length"] = ln.data
                record("length", ln, role)
                set_status("length", "done" if ln.status == "ok" else "blocked")
            if "mass" in tmods:
                ms = calculate_mass(snap, res["length"], g.data)
                res["mass"] = ms.data
                record("mass", ms, role)
                set_status("mass", "done" if ms.status == "ok" else "blocked")
            if "cost" in tmods:
                ct = calculate_cost(snap, res["mass"], g.data)
                res["cost"] = ct.data
                record("cost", ct, role)
                set_status("cost", "done" if ct.status == "ok" else "blocked")
        except Exception as exc:  # noqa: BLE001 - 失败如实记录，不由模型补算
            for m in ("geometry", "length", "mass", "cost"):
                if statuses.get(m) == "running":
                    set_status(m, "error")
            issues.setdefault("geometry", []).append(Issue(Code.TOOL_FAILED, f"{ROLE_NAME[role]}计算失败：{exc}",
                                                           retryable=True).to_dict())
        if checklist is not None:
            try:
                rr = evaluate_rules(checklist, snap, res.get("geometry"))
                run["rule_results"][role] = rr.data
                record("rule_check", rr, role)
                set_status("rule_check", {"ok": "done", "partial": "blocked"}.get(rr.status, "error"))
            except Exception as exc:  # noqa: BLE001
                set_status("rule_check", "error")
                issues.setdefault("rule_check", []).append(Issue(Code.TOOL_FAILED, f"核查执行失败：{exc}").to_dict())
    if "comparison" in tmods:
        try:
            cmp = compare_schemes(snaps["baseline"], snaps["candidate"], run["results"]["baseline"],
                                  run["results"]["candidate"])
            run["comparison"] = cmp.data
            record("comparison", cmp)
            set_status("comparison", "done" if cmp.status == "ok" else "blocked")
        except Exception as exc:  # noqa: BLE001
            set_status("comparison", "error")
            issues.setdefault("comparison", []).append(Issue(Code.TOOL_FAILED, f"比较失败：{exc}").to_dict())
    for m, st in list(statuses.items()):
        if st == "running":
            statuses[m] = "done"
    done = [m for m in tmods if statuses.get(m) == "done"]
    if len(done) == len(tmods):
        state = ExecState.COMPLETE
    elif any((run["results"].get(r) or {}).get("geometry", {}).get("metrics") for r in roles):
        state = ExecState.PARTIAL
    else:
        state = ExecState.FAILED
    run["execution_state"] = state.value
    cand_rr = run["rule_results"].get("candidate") or run["rule_results"].get("baseline")
    run["coverage"] = (cand_rr or {}).get("coverage") or {"formal_clause_count": 0, "demo_rule_count": 0,
                                                          "required_count": 0, "result_count": 0}
    statuses_used = set()
    for role, s in snaps.items():
        for f, fv in s["parameters"].items():
            for pid in fv.get("provenance_ids") or []:
                internal = f in ("region", "check_date", "measurement_model_id", "price_basis_id") or \
                    f.endswith("_profile_id")  # 系统生成的引用，不是用户输入的业务数值
                if not internal and fv.get("value") is not None:
                    statuses_used.add(s["provenance"].get(pid, {}).get("source_status"))
        for d in list((s.get("resolved_profiles") or {}).values()) + list((s.get("resolved_prices") or {}).values()):
            for pid in d.get("provenance_ids") or []:
                statuses_used.add(s["provenance"].get(pid, {}).get("source_status"))
    statuses_used.discard(None)
    run["source_statuses"] = sorted(statuses_used)
    run["contains_synthetic"] = "synthetic" in statuses_used
    run["contains_pending"] = "pending" in statuses_used
    run["completeness_text"] = completeness_text(run)
    run["review_items"] = review_items(run)
    run["ended_at"] = now_utc()
    try:
        run["report"] = build_report(run, "fallback")  # 结果卡用模板；模型解释只在用户追问“为什么”时生成
        statuses["report"] = "done"
    except Exception as exc:  # noqa: BLE001
        run["report"] = None
        statuses["report"] = "error"
        issues.setdefault("report", []).append(Issue(Code.TOOL_FAILED, f"报告生成失败：{exc}").to_dict())
    saved = ws.save_run(run)
    ws.log("run_settled", {"state": run["execution_state"], "modules": statuses}, project_id=project["project_id"],
           conversation_id=conversation_id, run_id=run["run_id"])
    return saved


def run_query(ws, *, query_text: str, conversation_id: str, request_id: str, project: dict | None) -> dict:
    existing = ws.find_run_by_request(request_id)
    if existing:
        return existing
    pkg = project_package(project) if project else {"rules": {}, "provenance": {}}
    idx = ws.document_index()
    q = retrieve_evidence(pkg, {"query_text": query_text, "collection": "all" if project and project.get("rules")
                                else "reference", "curtain_wall_type": "component_glass"}, top_k=6,
                          reference_index=idx)
    run = {"run_id": new_id("RUN"), "request_id": request_id,
           "input_hash": sha256_of({"q": query_text, "docs": [d["sha256"] for d in idx["documents"]]}),
           "project_id": (project or {}).get("project_id"), "conversation_id": conversation_id,
           "requested_task": "query", "requested_task_label": "查找资料", "started_at": now_utc(),
           "ended_at": now_utc(), "retrieval": q.data, "execution_state": "COMPLETE" if q.status == "ok" else "PARTIAL",
           "documents_available": len(idx["documents"]), "tool_versions": dict(TOOL_VERSIONS)}
    return ws.save_run(run)


def run_standard_query(ws, *, query_text: str, conversation_id: str, request_id: str, options: dict) -> dict:
    """An isolated read-only standards workflow, with no project/package parameters."""
    from agent.standard_qa import answer
    from evidence.standard_retrieval import StandardRetriever
    existing = ws.find_run_by_request(request_id)
    if existing:
        return existing
    library = ws.standard_library()
    retriever = StandardRetriever(library)
    started = now_utc()
    try:
        retrieval = retriever.search(query_text, options.get("collection_ids", []),
                                     standards=options.get("standards"), versions=options.get("versions"),
                                     categories=options.get("categories"))
        response = answer(library, retrieval, mode=options.get("answer_mode", "source"),
                          allow_model=bool(options.get("allow_model")))
    finally:
        retriever.close()
    run = {"run_id": new_id("RUN"), "request_id": request_id, "project_id": None,
           "conversation_id": conversation_id, "requested_task": "standard_qa", "requested_task_label": "规范问答",
           "input_hash": sha256_of({"query": query_text, "options": options, "retrieval": retrieval}),
           "started_at": started, "ended_at": now_utc(), "options": options,
           "retrieval": retrieval, "answer": response,
           "execution_state": "COMPLETE" if response["evidence"] else "PARTIAL"}
    return ws.save_run(run)


def completeness_text(run: dict) -> str:
    st = run["module_statuses"]
    parts = []
    for m in ("geometry", "length", "mass", "cost", "comparison"):
        if m in st:
            parts.append(f"{MODULE_TEXT[m]}{'已完成' if st[m] == 'done' else '未完成'}")
    if "rule_check" in st:
        cov = run.get("coverage") or {}
        parts.append(f"演示规则 {cov.get('result_count', 0)}/{cov.get('required_count', 0)} 项已记录")
    parts.append("正式规范未覆盖（正式条款 0 条）")
    prefix = {"COMPLETE": "本次计算完成：", "PARTIAL": "部分完成：", "FAILED": "未能计算："}[run["execution_state"]]
    return prefix + "；".join(parts)


def is_current(project: dict | None, run: dict | None) -> bool:
    """运行是否对应项目当前参数（否则界面标注“参数已修改，以下为上次结果”）。"""
    if not project or not run or run.get("project_id") != project["project_id"]:
        return False
    revs = {r: project["schemes"][r]["rev"] for r in project["schemes"]}
    return run.get("scheme_revs") == revs and run.get("data_rev") == project["data_rev"]


__all__ = ["run_task", "run_query", "is_current", "freeze_snapshots", "LEVELS"]
