"""本地工作区存储（SQLite）。

- 新工作区为空：没有项目、对话、材料、报价、规则、资料、运行与导出；不在任何路径自动写入演示数据。
- 所有查询都按显式 ID（项目、对话、运行）进行，不用“全局最新”兜底。
- 快照、运行、导出写入后不可修改；项目／对话可编辑（带 rev 乐观锁）、软删除、恢复与彻底清理。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path

from domain.contracts import Code, canonical_json, now_utc
from storage.titles import backfill_title

DEFAULT_DIR = Path(os.environ.get("MW_RUNTIME_DIR") or
                   Path(os.environ.get("LOCALAPPDATA") or Path.home()/'.local'/'share') / 'MqHelper' / 'workspace')

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, rev INTEGER NOT NULL,
  json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, trashed_at TEXT);
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY, project_id TEXT, title TEXT NOT NULL, status TEXT NOT NULL,
  rev INTEGER NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, trashed_at TEXT,
  trashed_with_project TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, scheme_id TEXT NOT NULL, version INTEGER NOT NULL,
  content_hash TEXT NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(project_id, scheme_id, version));
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, project_id TEXT, conversation_id TEXT, request_id TEXT UNIQUE NOT NULL,
  input_hash TEXT NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, json TEXT NOT NULL,
  pages TEXT NOT NULL, created_at TEXT NOT NULL, trashed_at TEXT);
CREATE TABLE IF NOT EXISTS library_materials (
  mat_id TEXT PRIMARY KEY, status TEXT NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exports (
  export_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT, request_id TEXT UNIQUE NOT NULL,
  json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analyses (
  analysis_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT, conversation_id TEXT, version INTEGER NOT NULL,
  json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, version));
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, conversation_id TEXT, run_id TEXT,
  event_type TEXT NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Workspace:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else DEFAULT_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in ("exports", "attachments"):
            (self.root / sub).mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self.db = sqlite3.connect(self.root / "workspace.db", check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._backfill_titles()

    def _backfill_titles(self):
        # 只补默认题名；不改变历史消息、排序时间或已有非默认名称。
        with self.tx():
            for cid, raw in self._all("SELECT conversation_id, json FROM conversations WHERE title='新对话'"):
                conv = json.loads(raw)
                if backfill_title(conv):
                    conv["rev"] += 1
                    self.db.execute("UPDATE conversations SET title=?, rev=?, json=? WHERE conversation_id=?",
                                    (conv["title"], conv["rev"], canonical_json(conv), cid))

    # ------------------------------------------------------------ 基础
    def tx(self):
        return _Tx(self)

    def _one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def _all(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def get_setting(self, key: str, default=None):
        r = self._one("SELECT value FROM settings WHERE key=?", key)
        return default if r is None else json.loads(r[0])

    def set_setting(self, key: str, value) -> None:
        with self.tx():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def counts(self) -> dict:
        q = lambda sql: self._one(sql)[0]  # noqa: E731
        return {
            "projects": q("SELECT COUNT(*) FROM projects WHERE status='active'"),
            "projects_trashed": q("SELECT COUNT(*) FROM projects WHERE status='trashed'"),
            "conversations": q("SELECT COUNT(*) FROM conversations WHERE status='active'"),
            "conversations_trashed": q("SELECT COUNT(*) FROM conversations WHERE status='trashed'"),
            "snapshots": q("SELECT COUNT(*) FROM snapshots"), "runs": q("SELECT COUNT(*) FROM runs"),
            "documents": q("SELECT COUNT(*) FROM documents WHERE status='active'"),
            "library_materials": q("SELECT COUNT(*) FROM library_materials WHERE status='active'"),
            "exports": q("SELECT COUNT(*) FROM exports"),
        }

    # ------------------------------------------------------------ 项目
    def create_project(self, project: dict) -> dict:
        now = now_utc()
        project = dict(project, rev=1, status="active", created_at=now, updated_at=now)
        with self.tx():
            self.db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)",
                            (project["project_id"], project["name"], "active", 1, canonical_json(project), now, now,
                             None))
        return project

    def get_project(self, project_id: str | None, include_trashed: bool = False) -> dict | None:
        if not project_id:
            return None
        r = self._one("SELECT json, status FROM projects WHERE project_id=?", project_id)
        if r is None or (r[1] != "active" and not include_trashed):
            return None
        return json.loads(r[0])

    def save_project(self, project: dict, expected_rev: int) -> dict:
        with self.tx():
            r = self._one("SELECT rev, status FROM projects WHERE project_id=?", project["project_id"])
            if r is None or r[1] != "active":
                raise StoreError(Code.STALE_DRAFT, "项目不存在或已删除")
            if r[0] != expected_rev:
                raise StoreError(Code.STALE_DRAFT, "项目已在别处更新，请刷新后重试")
            project = dict(project, rev=r[0] + 1, updated_at=now_utc())
            self.db.execute("UPDATE projects SET name=?, rev=?, json=?, updated_at=? WHERE project_id=?",
                            (project["name"], project["rev"], canonical_json(project), project["updated_at"],
                             project["project_id"]))
        return project

    def list_projects(self, status: str = "active") -> list[dict]:
        return [json.loads(r[0]) | {"status": r[1], "trashed_at": r[2]} for r in self._all(
            "SELECT json, status, trashed_at FROM projects WHERE status=? ORDER BY updated_at DESC", status)]

    # ------------------------------------------------------------ 对话
    def create_conversation(self, conv: dict) -> dict:
        now = now_utc()
        conv = dict(conv, rev=1, status="active", created_at=now, updated_at=now)
        with self.tx():
            self.db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (conv["conversation_id"], conv.get("project_id"), conv["title"], "active", 1,
                             canonical_json(conv), now, now, None, None))
        return conv

    def get_conversation(self, conversation_id: str | None, include_trashed: bool = False) -> dict | None:
        if not conversation_id:
            return None
        r = self._one("SELECT json, status FROM conversations WHERE conversation_id=?", conversation_id)
        if r is None or (r[1] != "active" and not include_trashed):
            return None
        return json.loads(r[0])

    def save_conversation(self, conv: dict, expected_rev: int) -> dict:
        with self.tx():
            r = self._one("SELECT rev, status FROM conversations WHERE conversation_id=?", conv["conversation_id"])
            if r is None or r[1] != "active":
                raise StoreError(Code.STALE_DRAFT, "对话不存在或已删除")
            if r[0] != expected_rev:
                raise StoreError(Code.STALE_DRAFT, "对话已更新，请刷新后重试")
            conv = dict(conv, rev=r[0] + 1, updated_at=now_utc())
            self.db.execute("UPDATE conversations SET project_id=?, title=?, rev=?, json=?, updated_at=? "
                            "WHERE conversation_id=?",
                            (conv.get("project_id"), conv["title"], conv["rev"], canonical_json(conv),
                             conv["updated_at"], conv["conversation_id"]))
        return conv

    def list_conversations(self, status: str = "active", project_id: str | None = None) -> list[dict]:
        sql = "SELECT json, status, trashed_at, trashed_with_project FROM conversations WHERE status=?"
        args = [status]
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        sql += " ORDER BY updated_at DESC, rowid DESC"
        return [json.loads(r[0]) | {"status": r[1], "trashed_at": r[2], "trashed_with_project": r[3]}
                for r in self._all(sql, *args)]

    # ------------------------------------------------------------ 回收站
    def trash_conversation(self, conversation_id: str) -> None:
        with self.tx():
            self.db.execute("UPDATE conversations SET status='trashed', trashed_at=?, trashed_with_project=NULL "
                            "WHERE conversation_id=? AND status='active'", (now_utc(), conversation_id))

    def restore_conversation(self, conversation_id: str) -> None:
        with self.tx():
            r = self._one("SELECT project_id FROM conversations WHERE conversation_id=?", conversation_id)
            if r and r[0]:
                pr = self._one("SELECT status FROM projects WHERE project_id=?", r[0])
                if pr and pr[0] != "active":
                    raise StoreError(Code.STALE_DRAFT, "该对话所属项目在回收站中，请先恢复项目")
            self.db.execute("UPDATE conversations SET status='active', trashed_at=NULL WHERE conversation_id=?",
                            (conversation_id,))

    def project_impact(self, project_id: str) -> dict:
        p = self.get_project(project_id, include_trashed=True) or {}
        return {
            "conversations": self._one("SELECT COUNT(*) FROM conversations WHERE project_id=?", project_id)[0],
            "schemes": len(p.get("schemes") or {}), "materials": len(p.get("materials") or {}),
            "prices": len(p.get("prices") or {}), "rules": len(p.get("rules") or {}),
            "runs": self._one("SELECT COUNT(*) FROM runs WHERE project_id=?", project_id)[0],
            "snapshots": self._one("SELECT COUNT(*) FROM snapshots WHERE project_id=?", project_id)[0],
            "exports": self._one("SELECT COUNT(*) FROM exports WHERE project_id=?", project_id)[0],
            "attachments": len(list((self.root / "attachments" / project_id).glob("*")))
            if (self.root / "attachments" / project_id).exists() else 0,
        }

    def trash_project(self, project_id: str) -> None:
        now = now_utc()
        with self.tx():
            self.db.execute("UPDATE projects SET status='trashed', trashed_at=? WHERE project_id=?", (now, project_id))
            self.db.execute("UPDATE conversations SET status='trashed', trashed_at=?, trashed_with_project=? "
                            "WHERE project_id=? AND status='active'", (now, project_id, project_id))

    def restore_project(self, project_id: str) -> None:
        with self.tx():
            self.db.execute("UPDATE projects SET status='active', trashed_at=NULL WHERE project_id=?", (project_id,))
            self.db.execute("UPDATE conversations SET status='active', trashed_at=NULL, trashed_with_project=NULL "
                            "WHERE trashed_with_project=?", (project_id,))

    def purge_project(self, project_id: str) -> dict:
        """彻底清理：数据库记录、快照、运行、导出文件、附件、事件。返回实际清理数量与未完成项。"""
        impact = self.project_impact(project_id)
        problems = []
        with self.tx():
            export_dirs = [json.loads(r[0]).get("dir") for r in
                           self._all("SELECT json FROM exports WHERE project_id=?", project_id)]
            for table in ("runs", "snapshots", "exports", "events", "conversations", "analyses"):
                self.db.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
            n = self.get_setting("purge_count", 0) + 1
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('purge_count', ?)", (json.dumps(n),))
        for d in export_dirs:
            for p in (Path(d), Path(d).with_suffix(".zip"), Path(d).with_suffix(".xlsx")) if d else ():
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()
                except OSError as e:
                    problems.append(f"{p.name}: {e}")
        att = self.root / "attachments" / project_id
        if att.exists():
            try:
                shutil.rmtree(att)
            except OSError as e:
                problems.append(f"附件目录: {e}")
        return {"cleared": impact, "problems": problems}

    def purge_conversation(self, conversation_id: str) -> None:
        with self.tx():
            self.db.execute("DELETE FROM events WHERE conversation_id=?", (conversation_id,))
            self.db.execute("DELETE FROM conversations WHERE conversation_id=?", (conversation_id,))

    def clear_workspace(self) -> dict:
        """清空本工作区所有业务数据（保留应用设置）。"""
        before = self.counts()
        problems = []
        with self.tx():
            for table in ("projects", "conversations", "snapshots", "runs", "documents", "library_materials",
                          "exports", "events", "analyses"):
                self.db.execute(f"DELETE FROM {table}")
        for sub in ("exports", "attachments"):
            d = self.root / sub
            try:
                shutil.rmtree(d)
            except OSError as e:
                problems.append(f"{sub}: {e}")
            d.mkdir(exist_ok=True)
        # Standards are independent reference resources, not project/chat data.
        # Their removal is an explicit operation in the standards manager.
        return {"cleared": before, "problems": problems}

    # ------------------------------------------------------------ 快照与运行（不可变）
    def save_snapshot(self, snap: dict) -> dict:
        with self.tx():
            ex = self._one("SELECT json FROM snapshots WHERE project_id=? AND scheme_id=? AND content_hash=? "
                           "ORDER BY version DESC LIMIT 1", snap["project_id"], snap["scheme_id"], snap["content_hash"])
            if ex:
                return json.loads(ex[0])
            v = (self._one("SELECT MAX(version) FROM snapshots WHERE project_id=? AND scheme_id=?",
                           snap["project_id"], snap["scheme_id"])[0] or 0) + 1
            snap = dict(snap, version=v, snapshot_id=f"{snap['project_id']}-{snap['scheme_id']}-v{v}")
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                            (snap["snapshot_id"], snap["project_id"], snap["scheme_id"], v, snap["content_hash"],
                             canonical_json(snap), snap["created_at"]))
        return snap

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        r = self._one("SELECT json FROM snapshots WHERE snapshot_id=?", snapshot_id)
        return None if r is None else json.loads(r[0])

    def save_run(self, run: dict) -> dict:
        with self.tx():
            ex = self._one("SELECT input_hash, json FROM runs WHERE request_id=?", run["request_id"])
            if ex:
                if ex[0] != run["input_hash"]:
                    raise StoreError(Code.IDEMPOTENCY_CONFLICT, "同一请求对应了不同输入，拒绝重复写入")
                return json.loads(ex[1])
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                            (run["run_id"], run.get("project_id"), run.get("conversation_id"), run["request_id"],
                             run["input_hash"], canonical_json(run), run["started_at"]))
        return run

    def find_run_by_request(self, request_id: str) -> dict | None:
        r = self._one("SELECT json FROM runs WHERE request_id=?", request_id)
        return None if r is None else json.loads(r[0])

    def get_run(self, run_id: str | None, project_id: str | None = None) -> dict | None:
        if not run_id:
            return None
        r = self._one("SELECT json, project_id FROM runs WHERE run_id=?", run_id)
        if r is None or (project_id is not None and r[1] != project_id):
            return None
        return json.loads(r[0])

    def list_runs(self, project_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self._all(
            "SELECT json FROM runs WHERE project_id=? ORDER BY created_at DESC, rowid DESC", project_id)]

    # ------------------------------------------------------------ 方案分析（每版不可变；重新生成形成新版本）
    def save_analysis(self, analysis: dict) -> dict:
        with self.tx():
            self.db.execute("INSERT INTO analyses VALUES (?,?,?,?,?,?,?)",
                            (analysis["analysis_id"], analysis["run_id"], analysis.get("project_id"),
                             analysis.get("conversation_id"), analysis["version"], canonical_json(analysis),
                             analysis["created_at"]))
        return analysis

    def get_analysis(self, analysis_id: str | None) -> dict | None:
        r = self._one("SELECT json FROM analyses WHERE analysis_id=?", analysis_id) if analysis_id else None
        return None if r is None else json.loads(r[0])

    def list_analyses(self, run_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self._all("SELECT json FROM analyses WHERE run_id=? ORDER BY version", run_id)]

    def next_analysis_version(self, run_id: str) -> int:
        return (self._one("SELECT MAX(version) FROM analyses WHERE run_id=?", run_id)[0] or 0) + 1

    # ------------------------------------------------------------ 资料
    def standard_library(self):
        """Separate reference records cannot enter project rules or parameters."""
        from storage.standard_library import StandardLibrary
        if not hasattr(self, "_standard_library"):
            self._standard_library = StandardLibrary(self.root)
        return self._standard_library

    def default_standard_selection(self):
        collections = self.standard_library().list_collections()
        available = {c['collection_id'] for c in collections}
        saved = self.get_setting('standard_collection_selection')
        if saved is not None:
            # Temporarily missing/disabled collections must not erase intent.
            return list(saved)
        builtins = [c['collection_id'] for c in collections if c.get('builtin') or c['collection_id']=='builtin-curtain-standards']
        return builtins or [c['collection_id'] for c in collections[:1]]

    def add_document(self, doc: dict, pages: list[dict]) -> dict:
        with self.tx():
            ex = self._one("SELECT json FROM documents WHERE sha256=? AND status='active'", doc["sha256"])
            if ex:
                return json.loads(ex[0]) | {"duplicate": True}
            self.db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                            (doc["doc_id"], doc["filename"], doc["sha256"], "active", canonical_json(doc),
                             json.dumps(pages, ensure_ascii=False), doc["created_at"], None))
        return doc

    def list_documents(self, status: str = "active") -> list[dict]:
        return [json.loads(r[0]) | {"status": r[1]} for r in self._all(
            "SELECT json, status FROM documents WHERE status=? ORDER BY created_at DESC", status)]

    def document_index(self) -> dict:
        docs, pages = [], []
        for r in self._all("SELECT json, pages FROM documents WHERE status='active'"):
            d = json.loads(r[0])
            docs.append(d)
            pages.extend(json.loads(r[1]))
        return {"documents": docs, "pages": pages}

    def set_document_status(self, doc_id: str, status: str) -> None:
        with self.tx():
            self.db.execute("UPDATE documents SET status=?, trashed_at=? WHERE doc_id=?",
                            (status, now_utc() if status == "trashed" else None, doc_id))

    def purge_document(self, doc_id: str) -> None:
        with self.tx():
            self.db.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        d = self.root / "attachments" / "_documents" / doc_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------ 个人材料库
    def save_library_material(self, mat: dict) -> None:
        with self.tx():
            self.db.execute("INSERT OR REPLACE INTO library_materials VALUES (?,?,?,?)",
                            (mat["mat_id"], "active", canonical_json(mat), mat.get("created_at") or now_utc()))

    def list_library(self) -> list[dict]:
        return [json.loads(r[0]) for r in self._all(
            "SELECT json FROM library_materials WHERE status='active' ORDER BY created_at DESC")]

    def delete_library_material(self, mat_id: str) -> None:
        with self.tx():
            self.db.execute("DELETE FROM library_materials WHERE mat_id=?", (mat_id,))

    # ------------------------------------------------------------ 导出与事件
    def find_export_by_request(self, request_id: str):
        r = self._one("SELECT json FROM exports WHERE request_id=?", request_id)
        return None if r is None else json.loads(r[0])

    def save_export(self, exp: dict) -> dict:
        with self.tx():
            ex = self._one("SELECT json FROM exports WHERE request_id=?", exp["request_id"])
            if ex:
                return json.loads(ex[0])
            self.db.execute("INSERT INTO exports VALUES (?,?,?,?,?,?)",
                            (exp["export_id"], exp["run_id"], exp.get("project_id"), exp["request_id"],
                             canonical_json(exp), exp["created_at"]))
        return exp

    def log(self, event_type: str, payload: dict, *, project_id=None, conversation_id=None, run_id=None) -> None:
        with self.tx():
            self.db.execute("INSERT INTO events (project_id, conversation_id, run_id, event_type, json, created_at) "
                            "VALUES (?,?,?,?,?,?)", (project_id, conversation_id, run_id, event_type,
                                                     canonical_json(payload), now_utc()))

    def events(self, *, project_id=None, run_id=None, limit=200) -> list[dict]:
        sql, args = "SELECT event_type, json, created_at FROM events WHERE 1=1", []
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        sql += " ORDER BY event_id DESC LIMIT ?"
        args.append(limit)
        return [{"event_type": r[0], "payload": json.loads(r[1]), "created_at": r[2]} for r in self._all(sql, *args)]


class _Tx:
    def __init__(self, ws: Workspace):
        self.ws = ws

    def __enter__(self):
        self.ws._lock.acquire()
        self.depth = self.ws._depth
        if self.depth == 0:
            self.ws.db.execute("BEGIN IMMEDIATE")
        self.ws._depth = self.depth + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ws._depth = self.depth
        try:
            if self.depth == 0:
                self.ws.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.ws._lock.release()
        return False
