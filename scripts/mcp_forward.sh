#!/usr/bin/env bash
# Supervised port-forward from this host to the in-cluster minions MCP server.
#
# The MCP service is ClusterIP with no ingress, so a herder running on a laptop
# has no route to it. This holds one open and restarts it when it drops.
#
#   mcp_forward.sh            run the supervisor (foreground; use a unit or a pane)
#   mcp_forward.sh --check    exit 0 if the tunnel is up, 1 if not
#
# --check exists because a port-forward dies QUIETLY. Without a way to tell
# "tunnel down" from "queue empty", the trigger reads a dead tunnel as no work
# waiting, stays silent, and the engine's 900s fallback bills the metered path
# while everything looks fine.
#
# What --check proves is the TUNNEL, not the server: kubectl accepts the local
# connection before it knows whether the pod behind it is healthy. It is the
# cheap precondition, not a health check for the MCP server itself.
set -uo pipefail

NAMESPACE="${MINIONS_NAMESPACE:-minion-suite}"
SERVICE="${MINIONS_MCP_SERVICE:-svc/minion-suite}"
PORT="${MINIONS_MCP_PORT:-8321}"
STATE_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}/minions"
STATE_FILE="$STATE_DIR/mcp-forward.state"

_tunnel_up() {
    # bash's /dev/tcp — no curl/nc dependency, and an SSE endpoint would hold a
    # GET open anyway, so a connect test is the honest check here.
    timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$PORT" 2>/dev/null
}

if [ "${1:-}" = "--check" ]; then
    _tunnel_up && exit 0
    exit 1
fi

mkdir -p "$STATE_DIR"
echo "supervising port-forward $NAMESPACE/$SERVICE :$PORT" >&2

backoff=1
while true; do
    printf 'pid=%s started=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$STATE_FILE"

    # Foreground: when this returns the tunnel is gone, whatever the reason.
    kubectl port-forward -n "$NAMESPACE" "$SERVICE" "$PORT:$PORT" >/dev/null 2>&1
    rc=$?

    echo "port-forward exited (rc=$rc); retrying in ${backoff}s" >&2
    sleep "$backoff"
    # Cap the backoff: a cluster that is briefly unreachable should not push
    # reconnection out to minutes, because every second without a tunnel is a
    # second the trigger cannot see work and the metered fallback creeps closer.
    backoff=$(( backoff < 30 ? backoff * 2 : 30 ))
done
