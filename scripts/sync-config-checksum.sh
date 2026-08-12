#!/usr/bin/env bash
#
# Stamp the sha256 of k8s/base/config/projects.yaml into the pod-template
# annotation of every Deployment that mounts it.
#
# Why this exists: a projects.yaml-only commit does not change the image tag, so
# ci-update-manifests.sh leaves the manifests untouched and ArgoCD has nothing to
# roll. The ConfigMap syncs, but the mount uses subPath (the kubelet never
# refreshes those) and build_registry reads the file once at startup, so the
# running engine keeps routing on stale config while ArgoCD reports Synced.
# Changing the annotation changes the pod template, which is what makes ArgoCD
# roll the pods and the new config take effect.
#
# Run this after editing projects.yaml, then commit both. tests/test_projects_yaml.py
# fails if you forget.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

CONFIG_FILE="$PROJECT_ROOT/k8s/base/config/projects.yaml"

# Every Deployment mounting minion-suite-config-files. A Deployment added here
# without its annotation would silently go stale, so the test derives this same
# list from the manifests rather than hardcoding it.
DEPLOYMENTS=(
    "$PROJECT_ROOT/k8s/base/minion-suite/deployment.yaml"
    "$PROJECT_ROOT/k8s/base/input-sources/deployment.yaml"
)

ANNOTATION="minion-suite/projects-checksum"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "error: $CONFIG_FILE not found" >&2
    exit 1
fi

CHECKSUM="$(sha256sum "$CONFIG_FILE" | cut -d' ' -f1)"

updated=0
unchanged=0
for manifest in "${DEPLOYMENTS[@]}"; do
    if [[ ! -f "$manifest" ]]; then
        echo "error: $manifest not found" >&2
        exit 1
    fi

    if ! grep -q "$ANNOTATION:" "$manifest"; then
        echo "error: no '$ANNOTATION:' line in ${manifest#"$PROJECT_ROOT"/}" >&2
        echo "       add the annotation to its pod template first" >&2
        exit 1
    fi

    current="$(grep -oE "$ANNOTATION: \"[0-9a-f]{64}\"" "$manifest" | grep -oE '[0-9a-f]{64}' || true)"
    if [[ "$current" == "$CHECKSUM" ]]; then
        unchanged=$((unchanged + 1))
        continue
    fi

    sed -i "s|\(\s*\)$ANNOTATION: .*|\1$ANNOTATION: \"$CHECKSUM\"|" "$manifest"
    echo "  ${manifest#"$PROJECT_ROOT"/} -> ${CHECKSUM:0:12}…"
    updated=$((updated + 1))
done

if [[ "$updated" -eq 0 ]]; then
    echo "Already in sync ($unchanged manifest(s)) at ${CHECKSUM:0:12}…"
else
    echo "Stamped $updated manifest(s); commit these with projects.yaml so ArgoCD rolls the pods."
fi
