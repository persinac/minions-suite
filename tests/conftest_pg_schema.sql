-- Test schema for the pytest Postgres fixture (tests/conftest.py).
--
-- GENERATED — do not hand-edit. Regenerate after adding a migration:
--
--   docker run -d --name pg -e POSTGRES_USER=minion -e POSTGRES_PASSWORD=minion \
--     -e POSTGRES_DB=minion -p 5434:5432 pgvector/pgvector:pg17
--   psql "$URL" -c 'CREATE SCHEMA IF NOT EXISTS minions'
--   dbmate --url "$URL" --migrations-dir database/pgsql/migrations \
--     --migrations-table minions.schema_migrations --no-dump-schema up
--   pg_dump -d minion --schema=minions --schema-only --no-owner --no-privileges \
--     --no-comments --exclude-table=minions.schema_migrations \
--     | grep -vE '^(CREATE SCHEMA|ALTER SCHEMA|CREATE EXTENSION|COMMENT ON|SET |SELECT pg_catalog|--)' \
--     | grep -vE '^\\' | sed -E 's/\bminions\.//g'
--
-- Objects are intentionally UNQUALIFIED: conftest creates a `minions_test` schema
-- and sets search_path to it before executing this file.
--
-- Requires the `vector` extension at database level (memory_nodes.embedding is
-- vector(1536)); pgvector/pgvector:pg17 provides it.
CREATE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;
CREATE TABLE agents (
    id text NOT NULL,
    job_id text NOT NULL,
    role text NOT NULL,
    task_id text,
    pid integer,
    status text DEFAULT 'starting'::text NOT NULL,
    host text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    error text,
    log_file text,
    input_tokens integer DEFAULT 0,
    output_tokens integer DEFAULT 0,
    cache_read_tokens integer DEFAULT 0,
    cache_creation_tokens integer DEFAULT 0,
    cost_usd real DEFAULT 0.0,
    num_turns integer DEFAULT 0,
    model text,
    k8s_job_name text
);
CREATE TABLE events (
    id bigint NOT NULL,
    job_id text,
    event_type text NOT NULL,
    source text,
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE events_id_seq OWNED BY events.id;
CREATE TABLE heartbeats (
    agent_id text NOT NULL,
    agent_role text NOT NULL,
    job_id text,
    current_task_id text,
    current_subtask_id text,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'idle'::text NOT NULL
);
CREATE TABLE jobs (
    id text NOT NULL,
    spec text NOT NULL,
    status text DEFAULT 'spec_received'::text NOT NULL,
    correlation_id text,
    external_id text,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    job_type text DEFAULT 'development'::text NOT NULL,
    mr_url text,
    gitlab_issue_id text,
    difficulty text
);
CREATE TABLE memory_entities (
    id text NOT NULL,
    name text NOT NULL,
    entity_type text,
    project text NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL
);
CREATE TABLE memory_links (
    id integer NOT NULL,
    from_node text NOT NULL,
    to_entity text NOT NULL,
    link_type text NOT NULL,
    confidence real DEFAULT 1.0 NOT NULL,
    reasoning text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE memory_links_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE memory_links_id_seq OWNED BY memory_links.id;
CREATE TABLE memory_nodes (
    id text NOT NULL,
    content text NOT NULL,
    title text,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    embedding public.vector(1536),
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_job_id text,
    source_agent_role text,
    project text NOT NULL,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE memory_operations (
    id integer NOT NULL,
    op text NOT NULL,
    tier text DEFAULT ''::text NOT NULL,
    project text DEFAULT ''::text NOT NULL,
    job_id text DEFAULT ''::text NOT NULL,
    agent_role text DEFAULT ''::text NOT NULL,
    duration_ms real DEFAULT 0.0 NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE memory_operations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE memory_operations_id_seq OWNED BY memory_operations.id;
CREATE TABLE message_log (
    id bigint NOT NULL,
    nats_subject text NOT NULL,
    nats_sequence bigint,
    job_id text,
    from_role text,
    to_role text,
    message_type text NOT NULL,
    payload jsonb,
    correlation_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE message_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE message_log_id_seq OWNED BY message_log.id;
CREATE TABLE messages (
    id text NOT NULL,
    job_id text NOT NULL,
    from_role text NOT NULL,
    to_role text,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE state_transitions (
    id bigint NOT NULL,
    job_id text,
    task_id text,
    subtask_id text,
    agent_id text,
    from_status text NOT NULL,
    to_status text NOT NULL,
    approved boolean NOT NULL,
    rejection_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE state_transitions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE state_transitions_id_seq OWNED BY state_transitions.id;
CREATE TABLE subtasks (
    id text NOT NULL,
    task_id text NOT NULL,
    sequence_num integer NOT NULL,
    description text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    timeout_seconds integer DEFAULT 120 NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    result jsonb,
    error text,
    attempt integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE tasks (
    id text NOT NULL,
    job_id text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    service text NOT NULL,
    agent_role text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    branch_name text,
    pr_number integer,
    pr_url text,
    review_status text,
    deploy_status text,
    revision_count integer DEFAULT 0 NOT NULL,
    timeout_seconds integer DEFAULT 600 NOT NULL,
    claimed_by text,
    attempt integer DEFAULT 1 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    mr_url text,
    mr_id text,
    specialty text,
    verdict text,
    comments_posted integer DEFAULT 0 NOT NULL
);
CREATE TABLE tool_calls (
    id bigint NOT NULL,
    job_id text,
    tool_name text NOT NULL,
    params jsonb,
    result text,
    error text,
    duration_ms real,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE tool_calls_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE tool_calls_id_seq OWNED BY tool_calls.id;
ALTER TABLE ONLY events ALTER COLUMN id SET DEFAULT nextval('events_id_seq'::regclass);
ALTER TABLE ONLY memory_links ALTER COLUMN id SET DEFAULT nextval('memory_links_id_seq'::regclass);
ALTER TABLE ONLY memory_operations ALTER COLUMN id SET DEFAULT nextval('memory_operations_id_seq'::regclass);
ALTER TABLE ONLY message_log ALTER COLUMN id SET DEFAULT nextval('message_log_id_seq'::regclass);
ALTER TABLE ONLY state_transitions ALTER COLUMN id SET DEFAULT nextval('state_transitions_id_seq'::regclass);
ALTER TABLE ONLY tool_calls ALTER COLUMN id SET DEFAULT nextval('tool_calls_id_seq'::regclass);
ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);
ALTER TABLE ONLY events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY heartbeats
    ADD CONSTRAINT heartbeats_pkey PRIMARY KEY (agent_id);
ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY memory_entities
    ADD CONSTRAINT memory_entities_name_project_key UNIQUE (name, project);
ALTER TABLE ONLY memory_entities
    ADD CONSTRAINT memory_entities_pkey PRIMARY KEY (id);
ALTER TABLE ONLY memory_links
    ADD CONSTRAINT memory_links_pkey PRIMARY KEY (id);
ALTER TABLE ONLY memory_nodes
    ADD CONSTRAINT memory_nodes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY memory_operations
    ADD CONSTRAINT memory_operations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY message_log
    ADD CONSTRAINT message_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY state_transitions
    ADD CONSTRAINT state_transitions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY subtasks
    ADD CONSTRAINT subtasks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY tool_calls
    ADD CONSTRAINT tool_calls_pkey PRIMARY KEY (id);
CREATE INDEX idx_agents_job ON agents USING btree (job_id, started_at);
CREATE INDEX idx_agents_k8s_job ON agents USING btree (k8s_job_name) WHERE (k8s_job_name IS NOT NULL);
CREATE INDEX idx_agents_status ON agents USING btree (status);
CREATE INDEX idx_events_created ON events USING btree (created_at);
CREATE INDEX idx_events_job ON events USING btree (job_id);
CREATE INDEX idx_events_type ON events USING btree (event_type);
CREATE INDEX idx_heartbeats_status ON heartbeats USING btree (status, last_seen);
CREATE INDEX idx_jobs_external_id ON jobs USING btree (external_id) WHERE (external_id IS NOT NULL);
CREATE INDEX idx_jobs_gitlab_issue ON jobs USING btree (gitlab_issue_id) WHERE (gitlab_issue_id IS NOT NULL);
CREATE INDEX idx_jobs_mr_url ON jobs USING btree (mr_url) WHERE (mr_url IS NOT NULL);
CREATE INDEX idx_jobs_status ON jobs USING btree (status);
CREATE INDEX idx_jobs_type ON jobs USING btree (job_type);
CREATE INDEX idx_memory_entities_project ON memory_entities USING btree (project);
CREATE INDEX idx_memory_links_from_node ON memory_links USING btree (from_node);
CREATE INDEX idx_memory_links_to_entity ON memory_links USING btree (to_entity);
CREATE INDEX idx_memory_nodes_created_at ON memory_nodes USING btree (created_at);
CREATE INDEX idx_memory_nodes_embedding ON memory_nodes USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');
CREATE INDEX idx_memory_nodes_project ON memory_nodes USING btree (project);
CREATE INDEX idx_memory_nodes_tags ON memory_nodes USING gin (tags);
CREATE INDEX idx_memory_operations_created_at ON memory_operations USING btree (created_at DESC);
CREATE INDEX idx_memory_operations_project ON memory_operations USING btree (project);
CREATE INDEX idx_message_log_correlation ON message_log USING btree (correlation_id) WHERE (correlation_id IS NOT NULL);
CREATE INDEX idx_message_log_job ON message_log USING btree (job_id, created_at);
CREATE INDEX idx_messages_job_role ON messages USING btree (job_id, to_role);
CREATE INDEX idx_subtasks_status ON subtasks USING btree (status);
CREATE INDEX idx_subtasks_task ON subtasks USING btree (task_id, sequence_num);
CREATE INDEX idx_tasks_job ON tasks USING btree (job_id, created_at);
CREATE INDEX idx_tasks_mr_url ON tasks USING btree (mr_url) WHERE (mr_url IS NOT NULL);
CREATE INDEX idx_tasks_retry ON tasks USING btree (status, attempt) WHERE ((status = 'failed'::text) AND (attempt < max_attempts));
CREATE INDEX idx_tasks_status_role ON tasks USING btree (status, agent_role);
CREATE INDEX idx_tasks_stuck ON tasks USING btree (status, updated_at) WHERE (status <> ALL (ARRAY['done'::text, 'failed'::text]));
CREATE INDEX idx_tool_calls_created ON tool_calls USING btree (created_at);
CREATE INDEX idx_tool_calls_job ON tool_calls USING btree (job_id);
CREATE INDEX idx_tool_calls_name ON tool_calls USING btree (tool_name);
CREATE INDEX idx_transitions_job ON state_transitions USING btree (job_id, created_at);
CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_tasks_updated_at BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id);
ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id);
ALTER TABLE ONLY memory_links
    ADD CONSTRAINT memory_links_from_node_fkey FOREIGN KEY (from_node) REFERENCES memory_nodes(id) ON DELETE CASCADE;
ALTER TABLE ONLY messages
    ADD CONSTRAINT messages_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id);
ALTER TABLE ONLY subtasks
    ADD CONSTRAINT subtasks_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id);
ALTER TABLE ONLY tasks
    ADD CONSTRAINT tasks_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id);
