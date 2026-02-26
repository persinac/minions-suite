# DevOps Review

## Dockerfiles
- Multi-stage builds separate build dependencies from runtime image
- Base images use specific tags (not `latest`) for reproducibility
- COPY instructions are ordered for optimal layer caching (deps before source)
- No secrets baked into the image (use build-time args or runtime env injection)
- Health checks are defined (HEALTHCHECK instruction or orchestrator probe)
- Non-root USER is specified for the runtime stage

## CI/CD Pipelines
- Pipeline stages are ordered correctly (lint → test → build → deploy)
- Tests run before deployment — never deploy without passing tests
- Secrets are injected via CI variables, not hardcoded in pipeline files
- Deployment steps have rollback mechanisms or are gated behind manual approval
- Cache configurations are correct (cache keys match dependency lockfiles)
- Artifact retention is set appropriately (not storing build artifacts forever)

## Infrastructure as Code
- Resource naming follows a consistent convention
- Tags/labels are applied for cost tracking and ownership
- Security groups and network policies follow least-privilege
- Secrets management uses a proper secrets store (Vault, Doppler, AWS Secrets Manager)
- State files (Terraform, Pulumi) are stored remotely with locking

## Shell Scripts
- Scripts use `set -euo pipefail` (fail on errors, undefined vars, pipe failures)
- All variables are quoted: `"$VAR"` not `$VAR`
- Temporary files are cleaned up (use `trap` for cleanup on exit)
- Scripts are portable across common shells (bash 4+, avoid bashisms in /bin/sh scripts)
- Exit codes are meaningful (non-zero for failure)

## Monitoring & Alerting
- New services have health check endpoints
- Logs are structured (JSON) and include correlation IDs
- Metrics are emitted for key operations (latency, error rates, queue depths)
- Alerts have clear runbooks or resolution steps
