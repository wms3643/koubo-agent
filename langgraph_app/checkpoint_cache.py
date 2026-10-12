"""PostgreSQL 检查点的 Redis 读缓存；持久化写入始终先经过数据库。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import uuid

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver

from .config import AppConfig

log = logging.getLogger(__name__)
_PROCESS = uuid.uuid4().hex
_STATES = {}
_STATES_LOCK = threading.Lock()
MAX_CACHE_BYTES = 2 * 1024 * 1024


class _CacheState:
    def __init__(self, url):
        from redis import Redis

        self.client = Redis.from_url(
            url, socket_connect_timeout=0.2, socket_timeout=0.2,
            retry_on_timeout=False,
        )
        self.lock = threading.RLock()
        self.generations = {}
        self.disabled = False

    def disable(self):
        if not self.disabled:
            log.warning("Checkpoint cache disabled; reads continue from PostgreSQL")
            self.disabled = True


class CachedPostgresSaver(PostgresSaver):
    """适用于单进程服务的读缓存，沿用官方 Saver 的写入和序列化语义。

    同一后端的读写使用共享锁；每次写入都提升线程缓存代数。
    Redis 故障后本进程停用该缓存，避免恢复连接时命中过期快照。
    进程标识隔离重启前缓存，旧键通过 TTL 回收。
    """

    def __init__(self, conn, config: AppConfig, *, purpose: str):
        super().__init__(conn)
        if purpose not in {"pipeline", "chat"}:
            raise ValueError("unknown checkpoint purpose")
        identity = json.dumps([config.database_url, purpose, config.redis_url])
        self.scope = hashlib.sha256(identity.encode()).hexdigest()
        self.ttl = config.checkpoint_cache_ttl
        with _STATES_LOCK:
            self.cache = _STATES.setdefault(self.scope, None)
            if self.cache is None:
                self.cache = _STATES[self.scope] = _CacheState(config.redis_url)

    def _key(self, config):
        c = config["configurable"]
        thread = str(c["thread_id"])
        generation = self.cache.generations.setdefault(thread, 0)
        identity = json.dumps([thread, c.get("checkpoint_ns", ""),
                               c.get("checkpoint_id"), generation])
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"koubo:checkpoint:v1:{_PROCESS}:{self.scope}:{digest}"

    def get_tuple(self, config):
        with self.cache.lock:
            if self.cache.disabled:
                return super().get_tuple(config)
            key = self._key(config)
            try:
                raw = self.cache.client.get(key)
                if raw:
                    kind, data = json.loads(raw)
                    value = self.serde.loads_typed((kind, base64.b64decode(data, validate=True)))
                    config_value, checkpoint, metadata, parent, writes = value
                    return CheckpointTuple(
                        config_value, checkpoint, metadata, parent,
                        [tuple(write) for write in writes] if writes is not None else None,
                    )
            except Exception:
                # Redis 及缓存解码错误不能影响持久化恢复。
                self.cache.disable()
            value = super().get_tuple(config)
            if value is not None and not self.cache.disabled:
                try:
                    kind, data = self.serde.dumps_typed(tuple(value))
                    raw = json.dumps([kind, base64.b64encode(data).decode()])
                    if len(raw) <= MAX_CACHE_BYTES:
                        self.cache.client.set(key, raw, ex=self.ttl)
                except Exception:
                    self.cache.disable()
            return value

    def _invalidate(self, thread_id):
        thread = str(thread_id)
        self.cache.generations[thread] = self.cache.generations.get(thread, 0) + 1

    def put(self, config, checkpoint, metadata, new_versions):
        with self.cache.lock:
            try:
                return super().put(config, checkpoint, metadata, new_versions)
            finally:
                self._invalidate(config["configurable"]["thread_id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        with self.cache.lock:
            try:
                return super().put_writes(config, writes, task_id, task_path)
            finally:
                self._invalidate(config["configurable"]["thread_id"])

    def delete_thread(self, thread_id):
        with self.cache.lock:
            try:
                return super().delete_thread(thread_id)
            finally:
                self._invalidate(thread_id)

    def delete_for_runs(self, run_ids):
        with self.cache.lock:
            try:
                return super().delete_for_runs(run_ids)
            finally:
                for thread in list(self.cache.generations):
                    self._invalidate(thread)

    def copy_thread(self, source_thread_id, target_thread_id):
        with self.cache.lock:
            try:
                return super().copy_thread(source_thread_id, target_thread_id)
            finally:
                self._invalidate(target_thread_id)

    def prune(self, thread_ids, *, strategy="keep_latest"):
        with self.cache.lock:
            try:
                return super().prune(thread_ids, strategy=strategy)
            finally:
                for thread in thread_ids:
                    self._invalidate(thread)
