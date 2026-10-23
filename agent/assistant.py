"""会话编排：一个输入框完成建项目、录入／修改参数、补参、确认、计算、解释、查资料、导出与删除请求。

原则（改版方案 §5）：
- 每个对话绑定明确的项目；找不到就是空态，不取其他项目兜底。
- 明确的录入或更正直接更新草稿并回显，可撤销；真正歧义才追问，一次只问一个。
- 首次计算集中确认一次关键参数与计量口径；之后无歧义的改参在需要时直接重算。
- 缺项只阻断依赖它的结果，能算的部分先给出；空值不当作 0。
- 数值全部来自确定性工具；模型只做语言理解与受校验的解释。删除、清空只能经确认卡执行。
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal
from pathlib import Path

from domain.contracts import Code, GenerationMethod, calc, dec, dstr, new_id, now_utc
from domain.fields import USER_FIELDS
from domain.units import conversion_note, parse_quantity
from reporting.report import all_metrics, check_model_text, fmt, model_explanation, template_explanation
from storage.workspace import StoreError, Workspace
from storage.titles import auto_title, backfill_title

from . import documents, engine, extraction, importer, parser, planner
from . import projects as P

TABLE_EXT = (".xlsx", ".xlsm", ".xls", ".csv")
DOC_EXT = (".pdf", ".txt", ".md", ".doc", ".docx")
HELP_TEXT = ("我可以帮你：\n"
             "1. **建立方案**：描述幕墙总宽、总高、分格宽高（矩形等距构件式玻璃幕墙），也可以粘贴表格或上传 Excel；\n"
             "2. **计算**立面面积、立柱横梁理论长度、型材质量和材料金额（需要线重和未税单价）；\n"
             "3. **比较**原方案与调整方案，例如“把分格宽改为 1.5m，与原方案比较”，并解释差异；\n"
             "4. **查找资料**：在你上传的 PDF/TXT 资料中检索片段（资料未经审核，仅供查阅）。\n\n"
             "结果只是立柱横梁的理论材料金额，不含玻璃、损耗与安装，不作结构安全验算或工程签审。")
TASK_BUTTONS = [("计算面积和长度", "length"), ("计算型材用量", "mass"), ("计算材料金额", "cost")]


class AssistantError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Turn:
    def __init__(self, ws: Workspace, conv: dict):
        self.ws = ws
        self.conv = conv
        self.conv_rev = conv["rev"]
        self.project = ws.get_project(conv.get("project_id")) if conv.get("project_id") else None
        self.proj_rev = self.project["rev"] if self.project else None
        self.project_dirty = False
        self.reset_conversation = False


class Assistant:
    def __init__(self, ws: Workspace):
        self.ws = ws

    # ================================================================ 对话与项目
    def new_conversation(self, project_id: str | None = None, *, mode: str = "project") -> dict:
        if mode not in ("project", "standard_qa"):
            raise AssistantError(Code.VALUE_INVALID, "未知会话模式")
        if mode == "standard_qa":
            project_id = None
        if project_id and not self.ws.get_project(project_id):
            raise AssistantError(Code.STALE_DRAFT, "项目不存在或已删除")
        p = self.ws.get_project(project_id) if project_id else None
        conv = {"conversation_id": new_id("CONV"), "project_id": project_id,
                "title": p["name"] if p else "新对话", "title_source": "auto" if p else "default",
                "messages": [], "pending": None, "task": None,
                "skipped": [], "last_field": None, "last_run_id": None, "pending_query": None,
                "last_change_msg": None, "model_mode": "auto", "seq": 0, "mode": mode}
        if mode == "standard_qa":
            conv["qa_options"] = {"collection_ids": [], "standards": [], "versions": [], "categories": [],
                                  "answer_mode": "source", "allow_model": False}
        if p:
            conv["messages"].append(self._msg(conv, "assistant", f"已打开项目“{p['name']}”。可以继续修改参数或要求计算。",
                                              kind="info"))
        return self.ws.create_conversation(conv)

    def conversation(self, conv_id: str) -> dict | None:
        return self.ws.get_conversation(conv_id)

    def project_of(self, conv: dict | None) -> dict | None:
        return self.ws.get_project(conv.get("project_id")) if conv and conv.get("project_id") else None

    def rename_conversation(self, conv_id: str, title: str) -> None:
        c = self.ws.get_conversation(conv_id)
        if not title.strip() or len(title.strip()) > 40:
            raise AssistantError(Code.VALUE_INVALID, "对话名称请填写 1–40 个字符。")
        if c:
            c["title"] = " ".join(title.split())
            c["title_source"] = "manual"
            self.ws.save_conversation(c, c["rev"])

    def rename_project(self, project_id: str, name: str) -> None:
        p = self.ws.get_project(project_id)
        if p and name.strip():
            p["name"] = name.strip()[:40]
            self.ws.save_project(p, p["rev"])

    def model_status(self) -> dict:
        st = self.ws.get_setting("model_status") or {}
        return {"configured": extraction.model_available(), "label": extraction.model_label(), **st}

    # ================================================================ 消息工具
    @staticmethod
    def _msg(conv, role, text, *, kind="text", card=None, actions=None, note=None) -> dict:
        conv["seq"] = conv.get("seq", 0) + 1
        mid = f"m{conv['seq']}"
        acts = [dict(a, id=f"{mid}a{i}") for i, a in enumerate(actions or [])]
        return {"id": mid, "role": role, "text": text, "kind": kind, "card": card, "actions": acts, "note": note,
                "at": now_utc(), "consumed": False}

    def _say(self, t: _Turn, text: str, **kw) -> dict:
        m = self._msg(t.conv, "assistant", text, **kw)
        t.conv["messages"].append(m)
        return m

    def _commit(self, t: _Turn) -> dict:
        if t.project is not None and t.project_dirty:
            t.project = self.ws.save_project(t.project, t.proj_rev)
            t.proj_rev = t.project["rev"]
        backfill_title(t.conv)
        try:
            return self.ws.save_conversation(t.conv, t.conv_rev)
        except StoreError:
            # 被删除（例如本轮把项目移入回收站）时不再保存
            return t.conv

    def _ensure_project(self, t: _Turn, name: str | None) -> dict:
        if t.project is None:
            proj = P.new_project(name or "未命名项目")
            proj = self.ws.create_project(proj)
            t.project, t.proj_rev = proj, proj["rev"]
            t.created_project = True
            t.conv["project_id"] = proj["project_id"]
            if t.conv.get("title_source") != "manual" and t.conv.get("title") in (None, "新对话"):
                t.conv.update(title=proj["name"], title_source="auto")
            self.ws.log("project_created", {"name": proj["name"]}, project_id=proj["project_id"],
                        conversation_id=t.conv["conversation_id"])
        return t.project

    def _consume(self, t: _Turn, msg_id: str) -> None:
        for m in t.conv["messages"]:
            if m["id"] == msg_id:
                m["consumed"] = True

    # ================================================================ 入口：发送一条消息
    def send(self, conv_id: str, text: str, files: list[tuple[str, bytes]] | None = None,
             *, request_id: str | None = None, on_started=None) -> dict:
        conv = self.ws.get_conversation(conv_id)
        if conv is None:
            raise AssistantError(Code.STALE_DRAFT, "对话不存在或已删除")
        if request_id and any(m.get("request_id") == request_id for m in conv["messages"]):
            return conv
        text = (text or "").strip()
        names = [f[0] for f in files or []]
        umsg = self._msg(conv, "user", text or "", note=("附件：" + "、".join(names)) if names else None)
        umsg.update(filenames=names, request_id=request_id)
        conv["messages"].append(umsg)
        # 会话身份独立于项目：首条有效输入（含仅附件）就生成主题，之后尊重手动改名。
        if conv.get("title_source") != "manual" and conv.get("title") in ("新对话", None):
            conv.update(title=auto_title(text, names), title_source="auto")
        # 在耗时理解之前保存消息与名称。即使服务失败，刷新也能看到本轮输入。
        conv = self.ws.save_conversation(conv, conv["rev"])
        t = _Turn(self.ws, conv)
        if on_started:
            on_started(conv)
        try:
            if conv.get("mode") == "standard_qa":
                if files:
                    self._say(t, "规范问答的资料请通过“管理规范库 → 导入OCR成果包”登记；本次附件未写入项目。", kind="info")
                self._standard_query(t, text, umsg["id"])
            elif files:
                for fname, data in files:
                    self._handle_file(t, fname, data, umsg["id"])
                if text and not importer.looks_like_table(text):
                    self._handle_text(t, text, umsg["id"])
            elif text and importer.looks_like_table(text):
                self._handle_paste(t, text)
            elif text:
                self._handle_text(t, text, umsg["id"])
        except AssistantError as e:
            self._say(t, e.message, kind="error")
        return self._commit(t)

    def set_qa_options(self, conv_id: str, options: dict) -> dict:
        conv = self.ws.get_conversation(conv_id)
        if not conv or conv.get("mode") != "standard_qa":
            raise AssistantError(Code.VALUE_INVALID, "仅规范问答会话可设置资料范围")
        keys = ("collection_ids", "standards", "versions", "categories", "answer_mode", "allow_model")
        conv["qa_options"] = {k: options[k] for k in keys if k in options}
        return self.ws.save_conversation(conv, conv["rev"])

    def _standard_query(self, t: _Turn, text: str, msg_id: str):
        run = engine.run_standard_query(self.ws, query_text=text, conversation_id=t.conv["conversation_id"],
                                       request_id=f"{t.conv['conversation_id']}:{msg_id}:standard_qa",
                                       options=t.conv.get("qa_options") or {})
        t.conv["last_run_id"] = run["run_id"]
        self._say(t, run["answer"]["text"], kind="query", card={"type": "standard_qa", "run_id": run["run_id"]},
                  note=run["answer"]["notice"])

    # ================================================================ 文本
    def _context(self, t: _Turn) -> dict:
        pend = t.conv.get("pending") or {}

        active = pend.get("field") if pend.get("type") == "question" else None
        ctx = {"active_field": active, "last_field": t.conv.get("last_field"), "model_context": {}}
        mc = ctx["model_context"]
        p = t.project
        mc["project"] = p["name"] if p else None
        if p:
            mc["schemes"] = {P.ROLE_NAME[r]: dict(P.summary_rows(p, r)) for r in p["schemes"]}
        mc["pending_question"] = pend.get("text") if pend.get("type") == "question" else None
        mc["recent_messages"] = [{"role": m["role"], "text": (m["text"] or "")[:160]}
                                 for m in t.conv["messages"][-7:-1] if m.get("text")]
        run = self.ws.get_run(t.conv.get("last_run_id"), (p or {}).get("project_id"))
        if run and run.get("requested_task") != "query":
            mc["last_result"] = self._conclusion(run)
        return ctx

    def _handle_text(self, t: _Turn, text: str, msg_id: str) -> None:
        und = extraction.understand(text, self._context(t), t.conv.get("model_mode", "auto"))
        rec = und.get("model_record") or {}
        if rec.get("ok") is not None:
            self.ws.set_setting("model_status", {"ok": rec["ok"], "at": now_utc(), "model": rec.get("model"),
                                                 "detail": rec.get("error") or "最近一次请求成功",
                                                 "duration_ms": rec.get("duration_ms")})
        self.ws.log("understand", {"text": text[:300], "mode": und["model_mode"],
                                   "intents": und["intents"], "task": und["task"],
                                   "patches": [(p["field"], p["op"], p["value"], p["unit"]) for p in und["patches"]],
                                   "model_record": {k: v for k, v in rec.items() if k != "raw_output"}},
                    project_id=(t.project or {}).get("project_id"), conversation_id=t.conv["conversation_id"])
        note = None
        if und["model_mode"] == "fallback" and rec.get("ok") is False:
            note = "在线理解暂不可用，本轮使用本地解析；你也可以点“查看参数”用表单填写。"
        intents = set(und["intents"])
        pend = t.conv.get("pending") or {}

        # 资料查阅优先承接要求查询；明确判断当前方案安全时仍走能力边界。
        if (parser.is_reference_query(text) or "query" in intents) and not und["patches"] and not parser.is_safety_request(text):
            self._query(t, text, msg_id)
            return

        # 1. 超出范围
        if und["scope_hints"] or parser.is_safety_request(text):
            self._say(t, parser.scope_message(und["scope_hints"] or ["完整结构计算"]) + "\n\n可以继续的事：描述矩形构件式幕墙的尺寸和分格，"
                         "计算立柱横梁理论用量与材料金额，或查找你上传的资料。", kind="scope", note=note,
                      actions=[{"label": "这是资料查询", "action": "force_query", "payload": {"text": text}}])
            return
        # 2. 确认卡 / 零价确认 / 跳过
        if und["confirm"] and pend.get("type") == "confirm":
            self._confirm_and_run(t, pend.get("task") or "auto", msg_id)
            return
        if und["confirm"] and pend.get("type") == "question" and pend.get("code") == Code.ZERO_PRICE_UNCONFIRMED:
            self._ack_zero(t, pend, msg_id)
            return
        if und["skip"] and pend.get("type") == "question":
            self._skip(t, pend, msg_id)
            return
        # 3. 删除、清空、撤销、改名、帮助
        if "delete_project" in intents or "delete_conversation" in intents:
            self._delete_request(t, "project" if "delete_project" in intents else "conversation")
            return
        if "clear_workspace" in intents:
            self._say(t, "清空工作区会移除全部项目、对话、资料与结果，需要在左下角“设置 → 工作区”中确认执行；"
                         "我不会通过聊天直接清理数据。", kind="info")
            return
        if "undo" in intents and not und["patches"]:
            self._undo(t)
            return
        if und.get("rename_to") and not und["patches"]:
            new_name = und["rename_to"]
            if re.search(r"对话|会话", text):
                old = t.conv.get("title") or "新对话"
                t.conv["title"] = new_name
                t.conv["title_source"] = "manual"
                self._say(t, f"对话已从“{old}”改名为“{new_name}”。", kind="info")
            elif t.project:
                old = t.project["name"]
                t.project["name"] = new_name
                t.project_dirty = True
                self._say(t, f"项目已从“{old}”改名为“{new_name}”。已有计算仍可追溯。", kind="info")
            else:
                self._say(t, "当前还没有项目；如果要改对话标题，请说“对话改名为……” 。", kind="info")
            return
        if "help" in intents and not und["patches"]:
            self._say(t, HELP_TEXT, kind="info")
            return
        # 4. 查资料（不建空项目、不追问几何或价格）
        if "query" in intents and not und["patches"]:
            self._query(t, und.get("query_text") or text, msg_id)
            return
        # 5. 导出
        if "export" in intents and not und["patches"]:
            self._export_card(t)
            return
        # 6. 解释
        if "explain" in intents and not und["patches"]:
            self._explain(t)
            return
        # 7. 录入／修改
        changed = False
        if und["patches"] or und["reuse_price"] or und["create_project"] or (und["tax"] and t.project):
            if t.project is None and not und["patches"] and not und["create_project"]:
                pass
            else:
                changed = self._modify(t, und, text, msg_id, note)
                if changed is None:
                    return
        elif und["ambiguous"]:
            a = und["ambiguous"][0]
            self._say(t, f"“{a['text']}”{a['reason']}。请说明它对应哪个参数，例如“分格宽 1.5 m”或“幕墙总宽 18 m”。",
                      kind="question", note=note)
            return
        # 8. 计算
        task = und["task"]
        if task and t.conv.get("task") != task:
            t.conv["skipped"] = []
        want = "compute" in intents or task is not None
        resume = pend.get("type") == "question" and changed
        auto = changed and t.conv.get("computed") and t.project and t.project.get("measurement_confirmed")
        if want or resume or auto:
            task = task or (pend.get("task") if resume else None) or t.conv.get("task") or "auto"
            self._compute(t, task, msg_id, note=note)
            return
        if changed:
            return
        if t.project is None:
            self._say(t, "我还没有识别到方案参数。可以这样描述：“幕墙宽 18 m、高 6 m，分格宽 1.2 m、高 1.5 m，先算型材用量”；"
                         "也可以粘贴表格或上传 Excel。只查资料可以直接提问。", kind="info", note=note)
        else:
            self._say(t, "我没有理解要修改或计算什么。可以说“把分格宽改为 1.5 m”“算材料金额”“为什么金额减少”。",
                      kind="info", note=note)

    # ================================================================ 修改
    def _resolve_target(self, t: _Turn, und: dict) -> tuple[str, bool]:
        p = t.project
        pend = t.conv.get("pending") or {}
        if und.get("target") == "baseline":
            return "baseline", False
        if und.get("target") == "candidate":
            return "candidate", not P.has_candidate(p)
        if pend.get("type") == "question" and pend.get("role"):
            return pend["role"], False
        if P.has_candidate(p):
            return "candidate", False
        fields = [x["field"] for x in und["patches"]]
        filling = all(P.user_value(p, "baseline", f)[0] is None for f in fields) if fields else True
        if p.get("ran") and not filling and (und.get("task") == "compare" or True):
            return "candidate", True
        return "baseline", False

    def _modify(self, t: _Turn, und: dict, text: str, msg_id: str, note: str | None):
        p = self._ensure_project(t, und.get("project_name"))
        if und.get("source_decl"):
            p["default_source"] = "synthetic" if und["source_decl"] == "synthetic" else "pending"
        if not und["patches"] and not und["reuse_price"] and not und["tax"]:
            # 只有“建一个方案”：先问任务或参数，不补任何预设几何
            t.project_dirty = True
            src = "演示数据" if p.get("default_source") == "synthetic" else "来源待核实（如是测试数据可以告诉我）"
            self._say(t, f"已建立项目“{p['name']}”（{src}）。请描述幕墙总宽、总高和分格尺寸；"
                         "当前支持矩形等距构件式玻璃幕墙。你想先算什么？", kind="question",
                      actions=[{"label": lbl, "action": "set_task", "payload": {"task": tk}} for lbl, tk in TASK_BUTTONS],
                      note=note)
            return None
        role, create = self._resolve_target(t, und)
        P.push_undo(p, f"“{text[:24]}”")
        created_cand = P.ensure_candidate(p) if create or role == "candidate" else False
        declared = "synthetic" if und.get("source_decl") == "synthetic" else None
        notes = "用户声明来自真实项目，未经专业核验" if und.get("source_decl") == "real" else None
        prov = P.add_provenance(p, declared=declared, source_ref=f"你在本轮输入：{text[:60]}",
                                location=f"{t.conv['conversation_id']}#{msg_id}",
                                generation_method=(GenerationMethod.LLM_EXTRACTED.value if und["model_mode"] == "online"
                                                   else GenerationMethod.PROVIDED.value), notes=notes)
        changes, problems = self._apply_patches(t, p, role, und["patches"], prov, und.get("tax"))
        for comp in und.get("reuse_price") or []:
            old = P.user_value(p, role, f"{comp}_unit_price")[0]
            if P.reuse_price(p, role, comp, prov=prov):
                changes.append(f"{P.COMP_NAME[comp]}单价沿用 {P._fmt(P.user_value(p, role, f'{comp}_unit_price')[0])} 元/kg")
            elif old is None:
                problems.append(f"本项目中没有可沿用的{P.COMP_NAME[comp]}报价，请直接告诉我单价")
        if und.get("tax") and not any(x["field"].endswith("unit_price") for x in und["patches"]):
            for comp in ("column", "beam"):
                pr = P.price_of(p, role, comp)
                if pr:
                    P.set_material(p, role, f"{comp}_unit_price", "set", pr["unit_price"], prov=pr["provenance_id"],
                                   raw=pr.get("raw"), tax=und["tax"])
            changes.append("报价口径：" + ("含税（本版不按含税价计算金额）" if und["tax"] == "inclusive" else "未税"))
        if not changes and not problems:
            P.undo(p)
            self._say(t, "没有可更新的参数。", kind="info", note=note)
            return False
        P.bump(p)
        t.project_dirty = True
        for x in und["patches"]:
            if USER_FIELDS[x["field"]]["kind"] == "length":
                t.conv["last_field"] = x["field"]
        created_proj = getattr(t, "created_project", False) and role == "baseline"
        head = ("已基于原方案建立调整方案：" if created_cand else
                f"已建立项目“{p['name']}”，原方案：" if created_proj else f"已更新{P.ROLE_NAME[role]}：")
        tail = "；其余参数沿用原方案。" if role == "candidate" else "。"
        if role == "baseline" and P.has_candidate(p):
            tail = "。调整方案中沿用原方案的参数已同步，之前的结果对应旧参数。"
        body = head + "；".join(changes) + tail if changes else ""
        if problems:
            body += ("\n\n" if body else "") + "未更新：" + "；".join(problems)
        if und.get("source_decl") == "synthetic":
            body += "\n\n本轮录入的数据标为演示数据（synthetic）。"
        elif und.get("source_decl") == "real":
            body += "\n\n已记录“来自真实项目”的说明；来源仍为待核实，未经专业复核。"
        m = self._say(t, body, kind="change", actions=[{"label": "撤销", "action": "undo", "payload": {}}], note=note)
        t.conv["last_change_msg"] = m["id"]
        t.conv["pending"] = None if (t.conv.get("pending") or {}).get("type") != "confirm" else t.conv["pending"]
        self.ws.log("modify", {"role": role, "changes": changes, "problems": problems}, project_id=p["project_id"],
                    conversation_id=t.conv["conversation_id"])
        return True

    def _apply_patches(self, t: _Turn, p: dict, role: str, patches: list[dict], prov: str, tax: str | None,
                       location_by_index: list[str] | None = None) -> tuple[list[str], list[str]]:
        changes, problems = [], []
        for i, x in enumerate(patches):
            uf, op = x["field"], x["op"]
            d = USER_FIELDS[uf]
            pv = prov if not location_by_index else location_by_index[i]
            old, unit = P.user_value(p, role, uf)
            old_s = "未填写" if old is None else P._fmt(old)
            if d["kind"] == "text":
                if op == "set" and x.get("value"):
                    P.set_material(p, role, uf, "set", str(x["value"]), prov=pv)
                    changes.append(f"{d['label']}“{x['value']}”")
                continue
            if op == "clear":
                if "component" in d:
                    P.set_material(p, role, uf, "clear", None, prov=pv)
                else:
                    P.set_geometry(p, role, uf, "clear", None, None, prov=pv, gm="provided")
                changes.append(f"{d['label']}已清空（不会沿用其他方案的值）")
                if uf.endswith("unit_price"):
                    p["zero_acks"][role] = [c for c in p["zero_acks"].get(role, []) if c != d.get("component")]
                continue
            v, u, iss = parse_quantity(x.get("value"), x.get("unit"), d["kind"], uf)
            if iss:
                if "component" in d or op in ("add", "sub"):
                    problems.append(f"{d['label']}：{iss[0].message}")
                    continue
                P.set_geometry(p, role, uf, "set", x.get("value"), x.get("unit"), prov=pv, gm="provided")
                problems.append(f"{d['label']}：{iss[0].message}")
                continue
            raw = f"{x.get('value')} {x.get('unit') or ''}".strip()
            if op in ("add", "sub"):
                if old is None:
                    problems.append(f"{d['label']}当前没有值，无法{'增加' if op == 'add' else '减少'}；请直接给出数值")
                    continue
                new = calc("+" if op == "add" else "-", dec(old), v)
                raw = f"{P._fmt(old)} {d['unit']} {'+' if op == 'add' else '−'} {P._fmt(v)} {d['unit']}"
                v = new
            if v < 0 or (v == 0 and d["kind"] != "price"):
                need = "不能为负" if d["kind"] == "price" else "必须大于 0"
                problems.append(f"{d['label']}{need}（输入为 {P._fmt(v)} {d['unit']}），未写入")
                continue
            conv_note = conversion_note(x.get("value"), x.get("unit"), d["kind"]) if op == "set" else raw
            if "component" in d:
                P.set_material(p, role, uf, "set", dstr(v), prov=pv, raw=raw, tax=x.get("tax") or tax)
                if uf.endswith("unit_price"):
                    p["zero_acks"][role] = [c for c in p["zero_acks"].get(role, []) if c != d["component"]]
            else:
                P.set_geometry(p, role, uf, "set", dstr(v), d["unit"], prov=pv, gm="provided")
                if op == "set":
                    fv = (p["schemes"][role].get("params") or p["schemes"][role].get("overrides") or {}).get(uf)
                    if fv is not None:
                        fv["raw_value"], fv["raw_unit"] = x.get("value"), x.get("unit")
            new_s = P._fmt(v)
            change = (f"{d['label']} {new_s} {d['unit']}" if old is None
                      else f"{d['label']} {old_s} → {new_s} {d['unit']}")
            if conv_note and conv_note != raw or op in ("add", "sub"):
                change += f"（{conv_note}）"
            if x.get("claimed_previous_value") is not None and old is not None:
                try:
                    pv_q, _, _ = parse_quantity(x["claimed_previous_value"], x.get("claimed_previous_unit") or x.get("unit"),
                                                d["kind"])
                    if pv_q is not None and pv_q != dec(old):
                        change += f"（注意：你说原值为 {x['claimed_previous_value']}，但当前记录为 {old_s} {d['unit']}）"
                except ValueError:
                    pass
            changes.append(change)
        return changes, problems

    # ================================================================ 计算
    def _compute(self, t: _Turn, task: str, msg_id: str, *, note: str | None = None, force_partial: bool = False):
        p = t.project
        if p is None:
            self._say(t, "还没有方案。请先描述幕墙总宽、总高和分格尺寸，例如“宽 18 m、高 6 m，分格宽 1.2 m、高 1.5 m”。",
                      kind="question", note=note)
            return
        t.conv["task"] = task
        if task == "compare" and not P.has_candidate(p):
            self._say(t, "目前只有一套方案，已作为原方案保存。告诉我要调整什么（例如“分格宽改为 1.5 m”），我再与原方案比较。",
                      kind="question", note=note)
            return
        pl = planner.plan(p, task)
        if not all(pl["ready"][r]["area"] for r in pl["roles"]):
            self._ask(t, pl, task, note=note)
            return
        tl = pl["target_level"]
        if not pl["ready_all"] and not force_partial and task != "auto":
            first_missing = next((lvl for lvl in planner.LEVELS
                                  if not all(pl["ready"][r][lvl] for r in pl["roles"])), None)
            q = planner.pick_question(pl, t.conv.get("skipped", []), task=task)
            # 缺分格或线重：先问；同时提供“先看已可计算部分”。缺单价：先给出长度／质量，再问单价。
            if first_missing in ("length", "mass") and q is not None:
                ready_lvl = planner.LEVELS[planner.LEVELS.index(first_missing) - 1]
                label = (f"保留真实尺寸，先看{planner.LEVEL_TEXT[ready_lvl]}" if q["code"] == Code.NON_DIVISIBLE_GRID
                         else f"先看{planner.LEVEL_TEXT[ready_lvl]}")
                self._ask(t, pl, task, q=q, note=note, extra_actions=[
                    {"label": label, "action": "run_partial", "payload": {"task": ready_lvl}}])
                return
        if not p.get("measurement_confirmed"):
            self._confirm_card(t, task, pl, note=note)
            return
        self._run(t, task, msg_id, note=note)

    def _run(self, t: _Turn, task: str, msg_id: str, *, note: str | None = None):
        p = t.project
        mode = "online" if extraction.model_available() and t.conv.get("model_mode") != "fallback" else "fallback"
        run = engine.run_task(self.ws, p, conversation_id=t.conv["conversation_id"],
                              request_id=f"{t.conv['conversation_id']}:{msg_id}:{task}", task=task, model_mode=mode,
                              change_reason=(p["schemes"].get("candidate") and "调整方案相对原方案") or "")
        t.conv["last_run_id"] = run["run_id"]
        t.conv["computed"] = True
        t.conv["pending"] = None
        if not p.get("ran"):
            p["ran"] = True
            t.project_dirty = True
        self._say(t, self._conclusion(run), kind="result", card={"type": "result", "run_id": run["run_id"]}, note=note)
        if run["execution_state"] != "COMPLETE":
            pl = planner.plan(p, task)
            q = planner.pick_question(pl, t.conv.get("skipped", []), task=task)
            if q:
                self._ask(t, pl, task, q=q)

    def _ask(self, t: _Turn, pl: dict, task: str, *, q: dict | None = None, note: str | None = None,
             extra_actions: list | None = None):
        q = q or planner.pick_question(pl, t.conv.get("skipped", []), task=task)
        if q is None:
            gaps = "；".join(planner.gaps_text(pl, r) for r in pl["roles"] if planner.gaps_text(pl, r))
            self._say(t, f"暂时无法{planner.TASK_TEXT.get(task, '计算')}：{gaps or '缺少必要参数'}。", kind="info", note=note)
            return
        actions = [{"label": o, "action": "answer", "payload": {"text": o}} for o in (q.get("options") or [])]
        actions += extra_actions or []
        actions.append({"label": "暂时跳过", "action": "skip", "payload": {}})
        purpose = USER_FIELDS[q["field"]]
        self._say(t, q["text"], kind="question", actions=actions, note=note,
                  card={"type": "question", "field": q["field"], "level": purpose["level"]})
        t.conv["pending"] = {"type": "question", **q}

    def _confirm_card(self, t: _Turn, task: str, pl: dict, *, note: str | None = None):
        p = t.project
        rows = self._key_rows(p)
        gaps = "；".join(planner.gaps_text(pl, r) for r in pl["roles"] if planner.gaps_text(pl, r))
        text = (f"计算前请确认一次本次使用的参数和计量口径（之后修改参数时不再重复确认）：\n\n"
                + "\n".join(f"- {a}：{b}" for a, b in rows)
                + "\n\n计量口径：矩形等距立面，左右边柱和顶底横梁计入，按分格轴线理论长度计量，不扣连接间隙，"
                  "不计损耗、玻璃、五金和安装；单价按未税材料价。"
                + (f"\n\n目前{gaps}，这些部分会显示为未计算。" if gaps else ""))
        self._say(t, text, kind="confirm", card={"type": "confirm", "task": task},
                  actions=[{"label": "确认并计算", "action": "confirm_run", "payload": {"task": task}},
                           {"label": "查看参数", "action": "open_params", "payload": {}}], note=note)
        t.conv["pending"] = {"type": "confirm", "task": task}

    def _key_rows(self, p: dict) -> list[tuple[str, str]]:
        def v(role, f):
            val, unit = P.user_value(p, role, f)
            return "未填写" if val is None else P._fmt(val)
        rows = []
        for role in p["schemes"]:
            pre = f"{P.ROLE_NAME[role]}" if P.has_candidate(p) else ""
            rows.append((f"{pre}幕墙尺寸", f"{v(role, 'width_m')} × {v(role, 'height_m')} m，"
                                          f"分格 {v(role, 'grid_width_m')} × {v(role, 'grid_height_m')} m"))
            rows.append((f"{pre}立柱／横梁", f"线重 {v(role, 'column_linear_mass')}／{v(role, 'beam_linear_mass')} kg/m，"
                                           f"单价 {v(role, 'column_unit_price')}／{v(role, 'beam_unit_price')} 元/kg"))
        src = "演示数据（synthetic）" if p.get("default_source") == "synthetic" else "来源待核实（用户提供）"
        rows.append(("数据性质", src))
        return rows[:5]

    def _confirm_and_run(self, t: _Turn, task: str, msg_id: str):
        p = t.project
        if p is None:
            return
        p["measurement_confirmed"] = True
        p["confirmed_at"] = now_utc()
        t.project_dirty = True
        self.ws.log("confirm_inputs", {"task": task, "meaning": "本次使用确认，不等于专业复核"},
                    project_id=p["project_id"], conversation_id=t.conv["conversation_id"])
        t.conv["pending"] = None
        self._compute(t, task, msg_id, force_partial=True)

    def _skip(self, t: _Turn, pend: dict, msg_id: str):
        key = f"{pend.get('role', 'baseline')}:{pend['field']}"
        t.conv["skipped"] = sorted(set(t.conv.get("skipped", [])) | {key})
        t.conv["pending"] = None
        self._say(t, f"好的，{USER_FIELDS[pend['field']]['label']}暂缺，相关结果会标为未计算；需要时再告诉我。", kind="info")
        if t.project and t.project.get("measurement_confirmed") and planner.plan(t.project, pend.get("task", "auto"))[
                "ready"].get("baseline", {}).get("area"):
            self._run(t, pend.get("task") or "auto", msg_id)

    def _ack_zero(self, t: _Turn, pend: dict, msg_id: str):
        comp = USER_FIELDS[pend["field"]]["component"]
        role = pend.get("role", "baseline")
        t.project["zero_acks"][role] = sorted(set(t.project["zero_acks"].get(role, [])) | {comp})
        t.project_dirty = True
        t.conv["pending"] = None
        self._say(t, f"已记录：{P.ROLE_NAME[role]}{P.COMP_NAME[comp]}单价确为 0（显式确认）。", kind="info")
        self._compute(t, pend.get("task") or "auto", msg_id)

    # ================================================================ 结果文字
    def _conclusion(self, run: dict) -> str:
        base = all_metrics((run.get("results") or {}).get("baseline"))
        cand = all_metrics((run.get("results") or {}).get("candidate"))
        comp = run.get("comparison") or {}
        if comp.get("comparable"):
            d = comp["delta_metrics"].get("delta_total_cost_cny", {})
            pct = (comp.get("percent_metrics") or {}).get("percent_total_cost") or {}
            val = dec(d["value"]) if d.get("value") is not None else Decimal(0)
            word = "减少" if val < 0 else "增加" if val > 0 else "不变"
            amt = fmt(d, "delta_total_cost_cny").lstrip("-")
            pct_s = f"，{'下降' if val < 0 else '上升'} {pct['display_value'].lstrip('-')}%" if pct.get(
                "value") is not None and val != 0 else ("" if pct.get("value") is not None else "（原方案金额为 0，变化率不可计算）")
            return f"调整方案相对原方案，已覆盖型材材料金额{word} {amt} 元{pct_s}。"
        one = cand or base
        who = "调整方案" if cand and base else ""
        parts = []
        if one.get("total_cost_cny", {}).get("value") is not None:
            parts.append(f"{who}已覆盖型材材料金额 {fmt(one['total_cost_cny'], 'total_cost_cny')} 元")
        if one.get("total_mass_kg", {}).get("value") is not None:
            parts.append(f"理论质量 {one['total_mass_kg']['display_value']} kg")
        elif one.get("column_length_m", {}).get("value") is not None:
            parts.append(f"立柱理论总长 {one['column_length_m']['display_value']} m、横梁 "
                         f"{one['beam_length_m']['display_value']} m")
        if one.get("facade_area_m2", {}).get("value") is not None and not parts:
            parts.append(f"立面面积 {one['facade_area_m2']['display_value']} m²")
        if run.get("comparison") and not comp.get("comparable"):
            parts.append("两方案口径不一致，未给出差值")
        txt = "；".join(parts) + "。" if parts else "没有可显示的结果。"
        if run["execution_state"] != "COMPLETE":
            missing = []
            for mod, label in (("length", "长度"), ("mass", "质量"), ("cost", "金额")):
                if run["module_statuses"].get(mod) in ("blocked", "error"):
                    missing.append(label)
            if missing:
                txt += f"{'、'.join(missing)}暂未计算（见下方原因）。"
        return txt

    # ================================================================ 解释
    def _explain(self, t: _Turn):
        p = t.project
        run = self.ws.get_run(t.conv.get("last_run_id"), (p or {}).get("project_id"))
        if run is None or run.get("requested_task") == "query":
            self._say(t, "还没有可以解释的计算结果。先告诉我方案参数并要求计算。", kind="info")
            return
        stale = "" if engine.is_current(p, run) else "（注意：参数在这次计算后已修改，以下解释针对上次结果。）\n\n"
        lines = [template_explanation(run)]
        for role in ("baseline", "candidate"):
            res = (run.get("results") or {}).get(role) or {}
            steps = [s for mod in ("length", "mass", "cost") for s in (res.get(mod) or {}).get("steps", [])
                     if s["formula_id"] in ("QTY-COLUMN-v1", "QTY-BEAM-v1", "MASS-v1", "COST-v1")
                     and not s["step_id"].endswith(("per_m2", "_count", "_rows", "_segments"))]
            if steps:
                name = P.ROLE_NAME[role] if len(run.get("results") or {}) > 1 else "计算"
                lines.append(f"\n**{name}步骤**：" + "；".join(f"{s['expression']} = {P._fmt(s['exact_value'])} {s['unit']}"
                                                         for s in steps[:6]))
        text = stale + "\n".join(lines)
        model_text, issues, _ = model_explanation(run, t.conv.get("model_mode", "auto"))
        if model_text and not check_model_text(model_text, run):
            text += f"\n\n补充说明：{model_text}"
        self._say(t, text, kind="explain", card={"type": "explain", "run_id": run["run_id"]})

    # ================================================================ 资料
    def _query(self, t: _Turn, qtext: str, msg_id: str):
        run = engine.run_query(self.ws, query_text=qtext, conversation_id=t.conv["conversation_id"],
                               request_id=f"{t.conv['conversation_id']}:{msg_id}:query", project=t.project)
        n = run.get("documents_available", 0)
        rt = run.get("retrieval") or {}
        hits = (rt.get("pinned") or []) + (rt.get("candidates") or [])
        if n == 0 and not hits:
            t.conv["pending_query"] = qtext
            self._say(t, f"可以帮你查找“{qtext}”相关要求。资料库里还没有资料，请先上传 PDF、TXT 或 MD 资料。"
                         "已保留这个问题，上传后可以继续检索。", kind="query",
                      actions=[{"label": "上传资料", "action": "open_documents"}])
            return
        if not hits:
            self._say(t, f"在 {n} 份资料中没有找到可靠匹配。这不表示不存在相关要求，需人工核实；可以换个关键词再试。",
                      kind="query", card={"type": "query", "run_id": run["run_id"]})
            return
        self._say(t, f"在 {n} 份资料中找到 {len(hits)} 条可能相关的片段。资料未经审核（来源、版本是否现行、是否适用均待核实），"
                     "仅供查阅，尚未对方案作判定：", kind="query", card={"type": "query", "run_id": run["run_id"]})

    # ================================================================ 文件
    def _handle_file(self, t: _Turn, fname: str, data: bytes, msg_id: str):
        low = fname.lower()
        if low.endswith(TABLE_EXT):
            res = importer.previews_from_file(fname, data)
            staging = self.ws.root / "attachments" / "_imports" / new_id("F")
            staging.mkdir(parents=True, exist_ok=True)
            (staging / Path(fname).name).write_bytes(data)
            if "error" in res:
                self._say(t, f"{fname}：{res['error']}", kind="error",
                          actions=[{"label": "下载空白模板", "action": "template", "payload": {}}])
                return
            if "sheet_choice" in res:
                t.conv["pending_sheets"] = {"filename": fname, "sheets": res["sheets"], "staging": str(staging)}
                self._say(t, f"{fname} 中有多个工作表看起来都包含参数，要导入哪一个？", kind="question",
                          actions=[{"label": s, "action": "choose_sheet", "payload": {"sheet": s}}
                                   for s in res["sheet_choice"]])
                return
            self._import_card(t, res["preview"], str(staging))
            return
        if low.endswith(DOC_EXT):
            r = documents.add_document(self.ws, fname, data)
            if not r["ok"]:
                self._say(t, f"{fname}：{r['error']}", kind="error")
                return
            d = r["doc"]
            if r["duplicate"]:
                msg = f"{fname} 已在资料库中（内容相同），未重复添加。"
            else:
                unit = "页" if low.endswith(".pdf") else "分段"
                msg = (f"已加入资料库：{d['title']}（{d['page_count']} {unit}，可检索 {d['text_pages']} {unit}）。"
                       + ("未提取到足够文本，需核对文件。" if d["needs_ocr"] else "")
                       + "资料标为“来源待核实、未审核”，只供查阅。")
            acts = []
            if t.conv.get("pending_query"):
                acts.append({"label": "用刚才的问题检索", "action": "rerun_query",
                             "payload": {"text": t.conv["pending_query"]}})
            self._say(t, msg, kind="info", actions=acts)
            return
        self._say(t, f"{fname}：暂不支持这种文件。表格请用 .xlsx/.csv，资料请用 PDF/TXT。图片和 CAD 图纸不能直接识别为可计算方案。",
                  kind="error")

    def _handle_paste(self, t: _Turn, text: str):
        pv = importer.preview_from_paste(text)
        if pv is None:
            self._handle_text(t, text, t.conv["messages"][-1]["id"])
            return
        self._import_card(t, pv, None)

    def _import_card(self, t: _Turn, pv: dict, staging: str | None):
        ok = [i for i in pv["items"] if i.get("ok")]
        bad = [i for i in pv["items"] if not i.get("ok")]
        lines = [f"识别到 {len(ok)} 项参数" + (f"，{len(bad)} 项需要处理" if bad else "") + "。确认后写入"
                 + ("当前项目" if t.project else "新项目") + "；导入前不会改动任何数据。"]
        if pv["missing"]:
            lines.append("仍缺：" + "、".join(pv["missing"]) + "（可之后在对话中补充）。")
        if pv["ambiguous"]:
            lines.append("有 " + str(len(pv["ambiguous"])) + " 列含义不明确，请在下面选择对应参数，不选则不导入。")
        for n in pv["notes"][:3]:
            lines.append(n)
        pv["staging"] = staging
        t.conv.setdefault("imports", {})[pv["import_id"]] = pv
        self._say(t, "\n".join(lines), kind="import", card={"type": "import", "import_id": pv["import_id"]})

    def apply_import(self, conv_id: str, msg_id: str, import_id: str, *, mapping: dict, mode: str,
                     declared: str | None) -> dict:
        conv = self.ws.get_conversation(conv_id)
        t = _Turn(self.ws, conv)
        pv = (conv.get("imports") or {}).get(import_id)
        if pv is None:
            raise AssistantError(Code.STALE_DRAFT, "导入预览已失效，请重新上传")
        self._consume(t, msg_id)
        items = [dict(i) for i in pv["items"] if i.get("ok")]
        for a in pv["ambiguous"]:
            f = mapping.get(a["key"])
            if f and f != "ignore":
                it = {"role": a["role"], "field": f, "raw_value": a["sample"], "raw_unit": a["unit"],
                      "location": a["location"], "tax": None}
                v, u, iss = parse_quantity(it["raw_value"], it["raw_unit"], USER_FIELDS[f]["kind"], f)
                if not iss:
                    items.append(it)
        if mode == "new" or t.project is None:
            if t.project is not None:
                t.conv = self.new_conversation()
                t = _Turn(self.ws, t.conv)
            name = Path(pv["source"].get("filename") or "粘贴的参数表").stem
            self._ensure_project(t, name)
        p = t.project
        if declared == "synthetic":
            p["default_source"] = "synthetic"
        P.push_undo(p, "导入表格")
        changes_all, problems_all = [], []
        for role in ("baseline", "candidate"):
            rows = [i for i in items if i["role"] == role]
            if not rows:
                continue
            if role == "candidate":
                P.ensure_candidate(p)
            provs = [P.add_provenance(p, declared=declared if declared == "synthetic" else None,
                                      source_ref=f"导入：{r['location']}", location=r["location"]) for r in rows]
            patches = [{"field": r["field"], "op": "set", "value": r["raw_value"], "unit": r["raw_unit"],
                        "tax": r.get("tax"), "claimed_previous_value": None} for r in rows]
            ch, pr = self._apply_patches(t, p, role, patches, provs[0] if provs else "", None, provs)
            changes_all += [f"{P.ROLE_NAME[role]}：{c}" for c in ch]
            problems_all += pr
        if pv.get("staging"):
            dst = self.ws.root / "attachments" / p["project_id"]
            dst.mkdir(parents=True, exist_ok=True)
            for f in Path(pv["staging"]).glob("*"):
                f.replace(dst / f.name)
        P.bump(p)
        t.project_dirty = True
        text = f"已导入到项目“{p['name']}”：\n" + "\n".join(f"- {c}" for c in changes_all[:12])
        if problems_all:
            text += "\n\n未导入：" + "；".join(problems_all)
        m = self._say(t, text, kind="change", actions=[{"label": "撤销", "action": "undo", "payload": {}}] + [
            {"label": lbl, "action": "set_task", "payload": {"task": tk}} for lbl, tk in TASK_BUTTONS])
        t.conv["last_change_msg"] = m["id"]
        self.ws.log("import", {"items": len(items), "source": pv["source"]}, project_id=p["project_id"],
                    conversation_id=t.conv["conversation_id"])
        return self._commit(t)

    # ================================================================ 导出、删除、撤销
    def _export_card(self, t: _Turn):
        p = t.project
        run = self.ws.get_run(t.conv.get("last_run_id"), (p or {}).get("project_id"))
        if run is None or run.get("requested_task") == "query":
            self._say(t, "还没有可以导出的计算结果。", kind="info")
            return
        note = "" if engine.is_current(p, run) else "（参数在这次计算后已修改，导出的是对应旧参数的历史结果。）"
        self._say(t, "比选表已准备好" + note + "。技术追溯包（含快照与计算过程 JSON）在“更多”中。", kind="export",
                  card={"type": "export", "run_id": run["run_id"]})

    def _delete_request(self, t: _Turn, what: str):
        if what == "project":
            if t.project is None:
                self._say(t, "当前对话没有项目可删除。", kind="info")
                return
            imp = self.ws.project_impact(t.project["project_id"])
            self._say(t, f"要把项目“{t.project['name']}”移入回收站吗？将一起移入：{imp['conversations']} 个对话、"
                         f"{imp['schemes']} 套方案、{imp['materials']} 条项目材料、{imp['runs']} 次计算结果、"
                         f"{imp['attachments']} 个附件。个人材料库中的材料不受影响。之后可以在回收站恢复或彻底清理。",
                      kind="confirm", card={"type": "delete", "target": "project"},
                      actions=[{"label": "移入回收站", "action": "trash_project", "payload": {}},
                               {"label": "取消", "action": "cancel", "payload": {}}])
        else:
            keep = f"项目“{t.project['name']}”及其方案和结果会保留，可在“项目”中重新打开。" if t.project else ""
            self._say(t, "要把这个对话移入回收站吗？" + keep, kind="confirm", card={"type": "delete", "target": "conversation"},
                      actions=[{"label": "移入回收站", "action": "trash_conversation", "payload": {}},
                               {"label": "取消", "action": "cancel", "payload": {}}])

    def _undo(self, t: _Turn):
        if t.project is None or not t.project.get("undo"):
            self._say(t, "没有可以撤销的修改。", kind="info")
            return
        label = P.undo(t.project)
        t.project_dirty = True
        t.conv["pending"] = None
        acts = ([{"label": "重新计算", "action": "set_task", "payload": {"task": t.conv.get("task") or "auto"}}]
                if t.conv.get("computed") else [])
        self._say(t, f"已撤销：{label}。已保存的计算结果不受影响。", kind="change", actions=acts)

    # ================================================================ 按钮动作
    def act(self, conv_id: str, msg_id: str, action: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        conv = self.ws.get_conversation(conv_id)
        if conv is None:
            raise AssistantError(Code.STALE_DRAFT, "对话不存在或已删除")
        t = _Turn(self.ws, conv)
        msg = next((m for m in conv["messages"] if m["id"] == msg_id), None)
        if msg is None or msg.get("consumed"):
            return conv
        if action in ("force_query", "rerun_query"):
            self._consume(t, msg_id)
            self._query(t, payload.get("text", ""), msg_id + "-corrected")
            return self._commit(t)
        if action == "answer":
            self._consume(t, msg_id)
            self.ws.save_conversation(t.conv, t.conv_rev)
            return self.send(conv_id, payload.get("text", ""))
        self._consume(t, msg_id)
        if action == "undo":
            if conv.get("last_change_msg") != msg_id:
                self._say(t, "只能撤销最近一次修改。", kind="info")
            else:
                self._undo(t)
        elif action == "confirm_run":
            self._confirm_and_run(t, payload.get("task") or "auto", msg_id)
        elif action == "run_partial":
            self._compute(t, payload.get("task") or "area", msg_id, force_partial=True)
        elif action == "set_task":
            self._compute(t, payload["task"], msg_id)
        elif action == "skip":
            pend = t.conv.get("pending") or {}
            if pend.get("type") == "question":
                self._skip(t, pend, msg_id)
        elif action == "trash_project" and t.project:
            name = t.project["name"]
            self.ws.trash_project(t.project["project_id"])
            t.project = None
            t.reset_conversation = True
            self.ws.log("trash_project", {"name": name})
            return {"_deleted": True, "message": f"项目“{name}”已移入回收站。"}
        elif action == "trash_conversation":
            self.ws.trash_conversation(conv_id)
            return {"_deleted": True, "message": "对话已移入回收站。"}
        elif action == "choose_sheet":
            ps = t.conv.get("pending_sheets") or {}
            rows = (ps.get("sheets") or {}).get(payload.get("sheet"))
            if rows is not None:
                pv = importer.analyze(rows, source={"kind": "file", "filename": ps.get("filename"),
                                                    "sheet": payload["sheet"]})
                t.conv["pending_sheets"] = None
                self._import_card(t, pv, ps.get("staging"))
        elif action == "cancel":
            self._say(t, "已取消。", kind="info")
        return self._commit(t)

    # ================================================================ 表单（同一草稿，不另建状态）
    def save_form(self, conv_id: str, role: str, values: dict, *, project_name: str | None = None,
                  declared: str | None = None, tax: str | None = None) -> dict:
        """values: {user_field: (raw_value or None, raw_unit)}；空字符串表示不修改，"清空"表示显式清空。"""
        conv = self.ws.get_conversation(conv_id)
        t = _Turn(self.ws, conv)
        p = self._ensure_project(t, project_name)
        if declared:
            p["default_source"] = declared
        patches = []
        for uf, (val, unit) in values.items():
            if val is None or str(val).strip() == "":
                continue
            if str(val).strip() == "清空":
                patches.append({"field": uf, "op": "clear", "value": None, "unit": None})
                continue
            cur, _ = P.user_value(p, role, uf)
            if USER_FIELDS[uf]["kind"] != "text" and cur is not None:
                v, _, iss = parse_quantity(val, unit, USER_FIELDS[uf]["kind"])
                if not iss and v == dec(cur) and tax is None:
                    continue
            elif USER_FIELDS[uf]["kind"] == "text" and str(val) == str(cur):
                continue
            patches.append({"field": uf, "op": "set", "value": str(val).strip(), "unit": unit or None,
                            "claimed_previous_value": None})
        if not patches and not tax:
            return self._commit(t)
        if role == "candidate":
            P.ensure_candidate(p)
        P.push_undo(p, "表单修改")
        prov = P.add_provenance(p, declared=declared, source_ref="你在参数表单中填写",
                                location=f"{conv_id}#form")
        ch, pr = self._apply_patches(t, p, role, patches, prov, tax)
        P.bump(p)
        t.project_dirty = True
        text = f"已通过表单更新{P.ROLE_NAME[role]}：" + ("；".join(ch) if ch else "无变化")
        if role == "baseline" and P.has_candidate(p) and ch:
            text += "。调整方案中沿用原方案的参数已同步，之前的结果对应旧参数"
        if pr:
            text += "\n\n未更新：" + "；".join(pr)
        m = self._say(t, text, kind="change", actions=[{"label": "撤销", "action": "undo", "payload": {}}])
        t.conv["last_change_msg"] = m["id"]
        return self._commit(t)

    # ================================================================ 规则与材料库
    def add_rule(self, project_id: str, **kw) -> str:
        p = self.ws.get_project(project_id)
        P.push_undo(p, "新增演示规则")
        rid = P.add_demo_rule(p, **kw)
        P.bump(p)
        self.ws.save_project(p, p["rev"])
        return rid

    def delete_rule(self, project_id: str, rule_id: str) -> None:
        p = self.ws.get_project(project_id)
        P.push_undo(p, "删除演示规则")
        p["rules"].pop(rule_id, None)
        P.bump(p)
        self.ws.save_project(p, p["rev"])

    def save_to_library(self, project_id: str, role: str, comp: str) -> str | None:
        p = self.ws.get_project(project_id)
        m = P.material_of(p, role, comp)
        if not m or m.get("linear_mass") is None:
            return None
        pr = P.price_of(p, role, comp)
        lib = {"mat_id": new_id("LIB"), "component_type": comp, "profile_name": m["profile_name"],
               "linear_mass": m["linear_mass"], "unit_price": (pr or {}).get("unit_price"),
               "tax_basis": (pr or {}).get("tax_basis"), "from_project": p["name"],
               "source_status": p["provenance"].get(m.get("provenance_id") or "", {}).get("source_status", "pending"),
               "created_at": now_utc()}
        self.ws.save_library_material(lib)
        return lib["mat_id"]

    def use_library_material(self, conv_id: str, role: str, mat_id: str) -> dict:
        lib = next((x for x in self.ws.list_library() if x["mat_id"] == mat_id), None)
        conv = self.ws.get_conversation(conv_id)
        t = _Turn(self.ws, conv)
        if lib is None or t.project is None:
            return conv
        p = t.project
        comp = lib["component_type"]
        P.push_undo(p, "使用材料库材料")
        if role == "candidate":
            P.ensure_candidate(p)
        prov = P.add_provenance(p, declared="synthetic" if lib.get("source_status") == "synthetic" else None,
                                source_ref=f"个人材料库：{lib['profile_name']}（来自项目 {lib.get('from_project')}）",
                                location=mat_id)
        P.set_material(p, role, f"{comp}_name", "set", lib["profile_name"], prov=prov)
        P.set_material(p, role, f"{comp}_linear_mass", "set", lib["linear_mass"], prov=prov,
                       raw=f"材料库 {lib['linear_mass']} kg/m")
        if lib.get("unit_price") is not None:
            P.set_material(p, role, f"{comp}_unit_price", "set", lib["unit_price"], prov=prov,
                           raw=f"材料库 {lib['unit_price']} 元/kg", tax=lib.get("tax_basis"))
        P.bump(p)
        t.project_dirty = True
        m = self._say(t, f"已为{P.ROLE_NAME[role]}{P.COMP_NAME[comp]}使用材料库中的“{lib['profile_name']}”（复制一份到本项目，"
                         f"线重 {lib['linear_mass']} kg/m" + (f"，单价 {lib['unit_price']} 元/kg" if lib.get("unit_price")
                                                           is not None else "") + "）。", kind="change",
                      actions=[{"label": "撤销", "action": "undo", "payload": {}}])
        t.conv["last_change_msg"] = m["id"]
        return self._commit(t)


__all__ = ["Assistant", "AssistantError", "copy", "re"]
