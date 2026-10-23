"""改版方案 §9.1：空白启动、生命周期与上下文（T01–T09）。"""
import json
import sqlite3

import pytest

from agent import migration
from agent.engine import is_current
from tests.chat_helpers import Chat, disp


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MW_FORCE_FALLBACK", "1")


def test_t01_blank_start_and_restart(tmp_path):
    c = Chat(tmp_path)
    assert all(v == 0 for v in c.ws.counts().values())
    # 重启（重新打开同一目录）仍为空，不生成任何演示记录
    c2 = Chat(tmp_path)
    assert all(v == 0 for v in c2.ws.counts().values())
    assert c2.ws.get_setting("active_dataset") is None


def test_t02_t33_query_without_project(tmp_path):
    c = Chat(tmp_path).new()
    c.say("先查资料：玻璃幕墙立柱的规范要求")
    m = c.last()
    assert m["kind"] == "query" and "还没有资料" in m["text"]
    assert c.ws.counts()["projects"] == 0  # 不建立空项目，也不追问几何或价格
    assert c.conv.get("pending_query")


def test_t03_create_from_scratch_without_presets(tmp_path):
    c = Chat(tmp_path).build_t10()
    p = c.project()
    assert p["name"] == "手工测试甲"
    blob = json.dumps(p, ensure_ascii=False)
    for legacy in ("S0", "S1", "COL-A", "BEAM-A", "DEMO-PRICE", "DEMO-R0"):
        assert legacy not in blob
    assert disp(c.run(), "baseline", "total_cost_cny") == "9,756.00"


def test_t04_two_projects_do_not_mix(tmp_path):
    a = Chat(tmp_path).build_t10()
    b = Chat(tmp_path).new()
    b.say("项目乙，宽12m、高9m，分格宽1.5m、高1.5m，立柱 3kg/m 20元/kg，横梁 1kg/m 30元/kg，算材料金额")
    b.click("确认并计算")
    a.say("把分格宽改为1.5m，与原方案比较")
    pa, pb = a.project(), b.project()
    assert pa["project_id"] != pb["project_id"]
    assert "candidate" in pa["schemes"] and "candidate" not in pb["schemes"]
    ra, rb = a.run(), b.run()
    assert ra["project_id"] == pa["project_id"] and rb["project_id"] == pb["project_id"]
    assert a.ws.get_run(rb["run_id"], pa["project_id"]) is None  # 按项目查询，不跨项目兜底
    # 刷新：按对话 ID 恢复，得到各自的项目
    assert Chat(tmp_path).a.project_of(a.ws.get_conversation(b.conv["conversation_id"]))["name"] == "项目乙"


def test_t05_t06_delete_restore(tmp_path):
    c = Chat(tmp_path).build_t10()
    pid = c.project()["project_id"]
    c.say("删除这个项目")
    assert c.last()["kind"] == "confirm"  # 不由聊天直接执行
    r = c.click("移入回收站")
    assert r.get("_deleted")
    again = Chat(tmp_path)
    assert again.ws.list_projects() == [] and again.ws.list_conversations() == []
    assert again.ws.get_project(pid) is None
    # 同名新项目
    n = Chat(tmp_path).new()
    n.say("手工测试甲，宽18m、高6m")
    again.ws.restore_project(pid)
    names = sorted(p["project_id"] for p in again.ws.list_projects())
    assert pid in names and len(names) == 2  # 恢复不覆盖同名新项目
    conv = again.ws.list_conversations(project_id=pid)
    assert conv and conv[0]["messages"]


def test_t07_purge_project(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.say("导出比选表")
    pid = c.project()["project_id"]
    c.ws.save_export({"export_id": "EXP-1", "run_id": c.run()["run_id"], "project_id": pid, "request_id": "R1",
                      "created_at": "x"})
    c.ws.trash_project(pid)
    res = c.ws.purge_project(pid)
    assert res["problems"] == []
    db = sqlite3.connect(tmp_path / "workspace.db")
    for table in ("projects", "conversations", "snapshots", "runs", "exports", "events"):
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (pid,)).fetchone()[0] == 0, table
    assert c.ws.get_setting("purge_count") == 1


def test_t08_library_material_kept(tmp_path):
    c = Chat(tmp_path).build_t10()
    pid = c.project()["project_id"]
    lib = c.a.save_to_library(pid, "baseline", "column")
    imp = c.ws.project_impact(pid)
    assert imp["materials"] == 2
    c.ws.trash_project(pid)
    c.ws.purge_project(pid)
    assert [x["mat_id"] for x in c.ws.list_library()] == [lib]


def test_clear_workspace_stays_blank(tmp_path):
    c = Chat(tmp_path).build_t10()
    c.ws.add_document({"doc_id": "D1", "filename": "a.txt", "sha256": "x", "created_at": "t"}, [])
    r = c.ws.clear_workspace()
    assert r["problems"] == []
    assert all(v == 0 for v in Chat(tmp_path).ws.counts().values())


def _old_db(path):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE runs (run_id TEXT, request_id TEXT, input_hash TEXT, draft_id TEXT, "
                      "execution_state TEXT, json TEXT, created_at TEXT); CREATE TABLE drafts (draft_id TEXT);")
    from tests.helpers import demo_package, snap
    pkg = demo_package()
    run = {"run_id": "RUN-old", "draft_id": "D1", "requested_task": "cost_compare", "case_id": "DEMO-CW-001",
           "snapshots": {"baseline": snap(pkg, "S0"), "candidate": snap(pkg, "S1")}}
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", ("RUN-old", "R", "h", "D1", "COMPLETE", json.dumps(run), "t"))
    con.execute("INSERT INTO drafts VALUES ('D1')")
    con.commit()
    con.close()


@pytest.mark.parametrize("choice", ["keep", "trash"])
def test_t09_migration_choice(tmp_path, monkeypatch, choice):
    old = tmp_path / "old" / "app.db"
    old.parent.mkdir()
    _old_db(old)
    monkeypatch.setattr(migration, "OLD_DB", old)
    c = Chat(tmp_path / "ws")
    assert migration.pending(c.ws)["runs"] == 1
    r = migration.migrate(c.ws, choice)
    assert r["converted"] == 1 and old.exists() and (old.parent / r["backup"].split("/")[-1].split("\\")[-1]).exists()
    active, trashed = c.ws.list_projects(), c.ws.list_projects("trashed")
    assert (len(active), len(trashed)) == ((1, 0) if choice == "keep" else (0, 1))
    assert migration.pending(c.ws) is None
    if choice == "keep":
        p = active[0]
        conv = c.ws.list_conversations(project_id=p["project_id"])[0]
        c.conv = conv
        c.say("比较两个方案")
        assert disp(c.run(), "candidate", "total_cost_cny") == "7,776.00"  # 旧 S1 金额由程序重算
        assert is_current(c.project(), c.run())


def test_t09_ignore_leaves_old_file(tmp_path, monkeypatch):
    old = tmp_path / "app.db"
    _old_db(old)
    monkeypatch.setattr(migration, "OLD_DB", old)
    c = Chat(tmp_path / "ws")
    migration.migrate(c.ws, "ignore")
    assert old.exists() and c.ws.counts()["projects"] == 0 and migration.pending(c.ws) is None
