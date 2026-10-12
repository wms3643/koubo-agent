-- ============================================================================
--  口播剪辑 Agent 平台 · PostgreSQL 数据模型草案（PostgreSQL 14+）
--  尚未接入当前应用；迁移、部署和并发行为仍需验证。
--  设计目标：多用户项目隔离、版本回滚、任务幂等与状态归档
--  三层存储分离：关系数据(本库) / 大对象(对象存储) / Agent 状态(JSONB)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- 项目名模糊搜索
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- 区间不重叠约束（剪辑区间）

-- ---------------------------------------------------------------------------
-- 1. 账号与额度
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id                  BIGSERIAL PRIMARY KEY,
    email               TEXT        NOT NULL,
    password_hash       TEXT        NOT NULL,
    display_name        TEXT        NOT NULL,
    plan                TEXT        NOT NULL DEFAULT 'free'
                        CHECK (plan IN ('free', 'pro', 'team')),
    monthly_quota_cents INTEGER     NOT NULL DEFAULT 0,   -- 每月额度（分）
    used_cents          INTEGER     NOT NULL DEFAULT 0,   -- 本期已消耗（分）
    status              TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended', 'deleted')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email)) WHERE status <> 'deleted';

-- ---------------------------------------------------------------------------
-- 2. 项目（聚合根；所有业务表都挂在它下面）
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT      NOT NULL REFERENCES users(id),
    name           TEXT        NOT NULL,
    description    TEXT,
    status         TEXT        NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'active', 'archived')),
    current_stage  TEXT        NOT NULL DEFAULT 'transcribe',  -- transcribe/cut/subs/overlay/render/qc
    row_version    INTEGER     NOT NULL DEFAULT 1,             -- 乐观锁
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ                                  -- 软删除
);
CREATE INDEX idx_projects_owner_status ON projects (owner_id, status, updated_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_projects_name_trgm ON projects USING gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- 3. 素材（视频等大文件只存对象存储 key，库内不落二进制）
-- ---------------------------------------------------------------------------
CREATE TABLE media_assets (
    id                BIGSERIAL PRIMARY KEY,
    project_id        BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind              TEXT        NOT NULL
                      CHECK (kind IN ('source_video', 'aroll', 'bgm', 'overlay', 'export')),
    storage_backend   TEXT        NOT NULL DEFAULT 's3'
                      CHECK (storage_backend IN ('s3', 'local')),   -- S3 兼容协议；开发环境用 local
    object_key        TEXT        NOT NULL,          -- 对象存储路径，不是文件内容
    storage_class     TEXT        NOT NULL DEFAULT 'standard'
                      CHECK (storage_class IN ('standard', 'infrequent', 'archive')),  -- 冷热分层
    upload_mode       TEXT        NOT NULL DEFAULT 'multipart'
                      CHECK (upload_mode IN ('simple', 'multipart')),                  -- 分片上传
    etag              TEXT,                          -- 分片上传的完成标识
    mime_type         TEXT        NOT NULL,
    size_bytes        BIGINT      NOT NULL,
    duration_ms       INTEGER,
    width             INTEGER,
    height            INTEGER,
    checksum_sha256   CHAR(64)    NOT NULL,          -- 秒传去重（前端上传前先算）
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, checksum_sha256)
);
CREATE INDEX idx_media_assets_project_kind ON media_assets (project_id, kind);

-- ---------------------------------------------------------------------------
-- 4. 转写（版本化：重转写不覆盖旧版本，只把 is_current 挪走）
-- ---------------------------------------------------------------------------
CREATE TABLE transcripts (
    id               BIGSERIAL PRIMARY KEY,
    project_id       BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_asset_id  BIGINT      NOT NULL REFERENCES media_assets(id),
    version          INTEGER     NOT NULL,
    is_current       BOOLEAN     NOT NULL DEFAULT TRUE,
    model            TEXT        NOT NULL,            -- faster-whisper large-v3
    language         TEXT        NOT NULL DEFAULT 'zh',
    word_timestamps  BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, version)
);
CREATE UNIQUE INDEX uq_transcripts_current ON transcripts (project_id) WHERE is_current;

-- 句 / 词级时间戳；words 用 JSONB（结构随模型版本变动，不适合拆列）
CREATE TABLE transcript_segments (
    id            BIGSERIAL PRIMARY KEY,
    transcript_id BIGINT      NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
    seg_index     INTEGER     NOT NULL,
    start_ms      INTEGER     NOT NULL,
    end_ms        INTEGER     NOT NULL,
    text          TEXT        NOT NULL,
    words         JSONB,                              -- [{w,t0,t1}, ...]
    confidence    REAL,
    CHECK (start_ms < end_ms),
    UNIQUE (transcript_id, seg_index)
);
CREATE INDEX idx_segments_transcript_time ON transcript_segments (transcript_id, start_ms);

-- 人工对逐字稿的修正（谁改了什么，可追溯、可回放）
CREATE TABLE text_corrections (
    id          BIGSERIAL PRIMARY KEY,
    segment_id  BIGINT      NOT NULL REFERENCES transcript_segments(id) ON DELETE CASCADE,
    before_text TEXT        NOT NULL,
    after_text  TEXT        NOT NULL,
    corrected_by BIGINT     NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_corrections_segment ON text_corrections (segment_id);

-- ---------------------------------------------------------------------------
-- 5. 剪辑方案与剪前、剪后时间映射
-- ---------------------------------------------------------------------------
CREATE TABLE cut_plans (
    id            BIGSERIAL PRIMARY KEY,
    project_id    BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    transcript_id BIGINT      NOT NULL REFERENCES transcripts(id),
    version       INTEGER     NOT NULL,
    is_current    BOOLEAN     NOT NULL DEFAULT TRUE,
    status        TEXT        NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'approved', 'superseded', 'rejected')),
    origin        TEXT        NOT NULL DEFAULT 'agent'
                  CHECK (origin IN ('agent', 'human')),
    note          TEXT,
    approved_by   BIGINT      REFERENCES users(id),
    approved_at   TIMESTAMPTZ,
    created_by    BIGINT      REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, version)
);
CREATE UNIQUE INDEX uq_cut_plans_current ON cut_plans (project_id) WHERE is_current;

-- 保留 / 删除区间：唯一的「真相源」
CREATE TABLE cut_plan_ranges (
    id          BIGSERIAL PRIMARY KEY,
    cut_plan_id BIGINT  NOT NULL REFERENCES cut_plans(id) ON DELETE CASCADE,
    action      TEXT    NOT NULL CHECK (action IN ('keep', 'delete')),
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    reason      TEXT,                                  -- 试音/重录/卡壳/空镜…
    CHECK (start_ms < end_ms),
    -- 同一方案内、同一动作的区间不允许重叠（数据库层挡住脏数据）
    EXCLUDE USING gist (
        cut_plan_id WITH =, action WITH =,
        int4range(start_ms, end_ms) WITH &&
    )
);
CREATE INDEX idx_cut_ranges_plan ON cut_plan_ranges (cut_plan_id, start_ms);

-- 剪前 → 剪后映射表：字幕与特效统一基于它重算（任一帧可反查原片时间码）
CREATE TABLE timeline_maps (
    id           BIGSERIAL PRIMARY KEY,
    cut_plan_id  BIGINT  NOT NULL REFERENCES cut_plans(id) ON DELETE CASCADE,
    src_start_ms INTEGER NOT NULL,
    src_end_ms   INTEGER NOT NULL,
    dst_start_ms INTEGER NOT NULL,
    dst_end_ms   INTEGER NOT NULL,
    CHECK (src_start_ms < src_end_ms AND dst_start_ms < dst_end_ms),
    CHECK ((src_end_ms - src_start_ms) = (dst_end_ms - dst_start_ms)),
    UNIQUE (cut_plan_id, src_start_ms)
);
CREATE INDEX idx_timeline_src ON timeline_maps (cut_plan_id, src_start_ms);
CREATE INDEX idx_timeline_dst ON timeline_maps (cut_plan_id, dst_start_ms);

-- 字幕轨（按方案版本 + 轨道版本存，改字幕不污染旧版本）
CREATE TABLE subtitle_cues (
    id           BIGSERIAL PRIMARY KEY,
    project_id   BIGINT  NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    cut_plan_id  BIGINT  NOT NULL REFERENCES cut_plans(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    is_current   BOOLEAN NOT NULL DEFAULT TRUE,
    cue_index    INTEGER NOT NULL,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    style        JSONB,                                -- ASS force_style 等
    source_seg_ids INTEGER[],                          -- 溯源到原始句
    CHECK (start_ms < end_ms),
    UNIQUE (cut_plan_id, version, cue_index)
);
CREATE INDEX idx_subs_current ON subtitle_cues (project_id, start_ms) WHERE is_current;

-- ---------------------------------------------------------------------------
-- 6. Agent 运行（可观测 + 可续跑 + 可算钱）
-- ---------------------------------------------------------------------------
CREATE TABLE agent_runs (
    id            BIGSERIAL PRIMARY KEY,
    project_id    BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id      BIGINT      NOT NULL REFERENCES users(id),
    thread_id     UUID        NOT NULL,               -- 与 LangGraph thread 对齐
    graph_version TEXT        NOT NULL,               -- 图结构变更后可追溯
    model         TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','waiting_human','succeeded','failed','cancelled')),
    current_node  TEXT,
    error_code    TEXT,
    cost_cents    INTEGER     NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_runs_thread ON agent_runs (thread_id, created_at DESC);
CREATE INDEX idx_runs_project_status ON agent_runs (project_id, status);

-- 每步一条：延迟 / token / 成本 全部可查（对应 SSE 实时推送的落库形态）
CREATE TABLE agent_steps (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT      NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_index        INTEGER     NOT NULL,
    node              TEXT        NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN ('ok','failed','skipped')),
    latency_ms        INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    caused_by_step    BIGINT      REFERENCES agent_steps(id),   -- 回退溯源
    payload           JSONB,                                    -- 节点输入/输出摘要
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, step_index)
);
CREATE INDEX idx_steps_run ON agent_steps (run_id, step_index);

-- 人工介入（interrupt → 人点批准/拒绝 → Command(resume) 续跑）
CREATE TABLE approvals (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT      NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_index    INTEGER,
    checkpoint_id TEXT        NOT NULL,
    decision      TEXT        CHECK (decision IN ('approve','reject')),
    comment       TEXT,
    payload       JSONB,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by    BIGINT      REFERENCES users(id),
    decided_at    TIMESTAMPTZ
);
CREATE INDEX idx_approvals_run ON approvals (run_id);
CREATE INDEX idx_approvals_pending ON approvals (requested_at) WHERE decision IS NULL;

-- Agent 状态检查点：结构不稳定 ⇒ JSONB；量大 ⇒ 可单独归档/清理
CREATE TABLE agent_checkpoints (
    id                   BIGSERIAL PRIMARY KEY,
    thread_id            UUID        NOT NULL,
    checkpoint_id        TEXT        NOT NULL,
    parent_checkpoint_id TEXT,
    state                JSONB       NOT NULL,
    metadata             JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, checkpoint_id)
);
CREATE INDEX idx_checkpoints_thread ON agent_checkpoints (thread_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 7. 渲染任务（幂等 + 重试 + 成本）
-- ---------------------------------------------------------------------------
CREATE TABLE render_jobs (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    cut_plan_id     BIGINT      REFERENCES cut_plans(id),
    spec            JSONB       NOT NULL,             -- assemble/mix 规格
    idempotency_key TEXT        NOT NULL,             -- sha256(project+plan+spec)，挡住重复付费渲染
    status          TEXT        NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    attempts        INTEGER     NOT NULL DEFAULT 0,
    max_attempts    INTEGER     NOT NULL DEFAULT 3,
    output_asset_id BIGINT      REFERENCES media_assets(id),
    error_code      TEXT,
    cost_cents      INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    UNIQUE (idempotency_key)
);
CREATE INDEX idx_render_jobs_queue ON render_jobs (status, created_at) WHERE status = 'queued';

-- 产物登记（SRT / 底片 / overlay / 成片 都在这）
CREATE TABLE artifacts (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id      BIGINT      REFERENCES render_jobs(id) ON DELETE SET NULL,
    kind        TEXT        NOT NULL
                CHECK (kind IN ('srt','aroll','overlay','final','qc_report','thumbnail')),
    asset_id    BIGINT      REFERENCES media_assets(id),
    object_key  TEXT,
    meta        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_artifacts_project_kind ON artifacts (project_id, kind, created_at DESC);

-- 成片质检明细（黑帧/削波/规格，异常定位到具体时间码）
CREATE TABLE qc_reports (
    id           BIGSERIAL PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES render_jobs(id) ON DELETE CASCADE,
    rule_code    TEXT        NOT NULL,                -- black_frame / audio_clip / spec_mismatch
    passed       BOOLEAN     NOT NULL,
    detail       TEXT,
    timecode_ms  INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_qc_job_failed ON qc_reports (job_id) WHERE passed = FALSE;

-- ---------------------------------------------------------------------------
-- 8. 审计（谁在什么时候动了什么，出问题能倒查）
-- ---------------------------------------------------------------------------
CREATE TABLE audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    BIGINT      REFERENCES users(id),
    action      TEXT        NOT NULL,                 -- approve_cut_plan / delete_project …
    entity_type TEXT        NOT NULL,
    entity_id   BIGINT,
    diff        JSONB,
    ip          INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_logs (entity_type, entity_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 9. 触发器：updated_at 自动维护
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_touch    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_projects_touch BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
