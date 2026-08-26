"""The audit middleware must be installed exactly once.

`create_server()` adds ToolAuditMiddleware, and `cli.py` used to add a second
one to the server that function had just returned. Both instances then wrapped
every tool call: two "TOOL CALL"/"TOOL DONE" log lines, and -- because
record_tool_call sits in the middleware's `finally` -- two rows in `tool_calls`
for one real call, each with its own independently measured duration.

Measured on 2026-08-26 before the fix:

    04:06:01  claim_engineer_work   1247 ms
    04:06:01  claim_engineer_work   1471 ms
    04:14:21  report_pr             4248 ms
    04:14:21  report_pr             4453 ms

Two durations for one call is the tell. It also made the doubled log lines look
like two genuine calls racing, which sent a real investigation down the wrong
path -- the audit trail misleading its reader is the actual cost here, not the
duplicate rows.
"""

import re
from pathlib import Path

from minions.config import Config
from minions.server.mcp import create_server
from minions.server.middleware import ToolAuditMiddleware


class TestExactlyOnce:
    async def test_create_server_installs_one_audit_middleware(self, db):
        server = create_server(db, Config.from_env())

        installed = [m for m in server.middleware if isinstance(m, ToolAuditMiddleware)]

        assert len(installed) == 1, f"expected exactly one ToolAuditMiddleware, found {len(installed)}"

    def test_no_caller_adds_a_second_one(self):
        """Structural, because the doubling lived at the CALL SITE -- the server
        itself was always correct, so no test of create_server could catch it.

        cli.py builds a server in four places; only one of them added the extra
        middleware, so the other three were already right and this keeps them
        that way.
        """
        source = Path(__file__).resolve().parents[2] / "minions" / "cli.py"
        text = source.read_text(encoding="utf-8", errors="replace")

        offenders = re.findall(r"add_middleware\(\s*ToolAuditMiddleware", text)

        assert offenders == [], f"cli.py must leave audit middleware to create_server(); found {len(offenders)} registration(s)"
