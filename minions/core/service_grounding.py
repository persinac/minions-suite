"""Whether the arbiter's service choice is grounded in the spec it read.

The arbiter routes tasks from a name list, with no tools to inspect any repo.
Every name in the registry is "valid", so validity alone cannot catch the
routing failure that actually happened: job 9a1aeba4's spec named flashback-cns
repeatedly and cited services/game_play_router, and the arbiter still created
the task on flashback-process — a docs repo whose name merely sounded right.
Nothing refused it, and an engineer checked out the wrong repo.

The rule here is deliberately one-sided, in the same spirit as
missing_verdicts(): it flags a choice only when the spec mentions OTHER
registered services and never the chosen one. A spec that names no service, or
that names the chosen one anywhere, grounds the choice — so a false flag needs
both a name-dropped bystander and a never-mentioned target, and the caller's
refuse-once handling caps even that at one extra turn.

Pure text and registry shapes only — no DB, no IO — mirroring spec_contract.py,
which is the other refuse-with-remedy contract enforced at the same MCP call
site.
"""

import re
from pathlib import PurePosixPath


def _casefold_tokens(*candidates: str) -> set[str]:
    return {c.casefold() for c in candidates if c}


def evidence_tokens(registry) -> dict[str, set[str]]:
    """Per service: every identifier a spec could plausibly use to mean it.

    Beyond the service key itself: the owning project's name, the tail of
    project_id (`flippin-balls/x` → `x`), and the basenames of repo_path and
    clone_url (`.git` stripped). All casefolded — projects.yaml keys are
    lowercase but GitHub spells some repos `Flashback-Android`. Extra tokens
    only ever make a choice easier to ground, never harder.
    """
    tokens: dict[str, set[str]] = {}
    for project in registry.values():
        if not project.services:
            continue
        for svc_name, svc in project.services.items():
            project_id = svc.project_id or project.project_id or ""
            clone_tail = ""
            if svc.clone_url:
                clone_tail = PurePosixPath(svc.clone_url.rstrip("/")).name
                if clone_tail.endswith(".git"):
                    clone_tail = clone_tail[: -len(".git")]
            repo_tail = ""
            if svc.repo_path:
                repo_tail = PurePosixPath(svc.repo_path.rstrip("/")).name
            tokens[svc_name] = _casefold_tokens(
                svc_name,
                project.name,
                project_id.rsplit("/", 1)[-1],
                repo_tail,
                clone_tail,
            )
    return tokens


def _token_pattern(token: str) -> re.Pattern:
    # \b treats "-" as a boundary, so `pinball-db` would match inside
    # `pinball-db-web`. Identifier characters on either side disqualify the hit.
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])")


def mentioned_services(text: str, tokens: dict[str, set[str]]) -> set[str]:
    """Service names whose evidence tokens appear in `text`."""
    if not text:
        return set()
    folded = text.casefold()
    mentioned = set()
    for svc_name, toks in tokens.items():
        for tok in toks:
            if _token_pattern(tok).search(folded):
                mentioned.add(svc_name)
                break
    return mentioned


def check_grounding(service: str, spec_text: str, registry) -> list[str]:
    """[] when the choice is grounded; else the OTHER services the spec mentions.

    Grounded means any of: empty registry, single-service registry, the spec
    mentions the chosen service, or the spec mentions no registered service at
    all. Only "the spec names other services and never this one" comes back
    non-empty — sorted, all of them, so the caller's refusal cannot steer the
    arbiter toward one arbitrary alternative.
    """
    tokens = evidence_tokens(registry)
    if len(tokens) <= 1:
        return []

    mentioned = mentioned_services(spec_text, tokens)
    if not mentioned:
        return []
    if service in mentioned:
        return []
    return sorted(mentioned)


def mismatch_remedy(service: str, mentioned: list[str], registry) -> str:
    """The refusal text handed back to the arbiter, remedy-first.

    Names all the evidence, quotes the chosen repo's description, and states
    that an identical retry will be accepted — the retry should be a
    re-derivation from the spec, not obedience to this message.
    """
    description = ""
    for project in registry.values():
        if project.services and service in project.services:
            description = project.description
            break

    described = f"'{service}'"
    if description:
        described = f"'{service}' (described as: {description})"

    return (
        f"The spec never mentions {described}, but it does mention: "
        f"{', '.join(mentioned)}. Re-read the spec and choose the service its "
        f"own text supports — repo names and file paths in the spec outweigh "
        f"name similarity. If '{service}' is genuinely the right target, call "
        f"create_task again with the same service and it will be accepted."
    )
