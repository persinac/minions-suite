#!/usr/bin/env python3
"""Fleet GitHub admin for the minions merge gate.

Runs with YOUR `gh` credentials (needs admin:org), not the minions App — the App
deliberately has no org-admin grant.

    ./scripts/gh-fleet.py doctor              # what works, what does not, and the fix
    ./scripts/gh-fleet.py ruleset show        # the org ruleset and its targets
    ./scripts/gh-fleet.py checks wallet-api   # effective required checks on a branch
    ./scripts/gh-fleet.py ruleset apply <file.json>
    ./scripts/gh-fleet.py squash-only [--apply]

Everything defaults to read-only. Mutating commands need --apply.

Why a script rather than pasted curl: the gate depends on several API surfaces
that fail in *quiet* ways — a ruleset that does not appear on the classic
branch-protection endpoint, an App permission that turns every check-run lookup
into a 403 the gate reports as "no checks reported". Both happened. `doctor`
probes each one and names the fix.
"""

import argparse
import json
import subprocess
import sys

ORG = "flippin-balls"


# --- plumbing ---------------------------------------------------------------


def gh(*args: str, check: bool = False) -> tuple[int, str]:
    """Run gh, returning (returncode, stdout-or-stderr)."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.returncode, (result.stdout or result.stderr).strip()


def gh_json(*args: str):
    code, out = gh(*args)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def ok(msg):
    print(f"  \033[32mOK\033[0m    {msg}")


def bad(msg, fix=""):
    print(f"  \033[31mFAIL\033[0m  {msg}")
    if fix:
        print(f"        fix: {fix}")


def warn(msg):
    print(f"  \033[33mWARN\033[0m  {msg}")


# --- commands ---------------------------------------------------------------


def cmd_doctor(args):
    """Probe every surface the merge gate depends on."""
    print(f"\nGitHub fleet doctor — org {ORG}\n")

    print("your gh credentials")
    code, _ = gh("api", f"/orgs/{ORG}/rulesets", "--jq", "length")
    if code == 0:
        ok("admin:org — can read org rulesets")
    else:
        bad("cannot read org rulesets", "gh auth refresh -h github.com -s admin:org")

    print("\norg ruleset")
    rulesets = gh_json("api", f"/orgs/{ORG}/rulesets") or []
    if not rulesets:
        bad("no org ruleset found", "./scripts/gh-fleet.py ruleset apply <file.json>")
    else:
        for rs in rulesets:
            state = rs.get("enforcement")
            label = f"#{rs.get('id')} {rs.get('name')} ({state})"
            if state == "active":
                ok(label)
            else:
                warn(f"{label} — not enforcing")

    print("\nminions App permissions (what the runtime gate needs)")
    print("  These are checked against a sample repo using YOUR token, which has")
    print("  broader access — so a PASS here does not prove the App can do it.")
    print("  The App's own grants are set at:")
    print(f"        https://github.com/organizations/{ORG}/settings/apps")
    print("  Required for the merge gate: Checks: Read-only, Contents: Read & write,")
    print("  Pull requests: Read & write, Metadata: Read-only.")

    print("\nsample repo wiring")
    for repo in args.repos or ["wallet-api"]:
        _report_repo(repo)
    print()


def _report_repo(repo: str):
    full = f"{ORG}/{repo}"
    print(f"\n  {full}")

    rules = gh_json("api", f"/repos/{full}/rules/branches/main")
    if rules is None:
        bad(f"{full}: cannot read branch rules")
        return

    types = {r.get("type") for r in rules}
    contexts = [
        c.get("context")
        for r in rules
        if r.get("type") == "required_status_checks"
        for c in (r.get("parameters") or {}).get("required_status_checks", [])
    ]

    if contexts:
        ok(f"required checks: {contexts}")
    else:
        bad("no required status checks — the fail-closed gate blocks every agent PR here")

    for rule, why in (("non_fast_forward", "force-push blocked"), ("deletion", "deletion blocked")):
        if rule in types:
            ok(why)
        else:
            warn(f"{rule} not set")

    repo_meta = gh_json("api", f"/repos/{full}", "--jq", "{squash:.allow_squash_merge,merge:.allow_merge_commit,rebase:.allow_rebase_merge}")
    if repo_meta:
        if repo_meta.get("squash"):
            extra = [k for k in ("merge", "rebase") if repo_meta.get(k)]
            if extra:
                warn(f"squash allowed, but so are {extra} — run: squash-only --apply")
            else:
                ok("squash-only")
        else:
            bad("squash merging DISABLED — every minions auto-merge will fail",
                "./scripts/gh-fleet.py squash-only --apply")


def cmd_ruleset_show(args):
    rulesets = gh_json("api", f"/orgs/{ORG}/rulesets") or []
    if not rulesets:
        print("  no org rulesets")
        return
    for rs in rulesets:
        detail = gh_json("api", f"/orgs/{ORG}/rulesets/{rs['id']}") or {}
        conds = (detail.get("conditions") or {}).get("repository_name", {})
        print(f"\n  #{rs['id']}  {rs['name']}  [{rs['enforcement']}]")
        print(f"    rules:   {[r['type'] for r in detail.get('rules', [])]}")
        print(f"    bypass:  {detail.get('bypass_actors') or '[] (correct)'}")
        include = conds.get("include") or []
        print(f"    repos:   {len(include)} targeted")
        for name in include:
            print(f"      - {name}")


def cmd_checks(args):
    for repo in args.repos:
        _report_repo(repo)
    print()


def cmd_ruleset_apply(args):
    payload = json.load(open(args.file))
    # Strip commentary: GitHub 422s on unexpected parameters.
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if not k.startswith("_comment")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    payload = strip(payload)
    name = payload.get("name")

    existing = next((r for r in (gh_json("api", f"/orgs/{ORG}/rulesets") or []) if r.get("name") == name), None)

    if not args.apply:
        verb = "UPDATE" if existing else "CREATE"
        print(f"  would {verb} ruleset {name!r}")
        print(f"    rules:  {[r['type'] for r in payload.get('rules', [])]}")
        print(f"    repos:  {len(payload['conditions']['repository_name']['include'])}")
        print(f"    bypass: {payload.get('bypass_actors')}")
        print("\n  re-run with --apply")
        return

    body = json.dumps(payload)
    if existing:
        code, out = _gh_stdin(["api", "--method", "PUT", f"/orgs/{ORG}/rulesets/{existing['id']}", "--input", "-"], body)
        print(f"  updated #{existing['id']}" if code == 0 else f"  FAILED: {out[:300]}")
    else:
        code, out = _gh_stdin(["api", "--method", "POST", f"/orgs/{ORG}/rulesets", "--input", "-"], body)
        print("  created" if code == 0 else f"  FAILED: {out[:300]}")


def _gh_stdin(args, body: str) -> tuple[int, str]:
    result = subprocess.run(["gh", *args], input=body, capture_output=True, text=True)
    return result.returncode, (result.stdout or result.stderr).strip()


def cmd_squash_only(args):
    """Squash-only on every repo the ruleset targets.

    minions runs `gh pr merge --squash --delete-branch`. If squash is disallowed
    repo-side, every auto-merge fails — after the review has already been paid for.
    """
    repos = args.repos
    if not repos:
        rulesets = gh_json("api", f"/orgs/{ORG}/rulesets") or []
        for rs in rulesets:
            detail = gh_json("api", f"/orgs/{ORG}/rulesets/{rs['id']}") or {}
            repos += ((detail.get("conditions") or {}).get("repository_name") or {}).get("include") or []
        repos = sorted(set(repos))

    if not repos:
        sys.exit("  no repos found — pass them explicitly or create the ruleset first")

    print(f"  {'applying to' if args.apply else 'would apply to'} {len(repos)} repos\n")
    for repo in repos:
        if not args.apply:
            print(f"    {repo}")
            continue
        code, out = gh(
            "api", "--method", "PATCH", f"/repos/{ORG}/{repo}",
            "-F", "allow_squash_merge=true",
            "-F", "allow_merge_commit=false",
            "-F", "allow_rebase_merge=false",
        )
        print(f"    {repo}: {'squash-only' if code == 0 else 'FAILED ' + out[:80]}")

    if not args.apply:
        print("\n  re-run with --apply")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="probe everything the merge gate depends on")
    d.add_argument("repos", nargs="*", help="repos to sample (default: wallet-api)")
    d.set_defaults(func=cmd_doctor)

    c = sub.add_parser("checks", help="effective required checks on a repo's default branch")
    c.add_argument("repos", nargs="+")
    c.set_defaults(func=cmd_checks)

    rs = sub.add_parser("ruleset", help="inspect or apply the org ruleset")
    rs_sub = rs.add_subparsers(dest="sub", required=True)
    rs_show = rs_sub.add_parser("show")
    rs_show.set_defaults(func=cmd_ruleset_show)
    rs_apply = rs_sub.add_parser("apply")
    rs_apply.add_argument("file")
    rs_apply.add_argument("--apply", action="store_true", help="actually write")
    rs_apply.set_defaults(func=cmd_ruleset_apply)

    s = sub.add_parser("squash-only", help="disable merge-commit and rebase on targeted repos")
    s.add_argument("repos", nargs="*")
    s.add_argument("--apply", action="store_true", help="actually write")
    s.set_defaults(func=cmd_squash_only)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
