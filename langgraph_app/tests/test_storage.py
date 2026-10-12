"""存储回归；设置 KB_TEST_DATABASE_URL 后运行真实 PostgreSQL 用例。"""

from dataclasses import replace
import os
import subprocess
import sys
from contextlib import closing
import uuid

import pytest
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from langgraph_app.config import AppConfig
from langgraph_app.graph import make_checkpointer, open_checkpoint_db
from langgraph_app.storage import (
    check_database, initialize_database, load_conversations, save_conversations,
)


class ApprovalState(TypedDict, total=False):
    text: str
    approved: bool


def approval_graph(conn, config=None, *, purpose="pipeline"):
    graph = StateGraph(ApprovalState)

    def approve(state):
        return {"approved": bool(interrupt({"text": state["text"]}))}

    graph.add_node("approval", approve)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)
    return graph.compile(checkpointer=make_checkpointer(conn, config, purpose=purpose))


def test_sqlite_resume_and_chat_isolation(tmp_path):
    config = AppConfig(checkpoint_db=tmp_path / "checkpoints.sqlite", projects_root=tmp_path)
    thread = {"configurable": {"thread_id": "same-id"}}
    with closing(open_checkpoint_db(config)) as conn:
        app = approval_graph(conn)
        app.invoke({"text": "批准剪辑"}, thread)
        assert app.get_state(thread).interrupts
    with closing(open_checkpoint_db(config)) as conn:
        app = approval_graph(conn)
        assert app.invoke(Command(resume=True), thread)["approved"] is True
    with closing(open_checkpoint_db(config, purpose="chat")) as conn:
        assert not approval_graph(conn).get_state(thread).values


def test_local_conversation_roundtrip(tmp_path):
    config = AppConfig(checkpoint_db=tmp_path / "state" / "checkpoints.sqlite")
    payload = {"version": 1, "conversations": [{"id": "first", "title": "中文标题"}]}
    assert not load_conversations(config)["conversations"]
    save_conversations(config, payload)
    assert load_conversations(config) == payload
    assert not (tmp_path / "state" / "conversations.json.tmp").exists()


def test_config_backend_and_secret_repr(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@localhost/koubo")
    config = AppConfig.from_env()
    assert config.storage_backend == "postgres"
    assert "secret" not in repr(config)
    monkeypatch.delenv("DATABASE_URL")
    assert AppConfig.from_env().storage_backend == "sqlite"


def test_invalid_purpose_rejected(tmp_path):
    with pytest.raises(ValueError):
        open_checkpoint_db(AppConfig(checkpoint_db=tmp_path / "state.sqlite"), purpose="bad")
    assert not (tmp_path / "state.sqlite").exists()


@pytest.fixture(scope="module")
def postgres_config(tmp_path_factory):
    url = os.environ.get("KB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KB_TEST_DATABASE_URL to run PostgreSQL integration tests")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    # 独立测试数据库；不会清理连接地址中原数据库的数据。
    database = "koubo_test_" + uuid.uuid4().hex
    root = tmp_path_factory.mktemp("postgres-storage")
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    config = AppConfig(
        database_url=make_conninfo(url, dbname=database),
        projects_root=root, checkpoint_db=root / "state" / "unused.sqlite",
    )
    try:
        initialize_database(config)
        yield config
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def test_postgres_init_is_idempotent(postgres_config):
    initialize_database(postgres_config)
    check_database(postgres_config)
    assert not postgres_config.checkpoint_db.exists()


def test_postgres_approval_survives_reconnection(postgres_config):
    thread = {"configurable": {"thread_id": "pending-" + uuid.uuid4().hex}}
    with closing(open_checkpoint_db(postgres_config)) as conn:
        app = approval_graph(conn)
        app.invoke({"text": "确认付费任务"}, thread)
        assert app.get_state(thread).interrupts
    with closing(open_checkpoint_db(postgres_config)) as conn:
        app = approval_graph(conn)
        assert app.get_state(thread).interrupts[0].value["text"] == "确认付费任务"
        assert app.invoke(Command(resume=False), thread)["approved"] is False
        assert not app.get_state(thread).interrupts


def test_postgres_approval_survives_process_restart(postgres_config):
    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_config.database_url
    env["KB_RECOVERY_THREAD"] = "process-" + uuid.uuid4().hex
    script = """
import os
import sys
from contextlib import closing
from langgraph.types import Command
from langgraph_app.config import AppConfig
from langgraph_app.graph import open_checkpoint_db
from langgraph_app.tests.test_storage import approval_graph
thread = {"configurable": {"thread_id": os.environ["KB_RECOVERY_THREAD"]}}
with closing(open_checkpoint_db(AppConfig.from_env())) as conn:
    app = approval_graph(conn)
    if sys.argv[1] == "write":
        app.invoke({"text": "process recovery"}, thread)
        assert app.get_state(thread).interrupts
    else:
        assert app.get_state(thread).interrupts[0].value["text"] == "process recovery"
        assert app.invoke(Command(resume=True), thread)["approved"] is True
        assert not app.get_state(thread).interrupts
"""
    for action in ("write", "resume"):
        result = subprocess.run(
            [sys.executable, "-c", script, action], env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr


def test_postgres_chat_memory_is_independent(postgres_config):
    thread = {"configurable": {"thread_id": "isolated-" + uuid.uuid4().hex}}
    with closing(open_checkpoint_db(postgres_config, purpose="chat")) as conn:
        app = approval_graph(conn)
        app.invoke({"text": "对话记忆"}, thread)
    with closing(open_checkpoint_db(postgres_config)) as conn:
        assert not approval_graph(conn).get_state(thread).values
    with closing(open_checkpoint_db(postgres_config, purpose="chat")) as conn:
        assert approval_graph(conn).get_state(thread).values["text"] == "对话记忆"
        make_checkpointer(conn).delete_thread(thread["configurable"]["thread_id"])
        assert not approval_graph(conn).get_state(thread).values


def test_postgres_conversations_roundtrip_and_workspace_scope(postgres_config):
    payload = {"version": 1, "conversations": [{"id": "test", "title": "项目记录", "events": []}]}
    save_conversations(postgres_config, payload)
    assert load_conversations(postgres_config) == payload
    other = replace(postgres_config, projects_root=postgres_config.projects_root / "another")
    assert not load_conversations(other)["conversations"]
    save_conversations(postgres_config, {"version": 1, "conversations": []})
    assert not load_conversations(postgres_config)["conversations"]


def test_postgres_pipeline_resumes_without_repeating_paid_step(postgres_config, tmp_path):
    from langgraph_app.tests.test_graph_e2e import FakeClient, FAIL, make_app, run_app

    client = FakeClient(script={"bgm:probe": [FAIL("kb_bgm", "bgm_needs_confirm")]})
    thread = "pipeline-" + uuid.uuid4().hex
    config = replace(postgres_config, projects_root=tmp_path)
    with closing(open_checkpoint_db(config)) as conn:
        app = make_app(config, client, checkpointer=make_checkpointer(conn, config))
        _, graph_config = run_app(app, tmp_path, thread=thread)
        assert app.get_state(graph_config).interrupts
        assert client.calls.count("bgm:paid") == 0
    with closing(open_checkpoint_db(config)) as conn:
        app = make_app(config, client, checkpointer=make_checkpointer(conn, config))
        result = app.invoke(Command(resume=True), graph_config)
        assert not result.get("halts")
        assert client.calls.count("bgm:paid") == 1
        assert result["approvals"]["paid:bgm"] is True


def test_postgres_web_restart_and_delete_memory(postgres_config, monkeypatch):
    from fastapi.testclient import TestClient
    from langgraph_app.webapp import server

    monkeypatch.setattr(server, "_config", lambda: postgres_config)
    monkeypatch.setattr(server, "_CONVS", {})
    monkeypatch.setattr(server, "_CONVS_LOADED", False)
    with TestClient(server.app) as web:
        assert web.get("/api/health").json()["storage"] == "postgres"
        response = web.post("/api/conversations", json={"title": "重启验证"})
        assert response.status_code == 200
        cid = response.json()["id"]
    with closing(open_checkpoint_db(postgres_config, purpose="chat")) as conn:
        approval_graph(conn).invoke({"text": "旧记忆"}, {"configurable": {"thread_id": cid}})
    server._CONVS = {}
    server._CONVS_LOADED = False
    with TestClient(server.app) as web:
        assert any(c["id"] == cid for c in web.get("/api/conversations").json()["conversations"])
        assert web.delete(f"/api/conversations/{cid}").status_code == 200
    with closing(open_checkpoint_db(postgres_config, purpose="chat")) as conn:
        assert not approval_graph(conn).get_state({"configurable": {"thread_id": cid}}).values


def test_postgres_failure_does_not_fall_back_to_sqlite(tmp_path):
    import psycopg

    config = AppConfig(database_url="postgresql://localhost:1/missing", checkpoint_db=tmp_path / "local.sqlite")
    with pytest.raises(psycopg.OperationalError):
        open_checkpoint_db(config)
    assert not config.checkpoint_db.exists()


def test_database_failure_health_and_startup(monkeypatch):
    from fastapi.testclient import TestClient
    from langgraph_app import storage
    from langgraph_app.webapp import server

    config = AppConfig(database_url="postgresql://localhost:1/missing")
    monkeypatch.setattr(server, "_config", lambda: config)

    def unavailable(_config):
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr(storage, "check_database", unavailable)
    # 不进入 lifespan，单独验证健康接口；启动失败在下方独立验证。
    response = TestClient(server.app).get("/api/health")
    assert response.status_code == 503
    assert "postgresql://" not in response.text
    with pytest.raises(RuntimeError, match="Database unavailable"):
        with TestClient(server.app):
            pass


@pytest.fixture
def cached_config(postgres_config):
    url = os.environ.get("KB_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set KB_TEST_REDIS_URL to run Redis integration tests")
    from redis import Redis

    client = Redis.from_url(url)
    try:
        assert client.ping()
    finally:
        client.close()
    return replace(postgres_config, redis_url=url)


def test_redis_hit_expiry_and_pending_write_invalidation(cached_config, monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver

    thread = {"configurable": {"thread_id": "cache-" + uuid.uuid4().hex}}
    with closing(open_checkpoint_db(cached_config)) as conn:
        app = approval_graph(conn, cached_config)
        app.invoke({"text": "cached approval"}, thread)
        saver = app.checkpointer
        first = saver.get_tuple(thread)
        key = saver._key(thread)
        assert 0 < saver.cache.client.ttl(key) <= cached_config.checkpoint_cache_ttl
        original = PostgresSaver.get_tuple
        calls = []

        def counted(self, config):
            calls.append(config)
            return original(self, config)

        monkeypatch.setattr(PostgresSaver, "get_tuple", counted)
        assert saver.get_tuple(thread) == first
        assert not calls  # 热缓存实际跳过 PostgreSQL 快照查询。
        saver.cache.client.pexpire(key, 1)
        import time
        time.sleep(0.02)
        assert saver.get_tuple(thread) == first
        assert len(calls) == 1
        saver.put_writes(first.config, [("extra", "new write")], "cache-test-task")
        assert ("cache-test-task", "extra", "new write") in saver.get_tuple(thread).pending_writes
        assert len(calls) == 2
        assert app.invoke(Command(resume=True), thread)["approved"] is True
        assert not app.get_state(thread).interrupts


def test_redis_shared_invalidation_and_schema_isolation(cached_config):
    thread = {"configurable": {"thread_id": "shared-" + uuid.uuid4().hex}}
    with closing(open_checkpoint_db(cached_config)) as one, closing(open_checkpoint_db(cached_config)) as two:
        app = approval_graph(one, cached_config)
        app.invoke({"text": "shared"}, thread)
        reader = make_checkpointer(two, cached_config)
        assert reader.get_tuple(thread)
        app.checkpointer.delete_thread(thread["configurable"]["thread_id"])
        assert reader.get_tuple(thread) is None
    with closing(open_checkpoint_db(cached_config, purpose="chat")) as conn:
        app = approval_graph(conn, cached_config, purpose="chat")
        app.invoke({"text": "chat only"}, thread)
        assert app.checkpointer.get_tuple(thread)
    with closing(open_checkpoint_db(cached_config)) as conn:
        assert make_checkpointer(conn, cached_config).get_tuple(thread) is None


def test_redis_corruption_falls_back_and_stays_disabled(cached_config):
    from langgraph_app.checkpoint_cache import _STATES

    thread = {"configurable": {"thread_id": "corrupt-" + uuid.uuid4().hex}}
    with closing(open_checkpoint_db(cached_config)) as conn:
        saver = make_checkpointer(conn, cached_config)
        app = approval_graph(conn, cached_config)
        app.invoke({"text": "recover from database"}, thread)
        assert saver.get_tuple(thread)
        saver.cache.client.set(saver._key(thread), b"invalid cache", ex=60)
        try:
            assert saver.get_tuple(thread).checkpoint["channel_values"]["text"] == "recover from database"
            assert saver.cache.disabled
            assert app.invoke(Command(resume=False), thread)["approved"] is False
        finally:
            _STATES.pop(saver.scope, None)


def test_redis_unavailable_still_persists_and_resumes(postgres_config):
    config = replace(postgres_config, redis_url="redis://127.0.0.1:1/0")
    thread = {"configurable": {"thread_id": "offline-" + uuid.uuid4().hex}}
    with closing(open_checkpoint_db(config)) as conn:
        app = approval_graph(conn, config)
        app.invoke({"text": "no redis"}, thread)
        assert app.checkpointer.cache.disabled
        assert app.get_state(thread).interrupts
    with closing(open_checkpoint_db(config)) as conn:
        assert approval_graph(conn, config).invoke(Command(resume=True), thread)["approved"] is True


def test_cache_config_and_sqlite_bypass(monkeypatch, tmp_path):
    monkeypatch.setenv("REDIS_URL", "redis://:secret@localhost/0")
    monkeypatch.setenv("KB_CHECKPOINT_CACHE_TTL", "30")
    config = AppConfig.from_env(checkpoint_db=tmp_path / "sqlite.db", database_url=None)
    assert config.checkpoint_cache_ttl == 30
    assert "secret" not in repr(config)
    with closing(open_checkpoint_db(config)) as conn:
        from langgraph.checkpoint.sqlite import SqliteSaver
        assert isinstance(make_checkpointer(conn, config), SqliteSaver)
    with pytest.raises(ValueError):
        replace(config, checkpoint_cache_ttl=0)


def test_redis_pipeline_paid_approval_recovery(cached_config, tmp_path):
    test_postgres_pipeline_resumes_without_repeating_paid_step(cached_config, tmp_path)


def test_redis_web_conversation_recovery(cached_config, monkeypatch):
    test_postgres_web_restart_and_delete_memory(cached_config, monkeypatch)
