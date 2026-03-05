"""Upload job artifacts (JSON snapshot + agent logs) to S3 on completion."""

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import Config
from .db import AbstractDatabase

logger = logging.getLogger(__name__)


class ArtifactUploader:
    def __init__(self, db: AbstractDatabase, config: Config):
        self.db = db
        self.config = config
        self._s3_client = None

    def is_enabled(self) -> bool:
        return bool(self.config.s3_artifact_bucket)

    def _get_s3_client(self):
        if self._s3_client is not None:
            return self._s3_client

        import boto3

        # Doppler injects AWS_ACCESS_KEY / AWS_SECRET_KEY; boto3 expects
        # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, so map explicitly.
        access_key = os.environ.get("AWS_ACCESS_KEY", "")
        secret_key = os.environ.get("AWS_SECRET_KEY", "")
        kwargs = {"region_name": self.config.s3_artifact_region}
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        self._s3_client = boto3.client("s3", **kwargs)
        return self._s3_client

    async def backfill(self):
        """Upload artifacts for all terminal jobs not yet archived to S3."""
        if not self.is_enabled():
            return

        from .core.models import JobStatus

        all_jobs = await self.db.get_all_jobs()
        terminal = {JobStatus.DONE, JobStatus.FAILED}
        candidates = [job for job in all_jobs if job.status in terminal]

        if not candidates:
            return

        uploaded = 0
        skipped = 0
        for job in candidates:
            events = await self.db.get_events(job.id)
            already_uploaded = any(e["event_type"] == "artifacts_uploaded" for e in events)
            if already_uploaded:
                skipped += 1
                continue

            prefix = await self.upload_job_artifacts(job.id)
            if prefix:
                bucket = self.config.s3_artifact_bucket
                await self.db.record_event(job.id, "artifacts_uploaded", "backfill", f"s3://{bucket}/{prefix}")
                uploaded += 1
            else:
                await self.db.record_event(job.id, "artifacts_upload_failed", "backfill", "uploader returned None")

        logger.info("Artifact backfill complete: %d uploaded, %d already in S3, %d total terminal", uploaded, skipped, len(candidates))

    async def upload_job_artifacts(self, job_id: str) -> str | None:
        """Upload job JSON snapshot and agent logs to S3.

        Returns the S3 prefix on success, or None on failure/disabled.
        """
        if not self.is_enabled():
            return None

        prefix = f"{self.config.s3_artifact_prefix}/jobs/{job_id}"

        try:
            # Export job data as JSON
            data = await self._export_job_data(job_id)
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            await self._upload_bytes(f"{prefix}/job.json", body, "application/json")

            # Upload agent log files
            log_files = await self._collect_log_files(job_id)
            for agent_id, role, log_path in log_files:
                log_key = f"{prefix}/logs/{role}-{agent_id}-{log_path.stem}.log"
                log_body = log_path.read_bytes()
                await self._upload_bytes(log_key, log_body, "text/plain")

            logger.info("Uploaded artifacts for job %s to s3://%s/%s", job_id, self.config.s3_artifact_bucket, prefix)
            return prefix

        except Exception:
            logger.exception("Failed to upload artifacts for job %s", job_id)
            return None

    async def _export_job_data(self, job_id: str) -> dict:
        """Query all DB tables and build a JSON-serializable snapshot."""
        from .core.models import _now

        job = await self.db.get_job(job_id)
        tasks = await self.db.get_tasks(job_id)
        agents = await self.db.get_agents_for_job(job_id)
        messages = await self.db.get_messages(job_id)
        events = await self.db.get_events(job_id)
        tool_calls = await self.db.get_tool_calls(job_id)
        usage = await self.db.get_job_usage(job_id)

        return {
            "exported_at": _now(),
            "job": job.model_dump() if job else None,
            "tasks": [t.model_dump() for t in tasks],
            "agents": [a.model_dump() for a in agents],
            "messages": [m.model_dump() for m in messages],
            "events": events,
            "tool_calls": tool_calls,
            "usage_summary": usage,
        }

    async def _collect_log_files(self, job_id: str) -> list[tuple[str, str, Path]]:
        """Return (agent_id, role, Path) for each agent with an existing log file."""
        agents = await self.db.get_agents_for_job(job_id)
        results = []
        for a in agents:
            if not a.log_file:
                continue
            p = Path(a.log_file)
            if p.exists():
                results.append((a.id, a.role, p))
        return results

    async def _upload_bytes(self, key: str, body: bytes, content_type: str):
        """Upload bytes to S3 using run_in_executor for the sync boto3 call."""
        client = self._get_s3_client()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: client.put_object(
                Bucket=self.config.s3_artifact_bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            ),
        )
