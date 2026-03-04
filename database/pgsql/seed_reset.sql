-- seed_reset.sql
-- Removes all seed data from minions tables (reverse dependency order).
-- Run with: psql $POSTGRES_URL -f seed_reset.sql

BEGIN;

DELETE FROM minions.state_transitions;
DELETE FROM minions.heartbeats;
DELETE FROM minions.subtasks;
DELETE FROM minions.message_log;
DELETE FROM minions.events;
DELETE FROM minions.tool_calls;
DELETE FROM minions.messages;
DELETE FROM minions.agents;
DELETE FROM minions.tasks;
DELETE FROM minions.jobs;

COMMIT;
