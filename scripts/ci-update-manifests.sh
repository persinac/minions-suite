#!/usr/bin/env bash
#
# Rewrite the image tag in the k8s overlays to VERSION, so ArgoCD picks up the
# image CI just pushed.
#
# Counterpart to flashback-cns/scripts/ci-update-manifests.sh, deliberately much
# smaller. That repo is a monorepo shipping one image for 21 services, so it
# needs a SERVICE_MAP of source-path -> service names and per-path change
# detection to decide which kustomizations to touch.
#
# minion-suite ships ONE image consumed by all three Deployments through a single
# `images:` entry in the prod overlay. Every change affects that one image, so
# there is nothing to map and nothing to filter — which is also why there is no
# ALL_SERVICES list to forget to update when a Deployment is added.
#

set -euo pipefail

VERSION="${1:?Usage: ci-update-manifests.sh <VERSION>}"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid version format: $VERSION (expected X.Y.Z)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Every kustomization carrying an `images:` block. Currently just the prod
# overlay; a staging overlay would be added here.
KUSTOMIZATIONS=(
    "$PROJECT_ROOT/k8s/overlays/prod/minion-suite/kustomization.yaml"
)

updated=0
for kustomization in "${KUSTOMIZATIONS[@]}"; do
    if [[ ! -f "$kustomization" ]]; then
        echo "warning: $kustomization not found, skipping" >&2
        continue
    fi

    if ! grep -q '^\s*newTag:' "$kustomization"; then
        echo "error: no newTag: line in $kustomization" >&2
        exit 1
    fi

    sed -i "s/^\(\s*\)newTag:.*/\1newTag: \"${VERSION}\"/" "$kustomization"
    echo "  ${kustomization#"$PROJECT_ROOT"/} -> ${VERSION}"
    updated=$((updated + 1))
done

# A silent no-op here would let CI push an image and commit a manifest that still
# points at the previous tag — ArgoCD would report Synced while running old code.
if [[ "$updated" -eq 0 ]]; then
    echo "error: no kustomizations updated" >&2
    exit 1
fi

echo "Updated $updated kustomization(s) to ${VERSION}"
