"""单一聊天主界面：左栏（新对话、最近对话与项目、设置）、主对话区、底部输入框；参数／依据／设置按需在弹窗查看。

界面只调用 Assistant 的公开方法，不实现任何计算公式；默认不显示哈希、状态枚举、调试开关和长参数表。
"""
from __future__ import annotations

import json
from html import escape
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from agent import documents, engine, importer, migration
from agent import projects as P
from agent.assistant import Assistant, AssistantError
from agent.package_import import import_package
from calculation.common import metric_label
from domain.contracts import RULE_STATUS_TEXT, RuleStatus, new_id, now_utc
from domain.fields import USER_FIELDS
from reporting.export import build_artifacts
from reporting.report import SCOPE_STATEMENT, all_metrics, banner, fmt, param_rows
from reporting.xlsx import build_xlsx
from storage.workspace import StoreError, Workspace

PLACEHOLDER = "输入方案尺寸、材料信息或你想调整的内容，也可以粘贴表格"
HINTS = {"compare": "先描述原方案（尺寸、分格、线重、单价），再说要怎么调整，例如“分格宽改为 1.5m，与原方案比较”",
         "query": "输入要查找的问题，例如“玻璃幕墙立柱截面厚度要求”（在你上传的资料中检索）"}
SOURCE_TEXT = {"synthetic": "演示数据", "pending": "来源待核实", "verified": "已确认来源", "public": "公开资料"}
REVIEW_TEXT = {"unreviewed": "未复核", "pending_professional_review": "待专业复核", "in_review": "复核中",
               "technically_reviewed": "已由指定人员复核", "rejected": "已退回", "outdated": "已过期"}
REASON = {"PRICE_MISSING": "缺单价", "MASS_MISSING": "缺线重", "PROFILE_MISSING": "缺线重", "FIELD_MISSING": "缺参数",
          "NON_DIVISIBLE_GRID": "分格不能整除", "UPSTREAM_NOT_DONE": "前一步未完成", "VALUE_INVALID": "输入无效",
          "PRICE_BASIS_MISMATCH": "报价口径需确认", "ZERO_PRICE_UNCONFIRMED": "零价待确认", "UNIT_REQUIRED": "缺单位",
          "AREA_CONFLICT": "面积不一致", "COMPARISON_INCOMPATIBLE": "口径不一致"}
FILE_TYPES = ["xlsx", "xlsm", "csv", "pdf", "txt", "md"]


# ====================================================================== 服务与状态
@st.cache_resource
def services() -> tuple[Workspace, Assistant]:
    ws = Workspace()
    return ws, Assistant(ws)


def current_conv(ws: Workspace):
    cid = st.session_state.get("conv_id") or st.query_params.get("c")
    conv = ws.get_conversation(cid) if cid else None
    if conv is None:  # 找不到就是空态，不取其他对话兜底
        st.session_state.pop("conv_id", None)
        if "c" in st.query_params:
            del st.query_params["c"]
        return None
    st.session_state["conv_id"] = conv["conversation_id"]
    st.query_params["c"] = conv["conversation_id"]
    return conv


def open_conv(conv_id: str | None) -> None:
    st.session_state["conv_id"] = conv_id
    st.session_state.pop("hint", None)
    if conv_id:
        st.query_params["c"] = conv_id
    elif "c" in st.query_params:
        del st.query_params["c"]
    st.session_state.pop("submission_error", None)


def want_dialog(name: str, **kw) -> None:
    st.session_state["dialog"] = (name, kw)


# ====================================================================== 左栏
def _plain_label(value: str) -> str:
    return "".join(chr(92) + c if c in "\\`*_{}[]()#+.!<>|~-" else c for c in value)


def _history_group(value: str) -> str:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).date()
        today = datetime.now(timezone(timedelta(hours=8))).date()
        return "今天" if date == today else "近 7 天" if (today - date).days < 7 else "更早"
    except (ValueError, TypeError):
        return "更早"


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        return value or "时间未记录"


def sidebar(ws: Workspace, asst: Assistant, conv) -> None:
    with st.sidebar:
        st.markdown('<div class="mw-brand"><div class="mw-brand-name">幕墙方案助手</div>'
                    '<div class="mw-brand-sub">方案整理 / 材料比选</div></div>', unsafe_allow_html=True)
        if st.button("新对话", icon=":material/add:", width="stretch", key="new_conv", type="primary"):
            open_conv(None)
            st.rerun()
        if st.button("规范问答", icon=":material/menu_book:", width="stretch", key="new_standard_qa"):
            qa = asst.new_conversation(mode="standard_qa")
            choices = ws.standard_library().list_collections()
            if choices:
                asst.set_qa_options(qa["conversation_id"], dict(qa["qa_options"], collection_ids=[choices[0]["collection_id"]]))
            open_conv(qa["conversation_id"])
            st.rerun()
        search = st.text_input("搜索对话", placeholder="搜索对话", icon=":material/search:",
                               label_visibility="collapsed", key="conv-search")
        convs = [c for c in ws.list_conversations() if c.get("messages")]
        visible = [c for c in convs if search.strip().casefold() in c.get("title", "").casefold()]
        limit = st.session_state.get("history_limit", 40)
        cur = (conv or {}).get("conversation_id")
        with st.container(height=400, border=False, key="sidebar-history"):
            if not visible:
                st.caption("没有找到对应对话" if search else "开始第一段对话，历史会保存在这里。")
                if search:
                    st.button("清除搜索", on_click=lambda: st.session_state.update({"conv-search": ""}))
            group = None
            for c in visible[:limit]:
                next_group = _history_group(c.get("updated_at"))
                if next_group != group:
                    st.caption(next_group)
                    group = next_group
                active = cur == c["conversation_id"]
                with st.container(horizontal=True, wrap=False, vertical_alignment="center",
                                  key=("history-current-" if active else "history-row-") + c["conversation_id"], gap="xxsmall"):
                    if st.button(_plain_label(c["title"]), icon=":material/chat_bubble_outline:",
                                 help=c["title"], key=f"cv-{c['conversation_id']}", width="stretch",
                                 type="tertiary"):
                        open_conv(c["conversation_id"])
                        st.rerun()
                    with st.popover("对话菜单", icon=":material/more_horiz:", width=36,
                                    help="对话菜单 · " + c["title"], key="menu-" + c["conversation_id"]):
                        new = st.text_input("重命名对话", c["title"], max_chars=40, key=f"rn-{c['conversation_id']}")
                        st.caption("最多 40 个字符。仅修改对话名称。")
                        if st.button("保存名称", disabled=not new.strip(), key=f"rns-{c['conversation_id']}"):
                            asst.rename_conversation(c["conversation_id"], new)
                            st.rerun()
                        if st.button("移入回收站", icon=":material/delete:", key=f"dc-{c['conversation_id']}"):
                            want_dialog("delete_conversation", conv_id=c["conversation_id"])
                            st.rerun()
            if len(visible) > limit and st.button(f"加载更多 · 还有 {len(visible) - limit} 条", width="stretch"):
                st.session_state["history_limit"] = limit + 40
                st.rerun()
            with_conv = {c.get("project_id") for c in convs}
            orphan = [p for p in ws.list_projects() if p["project_id"] not in with_conv]
            if orphan:
                st.caption("项目")
                for p in orphan:
                    if st.button(_plain_label(p["name"]), icon=":material/folder_open:",
                                 key=f"op-{p['project_id']}", width="stretch"):
                        c = asst.new_conversation(p["project_id"])
                        open_conv(c["conversation_id"])
                        st.rerun()
        with st.container(key="sidebar-tools"):
            st.divider()
            for label, icon, dialog in (("资料库", "library_books", "documents"), ("材料库", "inventory_2", "materials"),
                                        ("设置", "settings", "settings")):
                if st.button(label, icon=f":material/{icon}:", key=f"nav-{dialog}", width="stretch", type="tertiary"):
                    want_dialog(dialog, conv_id=(conv or {}).get("conversation_id"))
                    st.rerun()


def conversation_header(ws, asst, conv):
    project = asst.project_of(conv)
    title = (conv or {}).get("title") or "新对话"
    if not conv or not conv.get("messages"):
        return
    run = ws.get_run(conv.get("last_run_id"), (project or {}).get("project_id"))
    pending = (conv.get("pending") or {}).get("type")
    state = "资料查阅" if conv.get("pending_query") else "对话中"
    if project:
        state = "待补参数" if pending == "question" else "待首次确认" if pending == "confirm" else "方案草稿"
        if run and run.get("requested_task") != "query":
            state = ("当前结果" if run.get("execution_state") == "COMPLETE" else "部分结果") if engine.is_current(project, run) else "参数已修改，结果待更新"
    with st.container(key="conversation-header"):
        with st.container(horizontal=True, wrap=False, vertical_alignment="center", height=44, border=False):
            st.markdown(f'<div class="mw-title" title="{escape(title)}">{escape(title)}</div>', unsafe_allow_html=True)
            if project and st.button("方案参数", icon=":material/tune:", key="header-params", width=120):
                want_dialog("params", conv_id=conv["conversation_id"])
                st.rerun()
        project_label = f"项目：{project['name']}" if project else "未关联项目"
        role = ("调整方案" if P.has_candidate(project) else "原方案") if project else "资料与问答"
        st.markdown(f'<div class="mw-subtitle">{escape(project_label)}<span> · </span>{role}'
                    f'<span class="mw-status">{escape(state)}</span></div>', unsafe_allow_html=True)


# ====================================================================== 首页
def home(ws: Workspace) -> None:
    st.markdown('<div class="mw-home"><div class="mw-eyebrow">从一个方案开始</div><h1>让每一次调整，<br>都有清楚的依据。</h1>'
                '<p class="lead">描述你的方案，整理尺寸与材料，计算理论用量，<br>再看看调整前后的变化。</p></div>',
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    if c1.button("录入方案", icon=":material/edit_square:", width="stretch"):
        want_dialog("params", conv_id=None)
        st.rerun()
    if c2.button("调整并比较", icon=":material/compare_arrows:", width="stretch"):
        st.session_state["hint"] = "compare"
        st.rerun()
    if c3.button("查阅资料", icon=":material/menu_book:", width="stretch"):
        st.session_state["hint"] = "query"
        st.rerun()
    hint = st.session_state.get("hint")
    if hint:
        st.markdown(f'<div class="mw-task-hint"><span>当前任务</span><strong>{"调整并比较" if hint == "compare" else "查阅资料"}</strong><p>{HINTS[hint]}</p></div>',
                    unsafe_allow_html=True)
        if hint == "query":
            n = sum(d.get("text_pages", 0) > 0 for d in ws.list_documents())
            st.caption(f"当前可检索 {n} 份资料。" if n else "暂无可检索资料。上传后可继续查阅原问题。")
            if st.button("上传资料", icon=":material/upload_file:", key="home-upload"):
                want_dialog("documents")
                st.rerun()
        if st.button("退出当前任务", key="exit-hint"):
            st.session_state.pop("hint", None)
            st.rerun()
    st.markdown('<p class="mw-scope">当前支持矩形等距构件式幕墙；结果供方案辅助，不作安全验算或工程签审。</p>',
                unsafe_allow_html=True)
    with st.expander("查看详细范围"):
        st.markdown("- 可计算：立面面积、立柱横梁理论轴线长度（含边柱和顶底横梁）、型材质量、未税材料金额、两方案差值\n"
                    "- 不包含：玻璃、五金、加工、损耗、运输、安装、税费；不是采购量或工程总价\n"
                    "- 不支持：单元式／石材／金属板幕墙、曲面、洞口、非等距分格、CAD/BIM 识别、结构验算、施工图审查\n"
                    "- 资料检索只在你上传的资料中进行，资料未经审核，仅供查阅")
    mp = migration.pending(ws)
    if mp:
        with st.container(border=True):
            st.markdown(f"检测到旧版本数据（{mp['drafts']} 个草稿、{mp['runs']} 次计算）。升级不会自动删除它们，请选择：")
            a, b, c = st.columns(3)
            if a.button("保留旧项目继续使用"):
                r = migration.migrate(ws, "keep")
                st.toast(f"已转换 {r['converted']} 个项目，备份：{Path(r['backup']).name}")
                st.rerun()
            if b.button("移入回收站，从空白开始"):
                r = migration.migrate(ws, "trash")
                st.toast(f"已转换 {r['converted']} 个项目并移入回收站，备份：{Path(r['backup']).name}")
                st.rerun()
            if c.button("暂不处理"):
                migration.migrate(ws, "ignore")
                st.rerun()


# ====================================================================== 消息渲染
def _cell(run, role, key, mod):
    m = all_metrics((run.get("results") or {}).get(role)).get(key)
    if m and m.get("value") is not None:
        return fmt(m, key), False
    for i in (run.get("module_issues") or {}).get(mod, []):
        if i.get("blocking", True):
            return "未计算：" + REASON.get(i.get("code"), "缺参数"), True
    return "未计算：本次未覆盖", True


def _tags(run) -> str:
    out = []
    if run.get("contains_synthetic"):
        out.append('<span class="mw-tag demo">演示数据</span>')
    if run.get("contains_pending"):
        out.append('<span class="mw-tag warn">来源待核实</span>')
    out.append('<span class="mw-tag">仅立柱横梁理论材料金额</span>')
    cov = run.get("coverage") or {}
    if cov.get("required_count"):
        out.append(f'<span class="mw-tag">演示规则 {cov["required_count"]} 条；正式条款 0 条</span>')
    else:
        out.append('<span class="mw-tag">未执行正式规范核查</span>')
    return " ".join(out)


def result_card(ws, run: dict, project, msg_id: str, latest: bool, summary: str = "") -> None:
    current = engine.is_current(project, run)
    if not current:
        st.markdown('<div class="mw-stale">参数已修改 · 此卡为历史结果</div>', unsafe_allow_html=True)
        if st.button("按当前参数重新计算", icon=":material/refresh:", key="recalc-" + msg_id):
            _, asst = services()
            conv = current_conv(ws)
            asst.send(conv["conversation_id"], {"area": "算面积", "length": "算长度", "mass": "算型材质量",
                                                "compare": "比较两个方案"}.get(run.get("requested_task"), "算材料金额"))
            st.rerun()
    label = "历史结果" if not current else "本次计算" if run.get("execution_state") == "COMPLETE" else "部分结果"
    versions = " · ".join(f"{P.ROLE_NAME[r]} v{v.get('version', 1)}" for r, v in (run.get("snapshots") or {}).items())
    st.markdown(f'<div class="mw-result-identity"><strong>{label}</strong><span>{escape(versions)} · '
                f'{escape(_display_time(run.get("started_at", "")))}</span></div>', unsafe_allow_html=True)
    if summary:
        st.markdown(f'<div class="mw-result-summary">{escape(summary)}</div>', unsafe_allow_html=True)
    roles = [r for r in ("baseline", "candidate") if (run.get("results") or {}).get(r)]
    comp = run.get("comparison") or {}
    if len(roles) == 2:
        rows = [("材料金额（元）", "total_cost_cny", "cost"), ("理论质量（kg）", "total_mass_kg", "mass"),
                ("立柱总长（m）", "column_length_m", "length"), ("横梁总长（m）", "beam_length_m", "length")]
        preferred = {"length": "length", "mass": "mass", "area": "geometry"}.get(run.get("requested_task"), "cost")
        rows.sort(key=lambda row: row[2] != preferred)
        if not any(_cell(run, r, "column_length_m", "length")[0][0].isdigit() for r in roles):
            rows = [("立面面积（m²）", "facade_area_m2", "geometry")]
        html = ['<div class="mw-result"><table><tr><th></th><th>原方案</th><th>调整方案</th>'
                + ("<th>差值</th>" if comp.get("comparable") else "") + "</tr>"]
        for label, key, mod in rows:
            cells = [_cell(run, r, key, mod) for r in roles]
            d = (comp.get("delta_metrics") or {}).get(f"delta_{key}")
            html.append(f"<tr><td>{label}</td>" + "".join(
                f'<td class="{"missing" if miss else ""}">{v}</td>' for v, miss in cells)
                        + (f"<td>{fmt(d, 'delta_' + key) if d else '—'}</td>" if comp.get("comparable") else "")
                        + "</tr>")
        html.append("</table></div>")
    else:
        r = roles[0] if roles else "baseline"
        rows = [("立面面积（m²）", "facade_area_m2", "geometry"), ("立柱总长（m）", "column_length_m", "length"),
                ("横梁总长（m）", "beam_length_m", "length"), ("型材质量（kg）", "total_mass_kg", "mass"),
                ("材料金额（元）", "total_cost_cny", "cost")]
        rows = [x for x in rows if x[2] in run.get("target_modules", []) or x[2] == "geometry"]
        html = ['<div class="mw-result"><table>']
        for label, key, mod in rows:
            v, miss = _cell(run, r, key, mod)
            html.append(f'<tr><td>{label}</td><td class="{"missing" if miss else ""}">{v}</td></tr>')
        html.append("</table></div>")
    st.markdown("".join(html), unsafe_allow_html=True)
    if comp.get("comparable"):
        st.caption("差值 = 调整方案 − 原方案；金额减少仅表示本计量口径下的变化。")
        st.caption("在本计量模型下：" + ("材料与单价不变时，" if not any(
            c["field"].endswith(("unit_price", "linear_mass", "profile_id")) for c in comp.get("changed_fields") or [])
            else "本次同时改了材料或单价，差异不能全部归因于分格；") + _why(run))
    rr = run.get("rule_results") or {}
    if rr:
        lines = []
        for role in roles:
            for x in (rr.get(role) or {}).get("rule_results", []):
                lines.append(f"{P.ROLE_NAME[role]} {x.get('title') or x['rule_id']}：{x.get('status_text')}")
        st.caption("演示规则（不是正式规范结论）：" + "；".join(lines[:6]))
    st.markdown(_tags(run), unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1.3, 1.3, 1])
    if c1.button("查看计算依据", key=f"ev-{msg_id}"):
        want_dialog("evidence", run_id=run["run_id"])
        st.rerun()
    pname = (project or {}).get("name") or run.get("project_name") or "项目"
    c2.download_button("下载比选表" if current else "下载此历史版本", data=_xlsx(run["run_id"], current), file_name=f"{pname}_比选表_{run['run_id'][-6:]}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"dx-{msg_id}",
                       on_click=_log_export, args=(run["run_id"], "xlsx"))
    with c3.popover("更多"):
        st.download_button("技术追溯包（ZIP）" if current else "历史版本追溯包", data=_zip(run["run_id"], current), file_name=f"{pname}_追溯包_{run['run_id'][-6:]}.zip",
                           mime="application/zip", key=f"dz-{msg_id}", on_click=_log_export, args=(run["run_id"], "zip"))
        st.caption("包含 Markdown 报告、CSV 对比表、JSON 输入快照与文件清单，供技术复核。")


def _why(run) -> str:
    comp = run.get("comparison") or {}
    d = comp.get("delta_metrics") or {}
    parts = []
    lc, lt = d.get("delta_column_length_m"), d.get("delta_beam_length_m")
    if lc:
        parts.append(f"立柱总长变化 {lc['display_value']} m")
    if lt:
        parts.append(f"横梁总长变化 {lt['display_value']} m")
    return "，".join(parts) + "。结果不含玻璃及其他费用，规格适用性需工程师确认。"


@st.cache_data(max_entries=64, show_spinner=False)
def _xlsx_cached(run_id: str, current: bool, run_json: str) -> bytes:
    return build_xlsx(json.loads(run_json), current=current)


def _xlsx(run_id: str, current: bool) -> bytes:
    ws, _ = services()
    run = ws.get_run(run_id)
    return _xlsx_cached(run_id, current, json.dumps(run, ensure_ascii=False))


@st.cache_data(max_entries=32, show_spinner=False)
def _zip_cached(run_id: str, run_json: str, current: bool = True) -> bytes:
    import io
    import zipfile
    run = json.loads(run_json)
    files = build_artifacts(run)
    version_note = "当前结果" if current else "历史结果：参数已修改，此包为冻结的历史版本。"
    files["report.md"] = (version_note + "\n\n").encode("utf-8") + files["report.md"]
    files["版本说明.txt"] = (version_note + "\n运行：" + run_id + "\n" + SCOPE_STATEMENT).encode("utf-8")
    from domain.contracts import sha256_of
    manifest = {"run_id": run_id, "created_at": now_utc(), "files": [{"path": k, "sha256": sha256_of(v)}
                                                                      for k, v in files.items()]}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def _zip(run_id: str, current: bool = True) -> bytes:
    ws, _ = services()
    return _zip_cached(run_id, json.dumps(ws.get_run(run_id), ensure_ascii=False), current)


def _log_export(run_id: str, kind: str) -> None:
    ws, _ = services()
    run = ws.get_run(run_id) or {}
    ws.save_export({"export_id": new_id("EXP"), "run_id": run_id, "project_id": run.get("project_id"),
                    "request_id": new_id("REQ"), "created_at": now_utc(), "kind": kind})


def query_card(ws, run: dict) -> None:
    rt = run.get("retrieval") or {}
    for c in (rt.get("pinned") or []) + (rt.get("candidates") or []):
        location = c.get("location") or ""
        if str(c.get("source_ref") or "").lower().endswith((".txt", ".md")):
            location = location.replace("页", "分段")
        st.markdown(f"**{_plain_label(c.get('title') or '资料片段')}** · {_plain_label(location)}")
        st.text((c.get("exact_text") or "")[:180] + ("…" if len(c.get("exact_text") or "") > 180 else ""))
        with st.expander("查看完整片段"):
            st.caption(f"{c.get('source_ref')}｜{c.get('qualification')}")
            st.text(c.get("exact_text") or "")


def import_card(ws, asst, conv, msg) -> None:
    pv = (conv.get("imports") or {}).get(msg["card"]["import_id"])
    if pv is None:
        return
    source = pv.get("source") or {}
    st.caption(f"文件：{source.get('filename') or '粘贴内容'} · 工作表：{source.get('sheet') or '默认'}")
    st.info("待确认 · 尚未写入。确认后只导入本次所选工作表，其他表需另行导入。") if not msg.get("consumed") else None
    rows = [{"方案": P.ROLE_NAME[i["role"]], "参数": USER_FIELDS[i["field"]]["label"],
             "识别结果": i.get("display") or f"{i['raw_value']} {i.get('raw_unit') or ''}",
             "状态": "可导入" if i.get("ok") else f"需处理：{i.get('problem')}", "位置": i["location"]}
            for i in pv["items"]]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=min(260, 38 * (len(rows) + 1)))
    if msg.get("consumed"):
        st.caption("已导入 · 当前预览已完成。")
        return
    mapping = {}
    opts = ["ignore"] + list(USER_FIELDS)
    for a in pv["ambiguous"]:
        mapping[a["key"]] = st.selectbox(f"“{a['column']}”（示例值 {a['sample']} {a.get('unit') or ''}）对应：", opts,
                                         format_func=lambda f: "不导入" if f == "ignore" else USER_FIELDS[f]["label"],
                                         key=f"map-{msg['id']}-{a['key']}")
    c1, c2 = st.columns(2)
    mode = c1.radio("写入位置", ["current", "new"], horizontal=True, key=f"mode-{msg['id']}",
                    format_func=lambda m: "更新当前项目" if m == "current" else "新建项目",
                    index=0 if conv.get("project_id") else 1, disabled=not conv.get("project_id"))
    src = c2.radio("这批数据是", ["pending", "synthetic"], horizontal=True, key=f"src-{msg['id']}",
                   format_func=lambda s: "实际资料（来源待核实）" if s == "pending" else "测试／演示数据")
    targets = "、".join(dict.fromkeys(P.ROLE_NAME[i["role"]] for i in pv["items"] if i.get("ok"))) or "原方案"
    project = asst.project_of(conv) if mode == "current" else None
    st.caption(f"写入项目：{(project or {}).get('name') or '新建项目'} · 目标：{targets}")
    if project:
        changes = []
        for item in pv["items"]:
            if item.get("ok") and item["role"] in project["schemes"]:
                old, unit = P.user_value(project, item["role"], item["field"])
                if old is not None:
                    changes.append({"方案": P.ROLE_NAME[item["role"]], "覆盖参数": USER_FIELDS[item["field"]]["label"],
                                    "当前值": f"{old} {unit or ''}", "导入值": item.get("display") or str(item["raw_value"])})
        if changes:
            st.dataframe(pd.DataFrame(changes), hide_index=True, width="stretch")
    if st.button("导入到" + targets, key=f"imp-{msg['id']}", type="primary"):
        try:
            c = asst.apply_import(conv["conversation_id"], msg["id"], pv["import_id"], mapping=mapping, mode=mode,
                                  declared=src)
            open_conv(c["conversation_id"])
        except AssistantError as e:
            st.error(e.message)
        st.rerun()


def render_message(ws, asst, conv, project, msg, latest_ids: set) -> None:
    role = "user" if msg["role"] == "user" else "assistant"
    with st.chat_message(role, avatar=":material/person:" if role == "user" else ":material/architecture:"):
        if msg.get("text") and (msg.get("card") or {}).get("type") != "result":
            st.markdown(msg["text"])
        if msg.get("note"):
            st.caption(msg["note"])
        card = msg.get("card") or {}
        if card.get("type") == "result":
            run = ws.get_run(card["run_id"], (project or {}).get("project_id"))
            if run:
                result_card(ws, run, project, msg["id"], msg["id"] in latest_ids, msg.get("text") or "")
        elif card.get("type") == "query":
            run = ws.get_run(card["run_id"])
            if run:
                query_card(ws, run)
        elif card.get("type") == "standard_qa":
            from ui.standards import citation_card
            run = ws.get_run(card["run_id"])
            if run:
                citation_card(ws, run)
        elif card.get("type") == "import":
            import_card(ws, asst, conv, msg)
        elif card.get("type") == "export":
            run = ws.get_run(card["run_id"], (project or {}).get("project_id"))
            if run:
                cur = engine.is_current(project, run)
                st.download_button("下载比选表" if cur else "下载此历史版本", data=_xlsx(run["run_id"], cur), file_name=f"比选表_{run['run_id'][-6:]}.xlsx",
                                   key=f"dx-{msg['id']}", on_click=_log_export, args=(run["run_id"], "xlsx"))
        elif card.get("type") == "explain":
            if st.button("查看完整计算依据", key=f"ev-{msg['id']}"):
                want_dialog("evidence", run_id=card["run_id"])
                st.rerun()
        acts = [a for a in msg.get("actions") or []]
        if acts and not msg.get("consumed") and msg["id"] in latest_ids:
            if any(a["action"] == "undo" for a in acts) and conv.get("last_change_msg") != msg["id"]:
                acts = [a for a in acts if a["action"] != "undo"]
            cols = st.columns(min(4, max(1, len(acts))))
            for i, a in enumerate(acts):
                button_label = "确认参数并计算" if a["action"] == "confirm_run" else a["label"]
                if cols[i % len(cols)].button(button_label, key=f"act-{a['id']}",
                                             type="primary" if a["action"] == "confirm_run" else "secondary"):
                    if a["action"] == "open_params":
                        want_dialog("params", conv_id=conv["conversation_id"])
                    elif a["action"] == "template":
                        want_dialog("template")
                    elif a["action"] == "open_documents":
                        want_dialog("documents", conv_id=conv["conversation_id"])
                    else:
                        with st.spinner("正在处理…"):
                            r = asst.act(conv["conversation_id"], msg["id"], a["action"], a.get("payload"))
                        if r.get("_deleted"):
                            st.toast(r["message"])
                            open_conv(None)
                    st.rerun()


# ====================================================================== 弹窗：参数（同一草稿）
@st.dialog("方案参数", width="large", dismissible=False)
def params_dialog(conv_id: str | None):
    ws, asst = services()
    conv = ws.get_conversation(conv_id) if conv_id else None
    project = asst.project_of(conv)
    with st.popover("关闭参数", icon=":material/close:"):
        st.write("未保存的填写尚未写入。继续编辑可关闭此菜单；离开请先保存或明确放弃。")
        if st.button("放弃编辑并返回对话", key="params-close-top"):
            st.rerun()
    roles = list(project["schemes"]) if project else ["baseline"]
    tabs = st.tabs([P.ROLE_NAME[r] for r in roles])
    for ti, role in enumerate(roles):
        with tabs[ti], st.form(f"pf-{role}"):
            name = None
            if not project:
                name = st.text_input("项目名称", placeholder="例如：手工测试甲")
            st.caption("清空某一项表示删除该值（不会沿用原方案）。" if role == "candidate" else
                       "只填本次需要的内容；缺的项之后可以在对话中补充。")
            vals = {}
            geo = ["width_m", "height_m", "grid_width_m", "grid_height_m", "floor_height_m", "reported_area_m2"]
            mats = ["column_name", "column_linear_mass", "column_unit_price", "beam_name", "beam_linear_mass",
                    "beam_unit_price"]
            for group, fields in (("尺寸与分格", geo), ("立柱", mats[:3]), ("横梁", mats[3:])):
                st.markdown(f"**{group}**")
                for uf in fields:
                    d = USER_FIELDS[uf]
                    cur, unit = P.user_value(project, role, uf) if project else (None, d["unit"])
                    state = P.field_state(project, role, uf) if project else "missing"
                    c1, c2, c3 = st.columns([2, 2, 1])
                    c1.markdown(f"{d['label']}" + ("　<span class='mw-muted'>沿用原方案</span>" if state == "inherited"
                                                   else ""), unsafe_allow_html=True)
                    v = c2.text_input(d["label"], "" if cur is None else P._fmt(cur), key=f"in-{role}-{uf}",
                                      label_visibility="collapsed")
                    if d["kind"] == "text":
                        u = None
                    elif d["kind"] == "price":
                        u = c3.selectbox("单位", ["元/kg", "元/吨"], key=f"un-{role}-{uf}", label_visibility="collapsed")
                    elif d["kind"] == "length":
                        u = c3.selectbox("单位", ["m", "mm", "cm"], key=f"un-{role}-{uf}", label_visibility="collapsed")
                    else:
                        u = c3.selectbox("单位", [d["unit"]], key=f"un-{role}-{uf}", label_visibility="collapsed")
                    if cur is not None and v.strip() == "":
                        vals[uf] = ("清空", None)
                    elif v.strip() != ("" if cur is None else P._fmt(cur)):
                        vals[uf] = (v.strip(), u)
            basis = P.basis_of(project, role) if project else None
            current_tax = P.project_package(project)["price_bases"].get(basis["price_basis_id"], {}).get("tax_basis") if basis else "exclusive"
            tax = st.radio("报价口径", ["exclusive", "inclusive"], horizontal=True, key=f"tax-{role}",
                           index=1 if current_tax == "inclusive" else 0,
                           format_func=lambda x: "未税" if x == "exclusive" else "含税（不参与金额计算）")
            src = st.radio("本次填写的数据是", ["", "synthetic", "pending"], horizontal=True, key=f"src-{role}",
                           format_func=lambda x: {"": "沿用项目设置", "synthetic": "测试／演示数据",
                                                  "pending": "实际资料（来源待核实）"}[x])
            if st.form_submit_button("保存参数", type="primary"):
                cur_tax = None
                if project:
                    b = P.basis_of(project, role)
                    cur_tax = P.project_package(project)["price_bases"].get(b["price_basis_id"], {}).get("tax_basis") \
                        if b else None
                tax_change = tax if (cur_tax and tax != cur_tax) or (vals and any(k.endswith("unit_price")
                                                                                  for k in vals)) else None
                if conv is None:
                    conv = asst.new_conversation()
                c = asst.save_form(conv["conversation_id"], role, vals, project_name=name or None,
                                   declared=src or None, tax=tax_change)
                open_conv(c["conversation_id"])
                st.rerun()
    st.caption("关闭前请保存参数。未保存的填写不会写入项目。")
    if st.button("放弃未保存修改并关闭"):
        st.rerun()
    if project:
        a, b = st.columns(2)
        if a.button("项目高级设置", icon=":material/settings:"):
            want_dialog("project", conv_id=conv_id)
            st.rerun()
        if b.button("选用或保存材料", icon=":material/inventory_2:"):
            want_dialog("materials", conv_id=conv_id)
            st.rerun()


@st.dialog("项目高级设置", width="large")
def project_dialog(conv_id):
    ws, asst = services()
    conv = ws.get_conversation(conv_id)
    project = asst.project_of(conv)
    if not project:
        st.info("当前没有关联项目。")
        return
    with st.expander("演示规则"):
        rules = project.get("rules") or {}
        st.caption("这里只能建立演示规则，用于验证核查流程；它不是正式规范条款，结果不会写成合规结论。正式条款 0 条。")
        for rid, r in rules.items():
            c1, c2 = st.columns([5, 1])
            p_ = r["predicate"]
            c1.markdown(f"{r['title']}：`{p_['lhs']} {p_['operator']} {p_['threshold']['value']} {p_['threshold']['unit']}`")
            if c2.button("删除", key=f"dr-{rid}"):
                asst.delete_rule(project["project_id"], rid)
                st.rerun()
        with st.form("rule-add"):
            lhs_opts = {"grid_width_m": ("分格宽", "m"), "grid_height_m": ("分格高", "m"),
                        "panel_area_m2": ("板块面积", "m²"), "floor_height_m": ("层高", "m"),
                        "width_m": ("幕墙总宽", "m"), "height_m": ("幕墙总高", "m")}
            title = st.text_input("规则名称", placeholder="例如：分格宽演示条件")
            c1, c2, c3 = st.columns(3)
            lhs = c1.selectbox("参数", list(lhs_opts), format_func=lambda k: lhs_opts[k][0])
            op = c2.selectbox("条件", ["<=", "<", ">=", ">"])
            th = c3.text_input(f"阈值（{lhs_opts[lhs][1]}）")
            if st.form_submit_button("添加演示规则"):
                try:
                    from domain.contracts import dec
                    dec(th)
                    asst.add_rule(project["project_id"], title=title or f"{lhs_opts[lhs][0]}演示条件", lhs=lhs,
                                  operator=op, threshold=th, unit=lhs_opts[lhs][1])
                    st.rerun()
                except ValueError:
                    st.error("阈值必须是数字")
    with st.expander("项目名称与管理", expanded=True):
        new = st.text_input("项目名称", project["name"])
        if st.button("保存项目名称"):
            asst.rename_project(project["project_id"], new)
            st.rerun()
        src = "演示数据" if project.get("default_source") == "synthetic" else "来源待核实（未声明时的默认）"
        st.caption(f"项目默认数据性质：{src}。每个字段仍单独记录来源；确认参数用于计算不等于专业复核。")


    if st.button("将项目移入回收站", icon=":material/delete:"):
        want_dialog("delete_project", project_id=project["project_id"])
        st.rerun()


# ====================================================================== 弹窗：计算依据
@st.dialog("计算依据", width="large")
def evidence_dialog(run_id: str):
    ws, _ = services()
    run = ws.get_run(run_id)
    if run is None:
        st.info("结果不存在或已删除。")
        return
    project = ws.get_project(run.get("project_id"))
    if not engine.is_current(project, run):
        st.markdown('<div class="mw-stale">这是历史结果：之后参数已修改。</div>', unsafe_allow_html=True)
    st.caption(f"{run.get('project_name') or ''}｜计算时间 {run['started_at']}｜{run.get('completeness_text')}")
    t1, t2, t3, t4 = st.tabs(["计算步骤", "输入与来源", "规则核查", "技术详情"])
    with t1:
        for role in ("baseline", "candidate"):
            res = (run.get("results") or {}).get(role)
            if not res:
                continue
            st.markdown(f"**{P.ROLE_NAME[role]}**")
            for mod in ("geometry", "length", "mass", "cost"):
                for s in (res.get(mod) or {}).get("steps", []):
                    st.markdown(f"- {metric_label(s['step_id'].split(':')[-1])}：{s['expression']} = "
                                f"**{s['display_value']}** {s['unit']}　<span class='mw-muted'>{s['formula_id']}</span>",
                                unsafe_allow_html=True)
            gaps = [i["message"] for m in ("length", "mass", "cost") for i in (run.get("module_issues") or {}).get(m, [])
                    if i.get("blocking", True)]
            for g in dict.fromkeys(gaps):
                st.caption("未计算：" + g)
        comp = run.get("comparison") or {}
        if comp.get("steps"):
            st.markdown("**差值**（调整方案 − 原方案）")
            for s in comp["steps"]:
                if any(k in s["step_id"] for k in ("total_cost", "total_mass", "column_length", "beam_length", "percent")):
                    st.markdown(f"- {s['expression']} = **{s['display_value']}** {s['unit']}")
        st.caption(SCOPE_STATEMENT)
    with t2:
        prs = param_rows(run)
        if prs:
            st.dataframe(pd.DataFrame(prs), hide_index=True, width="stretch")
        for role in ("baseline", "candidate"):
            s = (run.get("snapshots") or {}).get(role)
            if not s:
                continue
            for comp_key in ("column", "beam"):
                pf = (s.get("resolved_profiles") or {}).get(comp_key) or {}
                pr = (s.get("resolved_prices") or {}).get(comp_key) or {}
                st.markdown(f"- {P.ROLE_NAME[role]}{P.COMP_NAME[comp_key]}：{pf.get('profile_name', '未填写')}，线重 "
                            f"{pf.get('linear_mass') or '缺失'} kg/m，单价 "
                            f"{pr.get('unit_price') if pr.get('unit_price') is not None else '缺失'} 元/kg"
                            f"（{'未税' if pr.get('tax_basis') == 'exclusive' else pr.get('tax_basis') or '—'}）")
        st.markdown("**来源记录**")
        provs = {}
        for s in (run.get("snapshots") or {}).values():
            provs.update(s.get("provenance") or {})
        for pid, p in sorted(provs.items()):
            st.markdown(f"- {SOURCE_TEXT.get(p.get('source_status'), p.get('source_status'))}｜"
                        f"{REVIEW_TEXT.get(p.get('review_status'), p.get('review_status'))}｜{p.get('source_ref')}")
        st.caption("计算结果：程序计算，待专业复核。确认参数用于本次计算，不代表工程审核。")
    with t3:
        rr = run.get("rule_results") or {}
        if not rr:
            st.markdown("本项目没有配置核查规则；正式条款 0 条，未执行正式规范核查。可在“项目高级设置 → 演示规则”中建立演示规则。")
        for role, data in rr.items():
            st.markdown(f"**{P.ROLE_NAME[role]}**")
            for x in data.get("rule_results", []):
                st.markdown(f"- {x.get('title')}：{x.get('status_text')} —— {x.get('reason')}")
        st.caption("演示规则只验证流程；任何状态都不汇总为“全部合规”，也不代表结构安全。")
    with t4:
        st.markdown(f"- 运行编号：`{run['run_id']}`\n- 请求编号：`{run['request_id']}`\n- 输入哈希：`{run['input_hash'][:24]}`\n"
                    f"- 项目数据版本：`{run.get('dataset_version_id')}`\n- 计量模型：`{run.get('measurement_model')}`\n"
                    f"- 工具版本：{', '.join(f'{k}={v}' for k, v in (run.get('tool_versions') or {}).items())}")
        for role, s in (run.get("snapshots") or {}).items():
            st.markdown(f"- {P.ROLE_NAME[role]}快照：`{s['snapshot_id']}`（哈希 `{s['content_hash'][:16]}`）")
        with st.expander("运行记录 JSON"):
            st.json({k: run.get(k) for k in ("module_statuses", "module_issues", "coverage", "source_statuses",
                                              "scheme_revs", "data_rev")}, expanded=False)


# ====================================================================== 弹窗：设置
@st.dialog("资料库", width="large")
def documents_dialog(conv_id=None):
    ws, asst = services()
    docs = ws.list_documents()
    st.caption("资料用于查阅。来源、版本与适用性待核实，不作为项目合规结论。")
    if st.button("管理规范库 / 导入OCR成果包", icon=":material/library_books:"):
        want_dialog("standards")
        st.rerun()
    up = st.file_uploader("上传 PDF / TXT / MD", type=["pdf", "txt", "md"], accept_multiple_files=True, key="docs-up")
    if st.button("加入资料库", disabled=not up, type="primary", icon=":material/upload_file:"):
        for f in up:
            with st.spinner(f"正在读取 {f.name}…"):
                result = documents.add_document(ws, f.name, f.getvalue())
            if result["ok"]:
                st.success(f"{f.name} · " + ("已在资料库，未重复加入" if result["duplicate"] else "已入库"))
            else:
                st.error(f"{f.name} · 未入库：{result['error']}。请选择其他文件重试。")
        docs = ws.list_documents()
    conv = ws.get_conversation(conv_id) if conv_id else None
    if conv and conv.get("pending_query") and docs:
        st.caption("已保留的问题：" + conv["pending_query"])
        if st.button("用原问题检索", icon=":material/search:"):
            asst.send(conv_id, "查阅资料：" + conv["pending_query"])
            st.rerun()
    if not docs:
        st.info("资料库为空。上传第一份资料后，即可在对话中提问。")
    for d in docs:
        with st.container(border=True):
            cols = st.columns([5, 1])
            cols[0].write(d["filename"])
            unit = "页" if d["filename"].lower().endswith(".pdf") else "分段"
            state = "可检索" if d["text_pages"] else "未提取到足够文本，需核对文件"
            cols[0].caption(f"{state} · {d['page_count']} {unit} / {d['text_pages']} 可检索 · 来源待核实 · 未审核")
            cols[0].caption("上传于 " + _display_time(d["created_at"]))
            with cols[1].popover("管理", icon=":material/more_horiz:"):
                st.write(f"将“{d['filename']}”移入回收站？")
                if st.button("移入回收站", key=f"dd-{d['doc_id']}"):
                    ws.set_document_status(d["doc_id"], "trashed")
                    st.toast("资料已移入回收站")
                    st.rerun()
    with st.expander("从本机文件夹导入"):
        folder = st.text_input("资料文件夹路径", placeholder="填写本机资料目录")
        if folder and st.button("导入文件夹"):
            with st.spinner("正在读取资料…"):
                r = documents.add_folder(ws, folder)
            if r["ok"]:
                st.success(f"新增 {r.get('added', 0)} 份，重复 {r.get('duplicates', 0)} 份")
            else:
                st.error(r["error"])
            for item in r.get("failed") or []:
                st.caption(item)


@st.dialog("材料库", width="large")
def materials_dialog(conv_id=None):
    ws, asst = services()
    conv = ws.get_conversation(conv_id) if conv_id else None
    project = asst.project_of(conv)
    st.caption("选用材料会复制到当前项目；之后修改项目或材料库，互不联动。")
    lib = ws.list_library()
    if project:
        role = st.radio("复制到哪个方案", list(project["schemes"]), horizontal=True,
                        format_func=lambda r: P.ROLE_NAME[r], key="lib-role")
        with st.expander("把当前项目材料存入材料库"):
            for comp in ("column", "beam"):
                m = P.material_of(project, role, comp)
                if m and m.get("linear_mass") is not None:
                    if st.button(f"存入{P.COMP_NAME[comp]} · {m['profile_name']}", key=f"sl-{role}-{comp}"):
                        asst.save_to_library(project["project_id"], role, comp)
                        st.toast("已存入材料库")
                        st.rerun()
    if not lib:
        st.info("材料库为空。在项目中填写线重与报价后，可将材料存入这里。")
    for x in lib:
        with st.container(border=True):
            st.write(f"{P.COMP_NAME[x['component_type']]} · {x['profile_name']}")
            price = x.get("unit_price")
            st.caption(f"线重 {x['linear_mass']} kg/m · " + (f"单价 {price} 元/kg" if price is not None else "缺单价，金额未计算")
                       + " · " + ("未税" if x.get("tax_basis") == "exclusive" else "含税" if x.get("tax_basis") == "inclusive" else "税费口径待确认"))
            st.caption(SOURCE_TEXT.get(x.get("source_status"), "来源待核实"))
            if project and st.button(f"复制到{P.ROLE_NAME[role]}", key="use-" + x["mat_id"]):
                asst.use_library_material(conv_id, role, x["mat_id"])
                st.toast("已复制到当前项目")
                st.rerun()
            with st.popover("管理材料", key="manage-" + x["mat_id"]):
                st.write("从个人材料库删除；已复制到项目的材料保留。")
                if st.button("确认删除", key="dl-" + x["mat_id"]):
                    ws.delete_library_material(x["mat_id"])
                    st.rerun()


@st.dialog("设置", width="large")
def settings_dialog():
    ws, asst = services()
    appearance, model, workspace_tab, maintenance = st.tabs(["外观与阅读", "模型连接", "工作区管理", "高级维护"])
    with appearance:
        st.caption("浅色 · 液态玻璃。阅读偏好保存在当前浏览器会话中。")
        preferences = st.session_state.get("reading_preferences", {})
        transparent = st.toggle("降低透明度", value=preferences.get("reduce_transparency", False), key="reduce_transparency")
        motion = st.toggle("减少动态效果", value=preferences.get("reduce_motion", False), key="reduce_motion")
        larger = st.toggle("增大正文字号", value=preferences.get("large_text", False), key="large_text")
        st.caption("系统“减少动态效果”偏好也会自动生效。")
        if st.button("应用阅读偏好", type="primary"):
            st.session_state["reading_preferences"] = dict(reduce_transparency=transparent, reduce_motion=motion, large_text=larger)
            st.rerun()
    with model:
        ms = asst.model_status()
        st.markdown(f"在线理解模型：**{ms['label']}**　{'已配置' if ms['configured'] else '未配置（使用本地解析）'}")
        if ms.get("ok") is True:
            st.markdown(f"最近一次真实请求成功（{ms.get('at')}，{ms.get('duration_ms')} ms）。")
        elif ms.get("ok") is False:
            st.markdown(f"最近一次请求失败（{ms.get('at')}）：{ms.get('detail')}。仍可使用表单和本地解析。")
        else:
            st.markdown("尚未发起过在线请求。")
        st.caption("密钥写在 .streamlit/secrets.toml（DEEPSEEK_API_KEY）。启用后用户输入会发送给模型服务商；接入真实工程数据前请确认保密要求。")
    with workspace_tab:
        with st.expander("回收站", expanded=True):
            tp = ws.list_projects("trashed")
            tc = [c for c in ws.list_conversations("trashed") if not c.get("trashed_with_project")]
            td = ws.list_documents("trashed")
            if not (tp or tc or td):
                st.markdown("回收站为空。")
            for p in tp:
                imp = ws.project_impact(p["project_id"])
                st.markdown(f"**项目**　{p['name']}　<span class='mw-muted'>{imp['conversations']} 个对话、{imp['runs']} 次计算、"
                            f"{imp['attachments']} 个附件</span>", unsafe_allow_html=True)
                c1, c2, c3 = st.columns([1, 2, 1])
                if c1.button("恢复", key=f"rp-{p['project_id']}"):
                    ws.restore_project(p["project_id"])
                    st.rerun()
                sure = c2.checkbox("我确认彻底清理，无法恢复", key=f"sp-{p['project_id']}")
                if c3.button("彻底清理", key=f"pp-{p['project_id']}", disabled=not sure):
                    r = ws.purge_project(p["project_id"])
                    msg = "已彻底清理" + (f"；未完成：{'；'.join(r['problems'])}" if r["problems"] else "")
                    st.toast(msg)
                    st.rerun()
            for c in tc:
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(f"**对话**　{_plain_label(c['title'])}")
                if c2.button("恢复", key=f"rc-{c['conversation_id']}"):
                    try:
                        ws.restore_conversation(c["conversation_id"])
                    except StoreError as e:
                        st.error(e.message)
                    st.rerun()
                confirmed = c1.checkbox("确认永久删除此对话", key=f"pc-confirm-{c['conversation_id']}")
                if c3.button("彻底删除", key=f"pc-{c['conversation_id']}", disabled=not confirmed):
                    ws.purge_conversation(c["conversation_id"])
                    st.rerun()
            for d in td:
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(f"**资料**　{d['title']}")
                if c2.button("恢复", key=f"rd-{d['doc_id']}"):
                    ws.set_document_status(d["doc_id"], "active")
                    st.rerun()
                confirmed = c1.checkbox("确认永久删除此资料", key=f"pd-confirm-{d['doc_id']}")
                if c3.button("彻底删除", key=f"pd-{d['doc_id']}", disabled=not confirmed):
                    ws.purge_document(d["doc_id"])
                    st.rerun()
            st.caption("已下载到电脑的报告和外部备份不在应用内清理范围。")
        with st.expander("工作区统计与清理"):
            cnt = ws.counts()
            st.markdown(f"项目 {cnt['projects']}（回收站 {cnt['projects_trashed']}）、对话 {cnt['conversations']}、"
                        f"计算结果 {cnt['runs']}、资料 {cnt['documents']}、材料库 {cnt['library_materials']}。")
            collections = ws.standard_library().list_collections()
            st.caption(f"独立规范库：{len(collections)} 个资料集，{sum(len(ws.standard_library().documents(c['collection_id'])) for c in collections)} 份登记资料。")
            st.caption("清空同时删除规范库登记、索引与导入报告；外部原始PDF和OCR成果保持不变。")
            st.markdown("清空工作区会删除以上全部业务数据和附件（保留应用设置与模型连接）。清空后重启仍保持空白，不会装回任何演示数据。")
            typed = st.text_input("输入“清空”以确认")
            if st.button("清空工作区", disabled=typed.strip() != "清空", type="primary"):
                r = ws.clear_workspace()
                st.session_state.clear()
                st.toast("工作区已清空" + (f"；未完成：{'；'.join(r['problems'])}" if r["problems"] else ""))
                st.rerun()
            st.caption("已下载的报告和外部备份不在清理范围内。")
    with maintenance:
        st.download_button("下载空白 Excel 模板", importer.template_xlsx(), file_name="幕墙方案_空白模板.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption("模板不含示例数值。上传时系统会显示识别结果，确认后才写入。")
        up = st.file_uploader("导入技术 JSON 数据包（维护用途）", type=["json"], key="pkg-up")
        if up and st.button("导入为项目"):
            r = import_package(ws, up.getvalue())
            if r["ok"]:
                st.success("已建立项目：" + "、".join(r["projects"]))
            else:
                st.error("数据包校验未通过：" + "；".join(f"{e['location']} {e['message']}" for e in r["errors"][:5]))
        mp = migration.pending(ws)
        if mp:
            st.markdown(f"旧版数据：{mp['drafts']} 个草稿、{mp['runs']} 次计算（{mp['path']}）")


@st.dialog("删除")
def delete_dialog(kind: str, **kw):
    ws, asst = services()
    if kind == "delete_project":
        p = ws.get_project(kw["project_id"])
        if p is None:
            st.info("项目不存在。")
            return
        imp = ws.project_impact(p["project_id"])
        st.markdown(f"把项目“{p['name']}”移入回收站？将一起移入：{imp['conversations']} 个对话、{imp['schemes']} 套方案、"
                    f"{imp['materials']} 条项目材料、{imp['runs']} 次计算结果、{imp['attachments']} 个附件。"
                    "个人材料库与资料库不受影响。可在“设置 → 工作区管理 → 回收站”恢复或彻底清理。")
        if st.button("移入回收站", type="primary"):
            ws.trash_project(p["project_id"])
            conv = ws.get_conversation(st.session_state.get("conv_id"))
            if conv is None:
                open_conv(None)
            st.rerun()
    else:
        c = ws.get_conversation(kw["conv_id"])
        if c is None:
            st.info("对话不存在。")
            return
        keep = "其所属项目及方案、结果会保留，可在左栏“没有对话的项目”中重新打开。" if c.get("project_id") else ""
        st.markdown(f"把对话“{c['title']}”移入回收站？{keep}")
        if st.button("移入回收站", type="primary"):
            ws.trash_conversation(c["conversation_id"])
            if st.session_state.get("conv_id") == c["conversation_id"]:
                open_conv(None)
            st.rerun()


@st.dialog("空白模板")
def template_dialog():
    st.download_button("下载空白 Excel 模板", importer.template_xlsx(), file_name="幕墙方案_空白模板.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption("两张表：方案参数（参数名称、数值、单位）和材料报价（用途、名称、线重、单价、价格单位），不含示例数值。")


# ====================================================================== 主程序
def _spinner_text(text: str, files) -> str:
    if files:
        return "正在读取文件…"
    if any(k in text for k in ("资料", "规范", "条文", "查找", "查询")):
        return "正在查找资料…"
    if any(k in text for k in ("算", "比较", "对比")):
        return "正在整理参数并计算…"
    return "正在整理参数…"


def main() -> None:
    ws, asst = services()
    conv = current_conv(ws)
    st.set_page_config(page_title=f"{conv['title']} · 幕墙方案助手" if conv else "幕墙方案助手",
                       page_icon=":material/architecture:", layout="wide", initial_sidebar_state="auto")
    from ui import style
    style.inject()
    sidebar(ws, asst, conv)
    project = asst.project_of(conv)
    standard_mode = bool(conv and conv.get("mode") == "standard_qa")
    header = st.empty()
    with header.container():
        conversation_header(ws, asst, conv)
    if standard_mode:
        from ui.standards import controls
        conv = controls(ws, asst, conv)
    if not standard_mode and (conv is None or not conv.get("messages")):
        home(ws)
    else:
        msgs = conv["messages"]
        latest_ids = {m["id"] for m in msgs[-3:]}
        for m in msgs:
            render_message(ws, asst, conv, project, m, latest_ids)
    placeholder = HINTS.get(st.session_state.get("hint")) or PLACEHOLDER
    if standard_mode:
        placeholder = "输入标准编号、条文号或中文问题，例如 JGJ102-2003 5.2.1"
    if st.session_state.get("submission_error"):
        st.error(st.session_state["submission_error"])
    with st.bottom:
        sub = st.chat_input(placeholder, accept_file=False if standard_mode else "multiple", file_type=FILE_TYPES, key="composer")
        with st.container(horizontal=True, vertical_alignment="center", key="composer-tools"):
            st.caption("Enter 发送 · Shift+Enter 换行 · 原始PDF为复核依据" if standard_mode else "Enter 发送 · Shift+Enter 换行 · 表格导入 / PDF、TXT、MD 资料查阅")
            if not standard_mode:
                st.download_button("空白模板", importer.template_xlsx(), file_name="幕墙方案_空白模板.xlsx",
                               icon=":material/download:", key="composer-template", type="tertiary")
    if sub:
        text = sub.text if hasattr(sub, "text") else str(sub)
        files = [(f.name, f.getvalue()) for f in (getattr(sub, "files", None) or [])]
        if not (text or "").strip() and not files:
            st.rerun()
        if conv is None:
            conv = asst.new_conversation()
            open_conv(conv["conversation_id"])
        def started(saved):
            header.markdown(f'<div class="mw-title">{escape(saved["title"])}</div>', unsafe_allow_html=True)
            st.caption("消息已保存 · 正在处理，可等待本轮完成。")
        with st.status(_spinner_text(text or "", files), expanded=True):
            try:
                asst.send(conv["conversation_id"], text or "", files, request_id=new_id("UI"), on_started=started)
            except (AssistantError, StoreError) as e:
                st.session_state["submission_error"] = getattr(e, "message", str(e)) + "。本轮输入已保留，可在对话中继续处理。"
            except Exception as e:
                ws.log("ui_request_error", {"type": type(e).__name__}, conversation_id=conv["conversation_id"])
                st.session_state["submission_error"] = "本轮处理未完成，已保留用户输入。请检查模型连接，或通过方案参数继续填写。"
        st.session_state.pop("hint", None)
        st.rerun()
    dlg = st.session_state.pop("dialog", None)
    if dlg:
        name, kw = dlg
        if name == "params":
            params_dialog(kw.get("conv_id"))
        elif name == "evidence":
            evidence_dialog(kw["run_id"])
        elif name == "settings":
            settings_dialog()
        elif name == "documents":
            documents_dialog(kw.get("conv_id"))
        elif name == "standards":
            from ui.standards import library_dialog
            library_dialog(ws)
        elif name == "standard_original":
            from ui.standards import original_dialog
            original_dialog(ws, **kw)
        elif name == "materials":
            materials_dialog(kw.get("conv_id"))
        elif name == "project":
            project_dialog(kw.get("conv_id"))
        elif name in ("delete_project", "delete_conversation"):
            delete_dialog(name, **kw)
        elif name == "template":
            template_dialog()


_ = (RuleStatus, RULE_STATUS_TEXT)
