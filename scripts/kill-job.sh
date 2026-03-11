#!/usr/bin/env bash
# Kill a minions job by ID — marks all subtasks, agents, tasks, and the job as failed.
# Usage: ./scripts/kill-job.sh <job_id> [--port 5434]

set -euo pipefail

JOB_ID="${1:?Usage: kill-job.sh <job_id> [--port PORT]}"
shift

DB_PORT="${MINION_DB_PORT:-5434}"
DB_USER="${MINION_DB_USER:-minion}"
DB_NAME="${MINION_DB_NAME:-minion}"
DB_HOST="${MINION_DB_HOST:-localhost}"
DB_PASS="${MINION_DB_PASS:-minion}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) DB_PORT="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

export PGPASSWORD="$DB_PASS"

echo "Killing job $JOB_ID ..."

# Check job exists
JOB_STATUS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
    "SELECT status FROM minions.jobs WHERE id = '$JOB_ID';")

if [[ -z "$JOB_STATUS" ]]; then
    echo "Job $JOB_ID not found."
    exit 1
fi

if [[ "$JOB_STATUS" == "failed" || "$JOB_STATUS" == "done" ]]; then
    echo "Job $JOB_ID is already $JOB_STATUS."
    exit 0
fi

echo "  Current status: $JOB_STATUS"

# Kill everything in order: subtasks -> agents -> tasks -> job
SUBS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
    "UPDATE minions.subtasks SET status = 'failed', error = 'manually killed'
     WHERE task_id IN (SELECT id FROM minions.tasks WHERE job_id = '$JOB_ID')
       AND status NOT IN ('completed', 'failed');
     SELECT count(*) FROM minions.subtasks
     WHERE task_id IN (SELECT id FROM minions.tasks WHERE job_id = '$JOB_ID')
       AND error = 'manually killed';")

AGENTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
    "UPDATE minions.agents SET status = 'failed', finished_at = NOW(), error = 'manually killed'
     WHERE job_id = '$JOB_ID' AND status NOT IN ('done', 'failed');
     SELECT count(*) FROM minions.agents WHERE job_id = '$JOB_ID' AND error = 'manually killed';")

TASKS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
    "UPDATE minions.tasks SET status = 'failed', error = 'manually killed'
     WHERE job_id = '$JOB_ID' AND status NOT IN ('done', 'failed', 'merged');
     SELECT count(*) FROM minions.tasks WHERE job_id = '$JOB_ID' AND error = 'manually killed';")

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c \
    "UPDATE minions.jobs SET status = 'failed', error = 'manually killed' WHERE id = '$JOB_ID';" > /dev/null

echo "  Killed: ${SUBS} subtasks, ${AGENTS} agents, ${TASKS} tasks"
echo "  Job $JOB_ID -> failed"
