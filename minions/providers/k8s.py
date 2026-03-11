"""K8s Job launcher for agent execution.

Creates discrete Kubernetes Jobs for each agent invocation, providing
true pod isolation, independent resource limits, automatic cleanup,
and native K8s observability.
"""

import json
import logging
from datetime import UTC, datetime

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config

from ..agents.dispatch import AgentWorkItem, serialize_work_item

logger = logging.getLogger(__name__)

# Role short codes for K8s Job naming (max 63 chars total)
ROLE_SHORTS = {
    "spec_analyst": "sa",
    "arbiter": "ab",
    "backend_engineer": "be",
    "frontend_engineer": "fe",
    "database_engineer": "db",
    "code_reviewer": "cr",
    "deploy_monitor": "dm",
}

# Default resource requests/limits per role category
_ENGINEER_RESOURCES = {
    "requests": {"cpu": "500m", "memory": "1Gi"},
    "limits": {"cpu": "2", "memory": "4Gi"},
}
_LIGHTWEIGHT_RESOURCES = {
    "requests": {"cpu": "250m", "memory": "512Mi"},
    "limits": {"cpu": "1", "memory": "2Gi"},
}
ROLE_RESOURCES = {
    "spec_analyst": _LIGHTWEIGHT_RESOURCES,
    "arbiter": _LIGHTWEIGHT_RESOURCES,
    "backend_engineer": _ENGINEER_RESOURCES,
    "frontend_engineer": _ENGINEER_RESOURCES,
    "database_engineer": _ENGINEER_RESOURCES,
    "code_reviewer": _LIGHTWEIGHT_RESOURCES,
    "deploy_monitor": _LIGHTWEIGHT_RESOURCES,
}

MANAGED_BY_LABEL = "minion-suite"


def _job_name(role: str, job_id: str, agent_id: str) -> str:
    """Generate a K8s-safe Job name: agent-{role_short}-{job_id[:8]}-{agent_id[:8]}."""
    short = ROLE_SHORTS.get(role, role[:2])
    return f"agent-{short}-{job_id[:8]}-{agent_id[:8]}"


def _configmap_name(job_name: str) -> str:
    """ConfigMap name derived from Job name."""
    return f"{job_name}-config"


class K8sJobLauncher:
    """Creates and manages K8s Jobs for agent execution."""

    def __init__(
        self,
        namespace: str,
        agent_image: str,
        agent_sa: str,
        job_ttl: int,
        secrets_name: str,
    ):
        self.namespace = namespace
        self.agent_image = agent_image
        self.agent_sa = agent_sa
        self.job_ttl = job_ttl
        self.secrets_name = secrets_name
        self._initialized = False

    async def _ensure_config(self) -> None:
        """Load K8s config once (in-cluster preferred, falls back to kubeconfig)."""
        if self._initialized:
            return
        try:
            k8s_config.load_incluster_config()
            logger.info("K8s: loaded in-cluster config")
        except k8s_config.ConfigException:
            await k8s_config.load_kube_config()
            logger.info("K8s: loaded kubeconfig")
        self._initialized = True

    async def launch_agent(self, work_item: AgentWorkItem, repo_clone_url: str) -> str:
        """Create a ConfigMap + K8s Job for an agent. Returns the Job name."""
        await self._ensure_config()

        name = _job_name(work_item.role, work_item.job_id, work_item.agent_id)
        cm_name = _configmap_name(name)

        # Serialize work item into ConfigMap
        work_item_json = json.dumps(json.loads(serialize_work_item(work_item).decode("utf-8")), indent=2)

        core_v1 = k8s_client.CoreV1Api()
        batch_v1 = k8s_client.BatchV1Api()

        labels = {
            "app.kubernetes.io/managed-by": MANAGED_BY_LABEL,
            "minion-suite/job-id": work_item.job_id[:8],
            "minion-suite/agent-id": work_item.agent_id[:8],
            "minion-suite/role": work_item.role,
        }

        try:
            # Create ConfigMap with the work item
            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(name=cm_name, namespace=self.namespace, labels=labels),
                data={"work-item.json": work_item_json},
            )
            await core_v1.create_namespaced_config_map(namespace=self.namespace, body=cm)

            # Build resource requirements
            role_res = ROLE_RESOURCES.get(work_item.role, _LIGHTWEIGHT_RESOURCES)

            # Build the Job spec
            job = k8s_client.V1Job(
                metadata=k8s_client.V1ObjectMeta(
                    name=name,
                    namespace=self.namespace,
                    labels={**labels, "app.kubernetes.io/component": "agent"},
                ),
                spec=k8s_client.V1JobSpec(
                    backoff_limit=0,
                    ttl_seconds_after_finished=self.job_ttl,
                    active_deadline_seconds=work_item.timeout + 60,
                    template=k8s_client.V1PodTemplateSpec(
                        metadata=k8s_client.V1ObjectMeta(
                            labels={
                                "app.kubernetes.io/managed-by": MANAGED_BY_LABEL,
                                "app.kubernetes.io/component": "agent",
                                "minion-suite/role": work_item.role,
                            },
                        ),
                        spec=k8s_client.V1PodSpec(
                            service_account_name=self.agent_sa,
                            restart_policy="Never",
                            init_containers=[
                                k8s_client.V1Container(
                                    name="git-clone",
                                    image="alpine/git:latest",
                                    command=["sh", "-c"],
                                    args=[f"git clone --depth 1 {repo_clone_url} /workspace/repo"],
                                    volume_mounts=[
                                        k8s_client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                                    ],
                                    resources=k8s_client.V1ResourceRequirements(
                                        requests={"cpu": "100m", "memory": "128Mi"},
                                        limits={"cpu": "500m", "memory": "512Mi"},
                                    ),
                                ),
                            ],
                            containers=[
                                k8s_client.V1Container(
                                    name="agent",
                                    image=self.agent_image,
                                    command=["python", "-m", "minions", "agent-worker"],
                                    env=[
                                        k8s_client.V1EnvVar(name="AGENT_WORK_ITEM_PATH", value="/config/work-item.json"),
                                        k8s_client.V1EnvVar(name="AGENT_WORKING_DIR", value="/workspace/repo"),
                                    ],
                                    env_from=[
                                        k8s_client.V1EnvFromSource(
                                            secret_ref=k8s_client.V1SecretEnvSource(name=self.secrets_name),
                                        ),
                                    ],
                                    volume_mounts=[
                                        k8s_client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                                        k8s_client.V1VolumeMount(name="config", mount_path="/config", read_only=True),
                                    ],
                                    resources=k8s_client.V1ResourceRequirements(
                                        requests=role_res["requests"],
                                        limits=role_res["limits"],
                                    ),
                                ),
                            ],
                            volumes=[
                                k8s_client.V1Volume(name="workspace", empty_dir=k8s_client.V1EmptyDirVolumeSource()),
                                k8s_client.V1Volume(name="config", config_map=k8s_client.V1ConfigMapVolumeSource(name=cm_name)),
                            ],
                        ),
                    ),
                ),
            )
            await batch_v1.create_namespaced_job(namespace=self.namespace, body=job)
            logger.info("K8s Job created: %s (role=%s, ns=%s)", name, work_item.role, self.namespace)
            return name

        finally:
            await core_v1.api_client.close()
            await batch_v1.api_client.close()

    async def get_job_status(self, job_name: str) -> str:
        """Returns: pending, running, succeeded, failed, or unknown."""
        await self._ensure_config()
        batch_v1 = k8s_client.BatchV1Api()
        try:
            job = await batch_v1.read_namespaced_job(name=job_name, namespace=self.namespace)
            status = job.status

            if status.succeeded and status.succeeded > 0:
                return "succeeded"
            if status.failed and status.failed > 0:
                return "failed"
            if status.active and status.active > 0:
                return "running"
            return "pending"
        except k8s_client.ApiException as e:
            if e.status == 404:
                return "unknown"
            raise
        finally:
            await batch_v1.api_client.close()

    async def get_pod_logs(self, job_name: str, tail_lines: int = 200) -> str:
        """Fetch logs from the agent pod of a K8s Job."""
        await self._ensure_config()
        core_v1 = k8s_client.CoreV1Api()
        try:
            pods = await core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"job-name={job_name}",
            )
            if not pods.items:
                return f"No pods found for Job {job_name}"

            pod = pods.items[0]
            try:
                logs = await core_v1.read_namespaced_pod_log(
                    name=pod.metadata.name,
                    namespace=self.namespace,
                    container="agent",
                    tail_lines=tail_lines,
                )
                return logs or "(empty log)"
            except k8s_client.ApiException:
                return f"Could not read logs for pod {pod.metadata.name}"
        finally:
            await core_v1.api_client.close()

    async def delete_job(self, job_name: str) -> None:
        """Delete a K8s Job and its associated ConfigMap."""
        await self._ensure_config()
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()
        try:
            try:
                await batch_v1.delete_namespaced_job(
                    name=job_name,
                    namespace=self.namespace,
                    propagation_policy="Background",
                )
                logger.info("Deleted K8s Job: %s", job_name)
            except k8s_client.ApiException as e:
                if e.status != 404:
                    raise

            cm_name = _configmap_name(job_name)
            try:
                await core_v1.delete_namespaced_config_map(name=cm_name, namespace=self.namespace)
            except k8s_client.ApiException as e:
                if e.status != 404:
                    raise
        finally:
            await batch_v1.api_client.close()
            await core_v1.api_client.close()

    async def cleanup_old_jobs(self, max_age_hours: int = 2) -> int:
        """Delete orphaned Jobs and ConfigMaps older than max_age_hours. Returns count deleted."""
        await self._ensure_config()
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()
        deleted = 0
        cutoff = datetime.now(UTC)

        try:
            jobs = await batch_v1.list_namespaced_job(
                namespace=self.namespace,
                label_selector=f"app.kubernetes.io/managed-by={MANAGED_BY_LABEL}",
            )
            for job in jobs.items:
                created = job.metadata.creation_timestamp
                if not created:
                    continue
                age_hours = (cutoff - created).total_seconds() / 3600
                if age_hours < max_age_hours:
                    continue

                is_finished = (
                    (job.status.succeeded and job.status.succeeded > 0)
                    or (job.status.failed and job.status.failed > 0)
                    or (not job.status.active or job.status.active == 0)
                )
                if not is_finished:
                    continue

                job_name = job.metadata.name
                try:
                    await batch_v1.delete_namespaced_job(
                        name=job_name,
                        namespace=self.namespace,
                        propagation_policy="Background",
                    )
                    deleted += 1
                except k8s_client.ApiException:
                    logger.debug("Failed to delete old Job %s", job_name, exc_info=True)

                cm_name = _configmap_name(job_name)
                try:
                    await core_v1.delete_namespaced_config_map(name=cm_name, namespace=self.namespace)
                except k8s_client.ApiException:
                    pass

            if deleted:
                logger.info("Cleaned up %d old K8s Jobs (older than %dh)", deleted, max_age_hours)
            return deleted
        finally:
            await batch_v1.api_client.close()
            await core_v1.api_client.close()

    async def list_agent_jobs(self) -> list[dict]:
        """List all minion-suite managed Jobs with status info."""
        await self._ensure_config()
        batch_v1 = k8s_client.BatchV1Api()
        try:
            jobs = await batch_v1.list_namespaced_job(
                namespace=self.namespace,
                label_selector=f"app.kubernetes.io/managed-by={MANAGED_BY_LABEL}",
            )
            result = []
            for job in jobs.items:
                status = "pending"
                if job.status.succeeded and job.status.succeeded > 0:
                    status = "succeeded"
                elif job.status.failed and job.status.failed > 0:
                    status = "failed"
                elif job.status.active and job.status.active > 0:
                    status = "running"

                result.append(
                    {
                        "name": job.metadata.name,
                        "status": status,
                        "role": job.metadata.labels.get("minion-suite/role", ""),
                        "job_id": job.metadata.labels.get("minion-suite/job-id", ""),
                        "agent_id": job.metadata.labels.get("minion-suite/agent-id", ""),
                        "created": job.metadata.creation_timestamp.isoformat() if job.metadata.creation_timestamp else "",
                    }
                )
            return result
        finally:
            await batch_v1.api_client.close()
