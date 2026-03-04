-- seed_fake_data.sql
-- Generates realistic fake data for the minions-suite dashboard.
-- Run with: psql $POSTGRES_URL -f seed_fake_data.sql

BEGIN;

-- ============================================================
-- Jobs — mix of review and development, various statuses
-- ============================================================

INSERT INTO minions.jobs (id, spec, status, job_type, mr_url, correlation_id, trello_card_id, error, created_at, updated_at) VALUES

-- Completed review jobs
('a1b2c3d4', 'Review MR for payments-api: Add Stripe webhook handler for subscription lifecycle events', 'done', 'review', 'https://gitlab.example.com/team/payments-api/-/merge_requests/142', 'corr-001', NULL, NULL,
 NOW() - INTERVAL '3 days 4 hours', NOW() - INTERVAL '3 days 3 hours 45 minutes'),

('b2c3d4e5', 'Review MR for auth-service: Implement OAuth2 PKCE flow for mobile clients', 'done', 'review', 'https://gitlab.example.com/team/auth-service/-/merge_requests/87', 'corr-002', NULL, NULL,
 NOW() - INTERVAL '2 days 6 hours', NOW() - INTERVAL '2 days 5 hours 30 minutes'),

('c3d4e5f6', 'Review MR for frontend: Refactor checkout form to use React Hook Form with Zod validation', 'done', 'review', 'https://gitlab.example.com/team/frontend/-/merge_requests/311', 'corr-003', NULL, NULL,
 NOW() - INTERVAL '1 day 8 hours', NOW() - INTERVAL '1 day 7 hours 20 minutes'),

-- In-progress review
('d4e5f6a7', 'Review MR for inventory-service: Add bulk import endpoint with CSV parsing and validation', 'review_in_progress', 'review', 'https://gitlab.example.com/team/inventory-service/-/merge_requests/56', 'corr-004', NULL, NULL,
 NOW() - INTERVAL '25 minutes', NOW() - INTERVAL '3 minutes'),

-- Failed review
('e5f6a7b8', 'Review MR for notifications: WebSocket push notification refactor', 'failed', 'review', 'https://gitlab.example.com/team/notifications/-/merge_requests/23', 'corr-005', NULL, 'LiteLLM rate limit exceeded after 3 retries',
 NOW() - INTERVAL '1 day 2 hours', NOW() - INTERVAL '1 day 1 hour 50 minutes'),

-- Development jobs
('f6a7b8c9', 'Implement rate limiting middleware for public API endpoints using sliding window algorithm with Redis backend', 'done', 'development', NULL, 'corr-006', 'card-abc123', NULL,
 NOW() - INTERVAL '5 days', NOW() - INTERVAL '4 days 16 hours'),

('a7b8c9d0', 'Add CSV export feature for transaction reports with date range filtering and column selection', 'dev_in_progress', 'development', NULL, 'corr-007', 'card-def456', NULL,
 NOW() - INTERVAL '2 hours', NOW() - INTERVAL '8 minutes'),

('b8c9d0e1', 'Set up database connection pooling with PgBouncer for multi-tenant isolation', 'tasks_created', 'development', NULL, 'corr-008', 'card-ghi789', NULL,
 NOW() - INTERVAL '45 minutes', NOW() - INTERVAL '40 minutes'),

-- Another completed dev job
('c9d0e1f2', 'Migrate user preferences from JSON column to normalized relational tables', 'done', 'development', NULL, 'corr-009', 'card-jkl012', NULL,
 NOW() - INTERVAL '7 days', NOW() - INTERVAL '6 days 18 hours'),

-- Failed dev job
('d0e1f2a3', 'Implement real-time dashboard metrics using Server-Sent Events', 'failed', 'development', NULL, 'corr-010', 'card-mno345', 'Agent exceeded max turns (30) without completing task',
 NOW() - INTERVAL '3 days 1 hour', NOW() - INTERVAL '3 days');


-- ============================================================
-- Tasks — each job has 1+ tasks
-- ============================================================

INSERT INTO minions.tasks (id, job_id, title, description, service, agent_role, status, branch_name, pr_number, pr_url, review_status, deploy_status, revision_count, timeout_seconds, attempt, max_attempts, error, mr_url, mr_id, verdict, comments_posted, created_at, updated_at) VALUES

-- Review tasks (one per review job)
('t1a1a1a1', 'a1b2c3d4', 'Review: Stripe webhook handler', 'Review the MR implementing Stripe webhook lifecycle event handling', 'payments-api', 'code_reviewer', 'done', NULL, NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 'https://gitlab.example.com/team/payments-api/-/merge_requests/142', '142', 'approve', 4,
 NOW() - INTERVAL '3 days 4 hours', NOW() - INTERVAL '3 days 3 hours 45 minutes'),

('t2b2b2b2', 'b2c3d4e5', 'Review: OAuth2 PKCE flow', 'Review the MR implementing OAuth2 PKCE for mobile', 'auth-service', 'code_reviewer', 'done', NULL, NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 'https://gitlab.example.com/team/auth-service/-/merge_requests/87', '87', 'request_changes', 7,
 NOW() - INTERVAL '2 days 6 hours', NOW() - INTERVAL '2 days 5 hours 30 minutes'),

('t3c3c3c3', 'c3d4e5f6', 'Review: Checkout form refactor', 'Review React Hook Form + Zod migration', 'frontend', 'code_reviewer', 'done', NULL, NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 'https://gitlab.example.com/team/frontend/-/merge_requests/311', '311', 'approve', 2,
 NOW() - INTERVAL '1 day 8 hours', NOW() - INTERVAL '1 day 7 hours 20 minutes'),

('t4d4d4d4', 'd4e5f6a7', 'Review: Bulk import endpoint', 'Review CSV bulk import with validation', 'inventory-service', 'code_reviewer', 'in_progress', NULL, NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 'https://gitlab.example.com/team/inventory-service/-/merge_requests/56', '56', NULL, 0,
 NOW() - INTERVAL '25 minutes', NOW() - INTERVAL '3 minutes'),

('t5e5e5e5', 'e5f6a7b8', 'Review: WebSocket push notifications', 'Review notification refactor', 'notifications', 'code_reviewer', 'failed', NULL, NULL, NULL, NULL, NULL, 0, 600, 2, 3, 'Rate limit exceeded',
 'https://gitlab.example.com/team/notifications/-/merge_requests/23', '23', NULL, 0,
 NOW() - INTERVAL '1 day 2 hours', NOW() - INTERVAL '1 day 1 hour 50 minutes'),

-- Development tasks (multiple per dev job)
-- Rate limiting job (done) — 2 tasks
('t6f6f6f6', 'f6a7b8c9', 'Implement sliding window rate limiter', 'Core rate limiting logic with Redis backend', 'management-api', 'backend_engineer', 'done', 'feat/rate-limiter', 234, 'https://gitlab.example.com/team/management-api/-/merge_requests/234', 'approved', 'success', 1, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '5 days', NOW() - INTERVAL '4 days 20 hours'),

('t7a7a7a7', 'f6a7b8c9', 'Add rate limit configuration API', 'REST endpoints for managing rate limit rules per tenant', 'management-api', 'backend_engineer', 'done', 'feat/rate-limiter', 234, 'https://gitlab.example.com/team/management-api/-/merge_requests/234', 'approved', 'success', 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '4 days 22 hours', NOW() - INTERVAL '4 days 18 hours'),

-- CSV export job (in progress) — 2 tasks, one done, one in progress
('t8b8b8b8', 'a7b8c9d0', 'Build CSV generation engine', 'Streaming CSV writer with column selection support', 'reporting-service', 'backend_engineer', 'done', 'feat/csv-export', NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour 15 minutes'),

('t9c9c9c9', 'a7b8c9d0', 'Add REST endpoint for CSV export', 'GET /api/v1/reports/export with date range and format params', 'reporting-service', 'backend_engineer', 'in_progress', 'feat/csv-export', NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '1 hour 10 minutes', NOW() - INTERVAL '8 minutes'),

-- PgBouncer job (tasks_created) — 2 pending tasks
('tad0d0d0', 'b8c9d0e1', 'Configure PgBouncer connection pooling', 'Set up PgBouncer with per-tenant pool sizing', 'infrastructure', 'database_engineer', 'pending', NULL, NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '45 minutes', NOW() - INTERVAL '45 minutes'),

('tbe1e1e1', 'b8c9d0e1', 'Update application DB layer for pooling', 'Modify connection handling to work with PgBouncer transaction mode', 'management-api', 'backend_engineer', 'pending', NULL, NULL, NULL, NULL, NULL, 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '45 minutes', NOW() - INTERVAL '45 minutes'),

-- User prefs migration (done) — 3 tasks
('tcf2f2f2', 'c9d0e1f2', 'Create normalized preference tables', 'Design and create relational tables for user preferences', 'management-api', 'backend_engineer', 'done', 'feat/pref-migration', 198, 'https://gitlab.example.com/team/management-api/-/merge_requests/198', 'approved', 'success', 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '7 days', NOW() - INTERVAL '6 days 22 hours'),

('tda3a3a3', 'c9d0e1f2', 'Write data migration script', 'Migrate existing JSON preferences to new tables', 'management-api', 'backend_engineer', 'done', 'feat/pref-migration', 198, 'https://gitlab.example.com/team/management-api/-/merge_requests/198', 'approved', 'success', 1, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '6 days 22 hours', NOW() - INTERVAL '6 days 20 hours'),

('teb4b4b4', 'c9d0e1f2', 'Update preference API endpoints', 'Refactor REST API to use normalized tables', 'management-api', 'backend_engineer', 'done', 'feat/pref-migration', 198, 'https://gitlab.example.com/team/management-api/-/merge_requests/198', 'approved', 'success', 0, 600, 1, 3, NULL,
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '6 days 20 hours', NOW() - INTERVAL '6 days 18 hours'),

-- SSE dashboard (failed) — 1 task failed
('tfc5c5c5', 'd0e1f2a3', 'Implement SSE metrics stream', 'Server-Sent Events endpoint for real-time dashboard data', 'management-api', 'backend_engineer', 'failed', 'feat/sse-dashboard', NULL, NULL, NULL, NULL, 0, 600, 3, 3, 'Max turns exceeded without completing implementation',
 NULL, NULL, NULL, 0,
 NOW() - INTERVAL '3 days 1 hour', NOW() - INTERVAL '3 days');


-- ============================================================
-- Agents — one per task execution
-- ============================================================

INSERT INTO minions.agents (id, job_id, role, task_id, pid, status, host, started_at, finished_at, error, log_file, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, num_turns, model, k8s_job_name) VALUES

-- Review agents
('ag01aa01', 'a1b2c3d4', 'code_reviewer', 't1a1a1a1', 48201, 'completed', 'minion-worker-1', NOW() - INTERVAL '3 days 3 hours 58 minutes', NOW() - INTERVAL '3 days 3 hours 45 minutes', NULL,
 'logs/agents/a1b2c3d4_code_reviewer_001.log', 45200, 8300, 12000, 3200, 0.1842, 12, 'claude-sonnet-4-20250514', 'review-a1b2c3d4'),

('ag02bb02', 'b2c3d4e5', 'code_reviewer', 't2b2b2b2', 48302, 'completed', 'minion-worker-1', NOW() - INTERVAL '2 days 5 hours 50 minutes', NOW() - INTERVAL '2 days 5 hours 30 minutes', NULL,
 'logs/agents/b2c3d4e5_code_reviewer_001.log', 62400, 14200, 18000, 4100, 0.2736, 18, 'claude-sonnet-4-20250514', 'review-b2c3d4e5'),

('ag03cc03', 'c3d4e5f6', 'code_reviewer', 't3c3c3c3', 48403, 'completed', 'minion-worker-2', NOW() - INTERVAL '1 day 7 hours 35 minutes', NOW() - INTERVAL '1 day 7 hours 20 minutes', NULL,
 'logs/agents/c3d4e5f6_code_reviewer_001.log', 31800, 5900, 9500, 2800, 0.1204, 8, 'claude-sonnet-4-20250514', 'review-c3d4e5f6'),

('ag04dd04', 'd4e5f6a7', 'code_reviewer', 't4d4d4d4', 49112, 'running', 'minion-worker-1', NOW() - INTERVAL '22 minutes', NULL, NULL,
 'logs/agents/d4e5f6a7_code_reviewer_001.log', 28900, 4100, 8200, 2100, 0.0983, 6, 'claude-sonnet-4-20250514', 'review-d4e5f6a7'),

('ag05ee05', 'e5f6a7b8', 'code_reviewer', 't5e5e5e5', 47890, 'failed', 'minion-worker-2', NOW() - INTERVAL '1 day 2 hours', NOW() - INTERVAL '1 day 1 hour 50 minutes', 'LiteLLM rate limit exceeded after 3 retries',
 'logs/agents/e5f6a7b8_code_reviewer_001.log', 15200, 2100, 4800, 1200, 0.0521, 4, 'claude-sonnet-4-20250514', 'review-e5f6a7b8'),

-- Second attempt on the failed review
('ag05ee06', 'e5f6a7b8', 'code_reviewer', 't5e5e5e5', 47920, 'failed', 'minion-worker-2', NOW() - INTERVAL '1 day 1 hour 45 minutes', NOW() - INTERVAL '1 day 1 hour 40 minutes', 'LiteLLM rate limit exceeded',
 'logs/agents/e5f6a7b8_code_reviewer_002.log', 8400, 900, 2100, 600, 0.0274, 2, 'claude-sonnet-4-20250514', 'review-e5f6a7b8-r2'),

-- Dev agents — rate limiter (done)
('ag06ff06', 'f6a7b8c9', 'backend_engineer', 't6f6f6f6', 46100, 'completed', 'minion-worker-1', NOW() - INTERVAL '4 days 23 hours', NOW() - INTERVAL '4 days 20 hours', NULL,
 'logs/agents/f6a7b8c9_backend_engineer_001.log', 124000, 38500, 42000, 12000, 0.6820, 26, 'claude-sonnet-4-20250514', 'dev-f6a7b8c9-t1'),

('ag07aa07', 'f6a7b8c9', 'backend_engineer', 't7a7a7a7', 46200, 'completed', 'minion-worker-2', NOW() - INTERVAL '4 days 19 hours', NOW() - INTERVAL '4 days 18 hours', NULL,
 'logs/agents/f6a7b8c9_backend_engineer_002.log', 89000, 24200, 31000, 8500, 0.4510, 19, 'claude-sonnet-4-20250514', 'dev-f6a7b8c9-t2'),

-- Dev agents — CSV export (in progress)
('ag08bb08', 'a7b8c9d0', 'backend_engineer', 't8b8b8b8', 49300, 'completed', 'minion-worker-1', NOW() - INTERVAL '1 hour 50 minutes', NOW() - INTERVAL '1 hour 15 minutes', NULL,
 'logs/agents/a7b8c9d0_backend_engineer_001.log', 72000, 19800, 24000, 7200, 0.3540, 16, 'claude-sonnet-4-20250514', 'dev-a7b8c9d0-t1'),

('ag09cc09', 'a7b8c9d0', 'backend_engineer', 't9c9c9c9', 49450, 'running', 'minion-worker-2', NOW() - INTERVAL '45 minutes', NULL, NULL,
 'logs/agents/a7b8c9d0_backend_engineer_002.log', 41200, 11500, 14000, 4100, 0.1980, 10, 'claude-sonnet-4-20250514', 'dev-a7b8c9d0-t2'),

-- Dev agents — user prefs migration (done)
('ag10dd10', 'c9d0e1f2', 'backend_engineer', 'tcf2f2f2', 44100, 'completed', 'minion-worker-1', NOW() - INTERVAL '6 days 23 hours', NOW() - INTERVAL '6 days 22 hours', NULL,
 'logs/agents/c9d0e1f2_backend_engineer_001.log', 68000, 21000, 22000, 6800, 0.3250, 14, 'claude-sonnet-4-20250514', 'dev-c9d0e1f2-t1'),

('ag11ee11', 'c9d0e1f2', 'backend_engineer', 'tda3a3a3', 44200, 'completed', 'minion-worker-2', NOW() - INTERVAL '6 days 21 hours', NOW() - INTERVAL '6 days 20 hours', NULL,
 'logs/agents/c9d0e1f2_backend_engineer_002.log', 52000, 15800, 17000, 5200, 0.2480, 11, 'claude-sonnet-4-20250514', 'dev-c9d0e1f2-t2'),

('ag12ff12', 'c9d0e1f2', 'backend_engineer', 'teb4b4b4', 44300, 'completed', 'minion-worker-1', NOW() - INTERVAL '6 days 19 hours 30 minutes', NOW() - INTERVAL '6 days 18 hours', NULL,
 'logs/agents/c9d0e1f2_backend_engineer_003.log', 47000, 13200, 15000, 4600, 0.2140, 9, 'claude-sonnet-4-20250514', 'dev-c9d0e1f2-t3'),

-- Dev agent — SSE dashboard (failed, 3 attempts)
('ag13aa13', 'd0e1f2a3', 'backend_engineer', 'tfc5c5c5', 45100, 'failed', 'minion-worker-1', NOW() - INTERVAL '3 days 1 hour', NOW() - INTERVAL '3 days 30 minutes', 'Max turns exceeded (30)',
 'logs/agents/d0e1f2a3_backend_engineer_001.log', 135000, 42000, 48000, 14000, 0.7120, 30, 'claude-sonnet-4-20250514', 'dev-d0e1f2a3-t1'),

('ag13aa14', 'd0e1f2a3', 'backend_engineer', 'tfc5c5c5', 45200, 'failed', 'minion-worker-2', NOW() - INTERVAL '3 days 25 minutes', NOW() - INTERVAL '3 days 5 minutes', 'Max turns exceeded (30)',
 'logs/agents/d0e1f2a3_backend_engineer_002.log', 128000, 39000, 44000, 13000, 0.6780, 30, 'claude-sonnet-4-20250514', 'dev-d0e1f2a3-t1r2'),

('ag13aa15', 'd0e1f2a3', 'backend_engineer', 'tfc5c5c5', 45300, 'failed', 'minion-worker-1', NOW() - INTERVAL '3 days 2 minutes', NOW() - INTERVAL '3 days', 'Max turns exceeded (30)',
 'logs/agents/d0e1f2a3_backend_engineer_003.log', 131000, 40500, 45000, 13500, 0.6940, 30, 'claude-sonnet-4-20250514', 'dev-d0e1f2a3-t1r3');


-- ============================================================
-- Events — timeline entries for each job
-- ============================================================

INSERT INTO minions.events (job_id, event_type, source, detail, created_at) VALUES

-- Review job a1b2c3d4
('a1b2c3d4', 'job_created', 'cli', 'Review requested for payments-api MR !142', NOW() - INTERVAL '3 days 4 hours'),
('a1b2c3d4', 'task_created', 'job_engine', 'code_reviewer task created for payments-api', NOW() - INTERVAL '3 days 3 hours 59 minutes'),
('a1b2c3d4', 'agent_launched', 'job_engine', 'Agent ag01aa01 launched for code_reviewer', NOW() - INTERVAL '3 days 3 hours 58 minutes'),
('a1b2c3d4', 'review_started', 'agent', 'Fetching MR diff (14 files changed)', NOW() - INTERVAL '3 days 3 hours 57 minutes'),
('a1b2c3d4', 'comment_posted', 'agent', 'Inline comment on src/webhooks/stripe.py:42', NOW() - INTERVAL '3 days 3 hours 52 minutes'),
('a1b2c3d4', 'comment_posted', 'agent', 'Inline comment on src/webhooks/stripe.py:87', NOW() - INTERVAL '3 days 3 hours 50 minutes'),
('a1b2c3d4', 'comment_posted', 'agent', 'Inline comment on src/models/subscription.py:23', NOW() - INTERVAL '3 days 3 hours 48 minutes'),
('a1b2c3d4', 'comment_posted', 'agent', 'Inline comment on tests/test_webhooks.py:15', NOW() - INTERVAL '3 days 3 hours 47 minutes'),
('a1b2c3d4', 'review_submitted', 'agent', 'Verdict: APPROVE — clean implementation with minor suggestions', NOW() - INTERVAL '3 days 3 hours 45 minutes'),
('a1b2c3d4', 'job_completed', 'job_engine', 'Review job completed successfully', NOW() - INTERVAL '3 days 3 hours 45 minutes'),

-- Review job b2c3d4e5
('b2c3d4e5', 'job_created', 'cli', 'Review requested for auth-service MR !87', NOW() - INTERVAL '2 days 6 hours'),
('b2c3d4e5', 'task_created', 'job_engine', 'code_reviewer task created for auth-service', NOW() - INTERVAL '2 days 5 hours 55 minutes'),
('b2c3d4e5', 'agent_launched', 'job_engine', 'Agent ag02bb02 launched for code_reviewer', NOW() - INTERVAL '2 days 5 hours 50 minutes'),
('b2c3d4e5', 'review_started', 'agent', 'Fetching MR diff (22 files changed)', NOW() - INTERVAL '2 days 5 hours 49 minutes'),
('b2c3d4e5', 'comment_posted', 'agent', 'Inline comment on src/oauth/pkce.py:18 — missing state parameter validation', NOW() - INTERVAL '2 days 5 hours 42 minutes'),
('b2c3d4e5', 'comment_posted', 'agent', 'Inline comment on src/oauth/pkce.py:55 — code_verifier entropy too low', NOW() - INTERVAL '2 days 5 hours 40 minutes'),
('b2c3d4e5', 'comment_posted', 'agent', 'Inline comment on src/oauth/token.py:31 — token expiry should be configurable', NOW() - INTERVAL '2 days 5 hours 38 minutes'),
('b2c3d4e5', 'review_submitted', 'agent', 'Verdict: REQUEST_CHANGES — security concerns with PKCE implementation', NOW() - INTERVAL '2 days 5 hours 30 minutes'),
('b2c3d4e5', 'job_completed', 'job_engine', 'Review job completed successfully', NOW() - INTERVAL '2 days 5 hours 30 minutes'),

-- In-progress review d4e5f6a7
('d4e5f6a7', 'job_created', 'webhook', 'Review requested for inventory-service MR !56', NOW() - INTERVAL '25 minutes'),
('d4e5f6a7', 'task_created', 'job_engine', 'code_reviewer task created for inventory-service', NOW() - INTERVAL '24 minutes'),
('d4e5f6a7', 'agent_launched', 'job_engine', 'Agent ag04dd04 launched for code_reviewer', NOW() - INTERVAL '22 minutes'),
('d4e5f6a7', 'review_started', 'agent', 'Fetching MR diff (8 files changed)', NOW() - INTERVAL '21 minutes'),

-- Failed review e5f6a7b8
('e5f6a7b8', 'job_created', 'cli', 'Review requested for notifications MR !23', NOW() - INTERVAL '1 day 2 hours'),
('e5f6a7b8', 'agent_launched', 'job_engine', 'Agent ag05ee05 launched for code_reviewer', NOW() - INTERVAL '1 day 2 hours'),
('e5f6a7b8', 'agent_failed', 'job_engine', 'Agent ag05ee05 failed: rate limit exceeded', NOW() - INTERVAL '1 day 1 hour 50 minutes'),
('e5f6a7b8', 'agent_launched', 'job_engine', 'Retry attempt 2: Agent ag05ee06 launched', NOW() - INTERVAL '1 day 1 hour 45 minutes'),
('e5f6a7b8', 'agent_failed', 'job_engine', 'Agent ag05ee06 failed: rate limit exceeded', NOW() - INTERVAL '1 day 1 hour 40 minutes'),
('e5f6a7b8', 'job_failed', 'job_engine', 'Job failed after 2 agent attempts', NOW() - INTERVAL '1 day 1 hour 40 minutes'),

-- Dev job f6a7b8c9 (done)
('f6a7b8c9', 'job_created', 'trello', 'Development job created from Trello card', NOW() - INTERVAL '5 days'),
('f6a7b8c9', 'spec_refined', 'orchestrator', 'Spec decomposed into 2 tasks', NOW() - INTERVAL '4 days 23 hours 30 minutes'),
('f6a7b8c9', 'agent_launched', 'job_engine', 'backend_engineer agent launched for rate limiter core', NOW() - INTERVAL '4 days 23 hours'),
('f6a7b8c9', 'pr_opened', 'agent', 'PR !234 opened on management-api', NOW() - INTERVAL '4 days 20 hours 30 minutes'),
('f6a7b8c9', 'agent_launched', 'job_engine', 'backend_engineer agent launched for config API', NOW() - INTERVAL '4 days 19 hours'),
('f6a7b8c9', 'review_approved', 'gitlab', 'PR !234 approved by @sarah', NOW() - INTERVAL '4 days 17 hours'),
('f6a7b8c9', 'pr_merged', 'gitlab', 'PR !234 merged to main', NOW() - INTERVAL '4 days 16 hours 30 minutes'),
('f6a7b8c9', 'deploy_success', 'k8s', 'Deployed to staging successfully', NOW() - INTERVAL '4 days 16 hours'),
('f6a7b8c9', 'job_completed', 'job_engine', 'All tasks completed and deployed', NOW() - INTERVAL '4 days 16 hours'),

-- Dev job a7b8c9d0 (in progress)
('a7b8c9d0', 'job_created', 'trello', 'Development job created from Trello card', NOW() - INTERVAL '2 hours'),
('a7b8c9d0', 'spec_refined', 'orchestrator', 'Spec decomposed into 2 tasks', NOW() - INTERVAL '1 hour 55 minutes'),
('a7b8c9d0', 'agent_launched', 'job_engine', 'backend_engineer agent launched for CSV engine', NOW() - INTERVAL '1 hour 50 minutes'),
('a7b8c9d0', 'task_completed', 'agent', 'CSV generation engine implemented', NOW() - INTERVAL '1 hour 15 minutes'),
('a7b8c9d0', 'agent_launched', 'job_engine', 'backend_engineer agent launched for REST endpoint', NOW() - INTERVAL '45 minutes');


-- ============================================================
-- Tool calls — agent MCP tool invocations
-- ============================================================

INSERT INTO minions.tool_calls (job_id, tool_name, params, result, error, duration_ms, created_at) VALUES

-- Review a1b2c3d4 tool calls
('a1b2c3d4', 'get_mr_diff', '{"mr_url": "https://gitlab.example.com/team/payments-api/-/merge_requests/142"}', 'Diff retrieved: 14 files, +482/-127 lines', NULL, 1240, NOW() - INTERVAL '3 days 3 hours 57 minutes'),
('a1b2c3d4', 'get_changed_files', '{"mr_url": "https://gitlab.example.com/team/payments-api/-/merge_requests/142"}', '["src/webhooks/stripe.py","src/models/subscription.py","src/handlers/billing.py","tests/test_webhooks.py"]', NULL, 890, NOW() - INTERVAL '3 days 3 hours 56 minutes'),
('a1b2c3d4', 'read_file', '{"path": "src/webhooks/stripe.py"}', 'File contents (142 lines)', NULL, 320, NOW() - INTERVAL '3 days 3 hours 55 minutes'),
('a1b2c3d4', 'read_file', '{"path": "src/models/subscription.py"}', 'File contents (89 lines)', NULL, 280, NOW() - INTERVAL '3 days 3 hours 54 minutes'),
('a1b2c3d4', 'search_code', '{"pattern": "webhook.*handler", "path": "src/"}', '12 matches found', NULL, 450, NOW() - INTERVAL '3 days 3 hours 53 minutes'),
('a1b2c3d4', 'post_inline_comment', '{"file": "src/webhooks/stripe.py", "line": 42, "body": "Consider adding idempotency key check"}', 'Comment posted', NULL, 680, NOW() - INTERVAL '3 days 3 hours 52 minutes'),
('a1b2c3d4', 'post_inline_comment', '{"file": "src/webhooks/stripe.py", "line": 87, "body": "Missing error handling for network timeouts"}', 'Comment posted', NULL, 720, NOW() - INTERVAL '3 days 3 hours 50 minutes'),
('a1b2c3d4', 'post_inline_comment', '{"file": "src/models/subscription.py", "line": 23, "body": "Enum should include PAST_DUE status"}', 'Comment posted', NULL, 690, NOW() - INTERVAL '3 days 3 hours 48 minutes'),
('a1b2c3d4', 'post_inline_comment', '{"file": "tests/test_webhooks.py", "line": 15, "body": "Add test case for duplicate webhook delivery"}', 'Comment posted', NULL, 710, NOW() - INTERVAL '3 days 3 hours 47 minutes'),
('a1b2c3d4', 'submit_review', '{"verdict": "approve", "summary": "Clean implementation with minor suggestions"}', 'Review submitted', NULL, 920, NOW() - INTERVAL '3 days 3 hours 45 minutes'),

-- In-progress review d4e5f6a7 tool calls
('d4e5f6a7', 'get_mr_diff', '{"mr_url": "https://gitlab.example.com/team/inventory-service/-/merge_requests/56"}', 'Diff retrieved: 8 files, +312/-45 lines', NULL, 1180, NOW() - INTERVAL '21 minutes'),
('d4e5f6a7', 'get_changed_files', '{"mr_url": "https://gitlab.example.com/team/inventory-service/-/merge_requests/56"}', '["src/api/bulk_import.py","src/parsers/csv_parser.py","src/validators/import_validator.py"]', NULL, 780, NOW() - INTERVAL '20 minutes'),
('d4e5f6a7', 'read_file', '{"path": "src/api/bulk_import.py"}', 'File contents (201 lines)', NULL, 310, NOW() - INTERVAL '19 minutes'),
('d4e5f6a7', 'read_file', '{"path": "src/parsers/csv_parser.py"}', 'File contents (156 lines)', NULL, 290, NOW() - INTERVAL '18 minutes'),
('d4e5f6a7', 'search_code', '{"pattern": "import.*csv", "path": "src/"}', '8 matches found', NULL, 410, NOW() - INTERVAL '15 minutes'),
('d4e5f6a7', 'read_file', '{"path": "src/validators/import_validator.py"}', 'File contents (98 lines)', NULL, 270, NOW() - INTERVAL '12 minutes'),

-- Dev job a7b8c9d0 tool calls (in progress)
('a7b8c9d0', 'read_file', '{"path": "src/reporting/models.py"}', 'File contents (67 lines)', NULL, 290, NOW() - INTERVAL '1 hour 48 minutes'),
('a7b8c9d0', 'search_code', '{"pattern": "class.*Report", "path": "src/"}', '4 matches found', NULL, 380, NOW() - INTERVAL '1 hour 45 minutes'),
('a7b8c9d0', 'list_files', '{"pattern": "src/reporting/**/*.py"}', '["models.py","service.py","routes.py","__init__.py"]', NULL, 210, NOW() - INTERVAL '1 hour 40 minutes');


-- ============================================================
-- Messages — inter-agent communication
-- ============================================================

INSERT INTO minions.messages (id, job_id, from_role, to_role, content, created_at) VALUES

-- Dev job f6a7b8c9 messages
('msg00001', 'f6a7b8c9', 'arbiter', NULL, 'Job f6a7b8c9 started: Implement rate limiting middleware. Decomposed into 2 tasks: (1) core rate limiter, (2) configuration API.', NOW() - INTERVAL '4 days 23 hours 30 minutes'),
('msg00002', 'f6a7b8c9', 'backend_engineer', 'arbiter', 'Task t6f6f6f6 complete: Sliding window rate limiter implemented with Redis backend. Tests passing. PR !234 opened.', NOW() - INTERVAL '4 days 20 hours 30 minutes'),
('msg00003', 'f6a7b8c9', 'backend_engineer', 'arbiter', 'Task t7a7a7a7 complete: Rate limit configuration endpoints added. Integrated with existing admin API.', NOW() - INTERVAL '4 days 18 hours'),
('msg00004', 'f6a7b8c9', 'arbiter', NULL, 'All tasks completed. PR !234 approved and merged. Deployment successful.', NOW() - INTERVAL '4 days 16 hours'),

-- Dev job a7b8c9d0 messages (in progress)
('msg00005', 'a7b8c9d0', 'arbiter', NULL, 'Job a7b8c9d0 started: CSV export feature. Decomposed into 2 tasks: (1) CSV generation engine, (2) REST endpoint.', NOW() - INTERVAL '1 hour 55 minutes'),
('msg00006', 'a7b8c9d0', 'backend_engineer', 'arbiter', 'Task t8b8b8b8 complete: Streaming CSV writer implemented with column selection support. Handles 100k+ rows efficiently.', NOW() - INTERVAL '1 hour 15 minutes'),
('msg00007', 'a7b8c9d0', 'arbiter', 'backend_engineer', 'Starting task t9c9c9c9: REST endpoint for CSV export. Use the streaming writer from task 1.', NOW() - INTERVAL '45 minutes'),

-- Failed job messages
('msg00008', 'd0e1f2a3', 'arbiter', NULL, 'Job d0e1f2a3 started: SSE metrics dashboard. Single task: implement SSE endpoint.', NOW() - INTERVAL '3 days 1 hour'),
('msg00009', 'd0e1f2a3', 'backend_engineer', 'arbiter', 'Task tfc5c5c5 failed: exceeded max turns attempting to implement SSE with proper backpressure handling. Need architectural guidance.', NOW() - INTERVAL '3 days');


-- ============================================================
-- Subtasks — granular breakdown for select tasks
-- ============================================================

INSERT INTO minions.subtasks (id, task_id, sequence_num, description, status, timeout_seconds, started_at, completed_at, result, error, attempt, created_at) VALUES

-- Subtasks for rate limiter task t6f6f6f6
('st010101', 't6f6f6f6', 1, 'Implement sliding window counter in Redis', 'completed', 120, NOW() - INTERVAL '4 days 22 hours 50 minutes', NOW() - INTERVAL '4 days 22 hours 20 minutes', '{"files_changed": 2, "lines_added": 145}', NULL, 1, NOW() - INTERVAL '4 days 23 hours'),
('st020202', 't6f6f6f6', 2, 'Create rate limit middleware decorator', 'completed', 120, NOW() - INTERVAL '4 days 22 hours 15 minutes', NOW() - INTERVAL '4 days 21 hours 45 minutes', '{"files_changed": 1, "lines_added": 67}', NULL, 1, NOW() - INTERVAL '4 days 23 hours'),
('st030303', 't6f6f6f6', 3, 'Add rate limit response headers', 'completed', 120, NOW() - INTERVAL '4 days 21 hours 40 minutes', NOW() - INTERVAL '4 days 21 hours 20 minutes', '{"files_changed": 1, "lines_added": 28}', NULL, 1, NOW() - INTERVAL '4 days 23 hours'),
('st040404', 't6f6f6f6', 4, 'Write unit and integration tests', 'completed', 120, NOW() - INTERVAL '4 days 21 hours 15 minutes', NOW() - INTERVAL '4 days 20 hours 30 minutes', '{"files_changed": 2, "lines_added": 210, "tests": 12}', NULL, 1, NOW() - INTERVAL '4 days 23 hours'),

-- Subtasks for in-progress CSV endpoint task t9c9c9c9
('st050505', 't9c9c9c9', 1, 'Define API schema and query parameters', 'completed', 120, NOW() - INTERVAL '40 minutes', NOW() - INTERVAL '32 minutes', '{"files_changed": 1, "lines_added": 45}', NULL, 1, NOW() - INTERVAL '1 hour 10 minutes'),
('st060606', 't9c9c9c9', 2, 'Implement streaming response handler', 'completed', 120, NOW() - INTERVAL '30 minutes', NOW() - INTERVAL '18 minutes', '{"files_changed": 1, "lines_added": 78}', NULL, 1, NOW() - INTERVAL '1 hour 10 minutes'),
('st070707', 't9c9c9c9', 3, 'Add date range filtering and validation', 'in_progress', 120, NOW() - INTERVAL '15 minutes', NULL, NULL, NULL, 1, NOW() - INTERVAL '1 hour 10 minutes'),
('st080808', 't9c9c9c9', 4, 'Write endpoint tests', 'pending', 120, NULL, NULL, NULL, NULL, 1, NOW() - INTERVAL '1 hour 10 minutes'),

-- Subtasks for failed SSE task tfc5c5c5
('st090909', 'tfc5c5c5', 1, 'Design SSE event schema', 'completed', 120, NOW() - INTERVAL '3 days 55 minutes', NOW() - INTERVAL '3 days 45 minutes', '{"files_changed": 1}', NULL, 1, NOW() - INTERVAL '3 days 1 hour'),
('st101010', 'tfc5c5c5', 2, 'Implement SSE endpoint with backpressure', 'failed', 120, NOW() - INTERVAL '3 days 40 minutes', NOW() - INTERVAL '3 days', NULL, 'Could not resolve async generator cleanup on client disconnect', 3, NOW() - INTERVAL '3 days 1 hour'),
('st111111', 'tfc5c5c5', 3, 'Add metrics aggregation pipeline', 'pending', 120, NULL, NULL, NULL, NULL, 1, NOW() - INTERVAL '3 days 1 hour');


-- ============================================================
-- Heartbeats — current agent liveness
-- ============================================================

INSERT INTO minions.heartbeats (agent_id, agent_role, job_id, current_task_id, current_subtask_id, last_seen, status) VALUES
('ag04dd04', 'code_reviewer', 'd4e5f6a7', 't4d4d4d4', NULL, NOW() - INTERVAL '30 seconds', 'running'),
('ag09cc09', 'backend_engineer', 'a7b8c9d0', 't9c9c9c9', 'st070707', NOW() - INTERVAL '15 seconds', 'running');


-- ============================================================
-- State transitions — audit log samples
-- ============================================================

INSERT INTO minions.state_transitions (job_id, task_id, subtask_id, agent_id, from_status, to_status, approved, rejection_reason, created_at) VALUES

-- Review job a1b2c3d4
('a1b2c3d4', NULL, NULL, NULL, 'spec_received', 'tasks_created', TRUE, NULL, NOW() - INTERVAL '3 days 3 hours 59 minutes'),
('a1b2c3d4', 't1a1a1a1', NULL, 'ag01aa01', 'pending', 'in_progress', TRUE, NULL, NOW() - INTERVAL '3 days 3 hours 58 minutes'),
('a1b2c3d4', 't1a1a1a1', NULL, 'ag01aa01', 'in_progress', 'done', TRUE, NULL, NOW() - INTERVAL '3 days 3 hours 45 minutes'),
('a1b2c3d4', NULL, NULL, NULL, 'review_in_progress', 'done', TRUE, NULL, NOW() - INTERVAL '3 days 3 hours 45 minutes'),

-- Dev job f6a7b8c9
('f6a7b8c9', NULL, NULL, NULL, 'spec_received', 'spec_ready', TRUE, NULL, NOW() - INTERVAL '4 days 23 hours 30 minutes'),
('f6a7b8c9', NULL, NULL, NULL, 'spec_ready', 'tasks_created', TRUE, NULL, NOW() - INTERVAL '4 days 23 hours'),
('f6a7b8c9', 't6f6f6f6', NULL, 'ag06ff06', 'pending', 'in_progress', TRUE, NULL, NOW() - INTERVAL '4 days 23 hours'),
('f6a7b8c9', 't6f6f6f6', NULL, 'ag06ff06', 'in_progress', 'done', TRUE, NULL, NOW() - INTERVAL '4 days 20 hours'),
('f6a7b8c9', NULL, NULL, NULL, 'tasks_created', 'dev_in_progress', TRUE, NULL, NOW() - INTERVAL '4 days 23 hours'),
('f6a7b8c9', NULL, NULL, NULL, 'dev_in_progress', 'pr_open', TRUE, NULL, NOW() - INTERVAL '4 days 20 hours 30 minutes'),
('f6a7b8c9', NULL, NULL, NULL, 'pr_open', 'done', TRUE, NULL, NOW() - INTERVAL '4 days 16 hours'),

-- In-progress transitions
('a7b8c9d0', NULL, NULL, NULL, 'spec_received', 'tasks_created', TRUE, NULL, NOW() - INTERVAL '1 hour 55 minutes'),
('a7b8c9d0', NULL, NULL, NULL, 'tasks_created', 'dev_in_progress', TRUE, NULL, NOW() - INTERVAL '1 hour 50 minutes'),
('a7b8c9d0', 't8b8b8b8', NULL, 'ag08bb08', 'pending', 'in_progress', TRUE, NULL, NOW() - INTERVAL '1 hour 50 minutes'),
('a7b8c9d0', 't8b8b8b8', NULL, 'ag08bb08', 'in_progress', 'done', TRUE, NULL, NOW() - INTERVAL '1 hour 15 minutes'),
('a7b8c9d0', 't9c9c9c9', NULL, 'ag09cc09', 'pending', 'in_progress', TRUE, NULL, NOW() - INTERVAL '45 minutes');


-- ============================================================
-- NATS message log — sample archived messages
-- ============================================================

INSERT INTO minions.message_log (nats_subject, nats_sequence, job_id, from_role, to_role, message_type, payload, correlation_id, created_at) VALUES

('jobs.review.requested.payments-api', 1001, 'a1b2c3d4', NULL, NULL, 'review_requested', '{"mr_url": "https://gitlab.example.com/team/payments-api/-/merge_requests/142", "project": "payments-api"}', 'corr-001', NOW() - INTERVAL '3 days 4 hours'),
('jobs.review.started.payments-api', 1002, 'a1b2c3d4', 'code_reviewer', NULL, 'review_started', '{"agent_id": "ag01aa01"}', 'corr-001', NOW() - INTERVAL '3 days 3 hours 58 minutes'),
('jobs.review.completed.payments-api', 1003, 'a1b2c3d4', 'code_reviewer', NULL, 'review_completed', '{"verdict": "approve", "comments": 4}', 'corr-001', NOW() - INTERVAL '3 days 3 hours 45 minutes'),

('jobs.review.requested.auth-service', 1004, 'b2c3d4e5', NULL, NULL, 'review_requested', '{"mr_url": "https://gitlab.example.com/team/auth-service/-/merge_requests/87", "project": "auth-service"}', 'corr-002', NOW() - INTERVAL '2 days 6 hours'),
('jobs.review.completed.auth-service', 1005, 'b2c3d4e5', 'code_reviewer', NULL, 'review_completed', '{"verdict": "request_changes", "comments": 7}', 'corr-002', NOW() - INTERVAL '2 days 5 hours 30 minutes'),

('jobs.review.requested.inventory-service', 1006, 'd4e5f6a7', NULL, NULL, 'review_requested', '{"mr_url": "https://gitlab.example.com/team/inventory-service/-/merge_requests/56", "project": "inventory-service"}', 'corr-004', NOW() - INTERVAL '25 minutes'),
('jobs.review.started.inventory-service', 1007, 'd4e5f6a7', 'code_reviewer', NULL, 'review_started', '{"agent_id": "ag04dd04"}', 'corr-004', NOW() - INTERVAL '22 minutes'),

('jobs.review.failed.notifications', 1008, 'e5f6a7b8', 'code_reviewer', NULL, 'review_failed', '{"error": "Rate limit exceeded", "attempts": 2}', 'corr-005', NOW() - INTERVAL '1 day 1 hour 40 minutes'),

('jobs.a7b8c9d0.status', 2001, 'a7b8c9d0', 'arbiter', NULL, 'status_update', '{"status": "dev_in_progress", "tasks_total": 2, "tasks_done": 1}', 'corr-007', NOW() - INTERVAL '1 hour 15 minutes');

COMMIT;
