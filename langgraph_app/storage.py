"""PostgreSQL 运行存储：检查点连接、会话快照及显式迁移。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import AppConfig

SCHEMAS = ("koubo_app", "koubo_pipeline", "koubo_chat")
MIGRATION_VERSION = 1


def connect_postgres(config: AppConfig, *, schema: str = "koubo_app"):
    if not config.database_url:
        raise ValueError("DATABASE_URL is required for PostgreSQL storage")
    if schema not in SCHEMAS:
        raise ValueError("unknown storage schema")
    import psycopg
    from psycopg.rows import dict_row

    # PostgresSaver 要求 autocommit 和 dict_row。schema 仅接受内部常量。
    return psycopg.connect(
        config.database_url,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
        connect_timeout=10,
        options=f"-c search_path={schema} -c statement_timeout=30000",
    )


def initialize_database(config: AppConfig) -> None:
    """应用迁移与官方 Saver 迁移。重复执行安全，不清理已有数据。"""
    from langgraph.checkpoint.postgres import PostgresSaver

    with connect_postgres(config) as conn:
        conn.execute("SELECT pg_advisory_lock(1262633551)")
        try:
            with conn.transaction():
                for schema in SCHEMAS:
                    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS koubo_app.schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS koubo_app.conversation_snapshots (
                        workspace_key TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        CHECK (jsonb_typeof(payload) = 'object')
                    )
                """)
                conn.execute("""
                    INSERT INTO koubo_app.schema_migrations (version)
                    VALUES (%s) ON CONFLICT DO NOTHING
                """, (MIGRATION_VERSION,))
            # Saver 的迁移包含非事务 DDL，由官方 setup 管理。
            for purpose in ("pipeline", "chat"):
                with connect_postgres(config, schema=f"koubo_{purpose}") as cp_conn:
                    PostgresSaver(cp_conn).setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(1262633551)")


def check_database(config: AppConfig) -> None:
    """确认连接与必要迁移可用；配置失败不会回退到 SQLite。"""
    with connect_postgres(config) as conn:
        for name in (
            "koubo_app.schema_migrations", "koubo_app.conversation_snapshots",
            "koubo_pipeline.checkpoints", "koubo_pipeline.checkpoint_writes",
            "koubo_pipeline.checkpoint_blobs", "koubo_chat.checkpoints",
            "koubo_chat.checkpoint_writes", "koubo_chat.checkpoint_blobs",
        ):
            row = conn.execute("SELECT to_regclass(%s) AS table_name", (name,)).fetchone()
            if not row["table_name"]:
                raise RuntimeError("Database migration required: python -m langgraph_app.database init")
        row = conn.execute(
            "SELECT 1 FROM koubo_app.schema_migrations WHERE version = %s",
            (MIGRATION_VERSION,),
        ).fetchone()
        if not row:
            raise RuntimeError("Database migration version is missing")


def workspace_key(config: AppConfig) -> str:
    root = str(Path(config.projects_root).resolve())
    return hashlib.sha256(root.encode("utf-8")).hexdigest()


def load_conversations(config: AppConfig) -> dict:
    if config.database_url:
        with connect_postgres(config) as conn:
            row = conn.execute(
                "SELECT payload FROM koubo_app.conversation_snapshots WHERE workspace_key = %s",
                (workspace_key(config),),
            ).fetchone()
            return row["payload"] if row else {"version": 1, "conversations": []}
    path = Path(config.checkpoint_db).parent / "conversations.json"
    if not path.is_file():
        return {"version": 1, "conversations": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_conversations(config: AppConfig, payload: dict) -> None:
    if config.database_url:
        from psycopg.types.json import Jsonb

        with connect_postgres(config) as conn:
            conn.execute("""
                INSERT INTO koubo_app.conversation_snapshots (workspace_key, payload)
                VALUES (%s, %s)
                ON CONFLICT (workspace_key) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = now()
            """, (workspace_key(config), Jsonb(payload)))
        return
    path = Path(config.checkpoint_db).parent / "conversations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
