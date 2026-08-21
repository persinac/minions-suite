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

HEALTH_INTERVAL="${MINIONS_MCP_HEALTH_INTERVAL:-30}"

_tunnel_up() {
    # bash's /dev/tcp — no curl/nc dependency, and an SSE endpoint would hold a
    # GET open anyway, so a connect test is the honest check here.
    timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$PORT" 2>/dev/null
}

_server_answers() {
    # Does the MCP SERVER respond, not merely the local socket accept?
    #
    # This is the check whose absence cost four days. `kubectl port-forward` to a
    # SERVICE pins one pod at startup and does NOT exit when that pod is
    # replaced: the forward stays up, keeps accepting local connections, and
    # forwards them nowhere. On 2026-08-17 exactly that happened, the supervisor
    # sat waiting for an exit that never came, and every engineer job paid the
    # 900s herder timeout and fell back to the metered API while `--check` kept
    # passing.
    #
    # Piped through `head -1` on purpose: /sse holds the response open, so
    # waiting for the body would always time out. The status line arrives
    # immediately and head closing the pipe ends the request.
    [ "$(curl -s -i -N -m 5 "http://127.0.0.1:$PORT/sse" 2>/dev/null | head -1 | grep -c ' 200 ')" = "1" ]
}

if [ "${1:-}" = "--check" ]; then
    _tunnel_up && exit 0
    exit 1
fi

if [ "${1:-}" = "--check-server" ]; then
    _server_answers && exit 0
    exit 1
fi

mkdir -p "$STATE_DIR"
echo "supervising port-forward $NAMESPACE/$SERVICE :$PORT" >&2

backoff=1
while true; do
    printf 'pid=%s started=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$STATE_FILE"

    # BACKGROUND, not foreground. Waiting for kubectl to exit was the bug: a
    # forward pinned to a replaced pod never exits, so the supervisor blocked
    # forever on a tunnel that had been useless for days. Supervising means
    # asking whether it still WORKS, not whether it is still running.
    kubectl port-forward -n "$NAMESPACE" "$SERVICE" "$PORT:$PORT" >/dev/null 2>&1 &
    fwd=$!

    # Give it a moment to bind before the first probe, so a healthy start is not
    # torn down as a failure.
    sleep 2

    while kill -0 "$fwd" 2>/dev/null; do
        if _server_answers; then
            backoff=1  # a working tunnel earns back the fast first retry
        else
            echo "port-forward is up but the MCP server does not answer — replacing it" >&2
            kill "$fwd" 2>/dev/null
            break
        fi
        sleep "$HEALTH_INTERVAL"
    done

    wait "$fwd" 2>/dev/null
    rc=$?

    echo "port-forward exited (rc=$rc); retrying in ${backoff}s" >&2
    sleep "$backoff"
    # Cap the backoff: a cluster that is briefly unreachable should not push
    # reconnection out to minutes, because every second without a tunnel is a
    # second the trigger cannot see work and the metered fallback creeps closer.
    backoff=$(( backoff < 30 ? backoff * 2 : 30 ))
done
