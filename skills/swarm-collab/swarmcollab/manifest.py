"""Manifest validation: pure dict + filesystem checks for a swarm-collab setup.

Mirrors the intent of swarm.py's validate_manifest. Does not import
swarmcollab.model.Run: this must be usable before a run directory exists, on a
plain draft manifest dict.
"""

from pathlib import Path

from swarmcollab import util

REQUIRED_STRINGS = ("name", "repo", "workdir", "baseline_commit", "goal")
ROLE_KEYS = ("strategist", "implementor", "critic")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_bound(container, key, label, errors, *, minimum, inclusive=True):
    value = container.get(key) if isinstance(container, dict) else None
    ok = _is_number(value) and (value >= minimum if inclusive else value > minimum)
    if not ok:
        cmp = ">=" if inclusive else ">"
        errors.append(f"{label} must be a number {cmp} {minimum}")


def _check_int_bound(container, key, label, errors, *, minimum):
    value = container.get(key) if isinstance(container, dict) else None
    if not (_is_int(value) and value >= minimum):
        errors.append(f"{label} must be an integer >= {minimum}")


def validate_manifest(m: dict, draft: Path) -> list:
    """Validate a swarm-collab manifest dict; return error strings (empty = valid).

    `draft` is the directory the manifest was drafted in. The current rule set
    does not need it (unlike swarm.py's version, which checks in-run-dir
    prompts/adapters); it is accepted for interface parity with future callers
    such as the `init` command.
    """
    errors = []

    for key in REQUIRED_STRINGS:
        if not isinstance(m.get(key), str) or not m[key].strip():
            errors.append(f"{key} must be a non-empty string")

    repo = m.get("repo")
    repo_ok = isinstance(repo, str) and bool(repo.strip())
    if repo_ok and not Path(repo).is_dir():
        errors.append(f"repo is not an existing directory: {repo}")
        repo_ok = False

    baseline_commit = m.get("baseline_commit")
    baseline_ok = isinstance(baseline_commit, str) and bool(baseline_commit.strip())

    roles = m.get("roles")
    if not isinstance(roles, dict):
        errors.append("roles must be a dict")
    else:
        for key in ROLE_KEYS:
            _check_int_bound(roles, key, f"roles.{key}", errors, minimum=0)
        if _is_int(roles.get("implementor")) and roles["implementor"] < 1:
            errors.append("roles.implementor must be >= 1")

    budget = m.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget must be a dict")
    else:
        _check_bound(budget, "max_usd", "budget.max_usd", errors, minimum=0, inclusive=False)
        _check_bound(budget, "max_rounds", "budget.max_rounds", errors, minimum=1)
        _check_bound(budget, "max_hours", "budget.max_hours", errors, minimum=0, inclusive=False)
        _check_bound(budget, "round_token_cap", "budget.round_token_cap", errors, minimum=1)
        _check_bound(budget, "agent_token_cap", "budget.agent_token_cap", errors, minimum=1)

    view = m.get("view")
    if not isinstance(view, dict):
        errors.append("view must be a dict")
    else:
        _check_bound(view, "token_ceiling", "view.token_ceiling", errors, minimum=500)
        _check_bound(view, "compact_threshold_entries", "view.compact_threshold_entries", errors, minimum=1)

    messaging = m.get("messaging")
    if not isinstance(messaging, dict):
        errors.append("messaging must be a dict")
    else:
        _check_bound(messaging, "per_turn_quota", "messaging.per_turn_quota", errors, minimum=0)
        _check_bound(messaging, "max_tokens", "messaging.max_tokens", errors, minimum=1)

    ev = m.get("eval")
    if not isinstance(ev, dict):
        errors.append("eval must be a dict")
    else:
        for key in ("proxy_command", "full_command"):
            if not isinstance(ev.get(key), str) or not ev[key].strip():
                errors.append(f"eval.{key} must be a non-empty string")
        _check_bound(ev, "verify_tolerance", "eval.verify_tolerance", errors, minimum=0)

    rounds = m.get("rounds")
    if not isinstance(rounds, dict):
        errors.append("rounds must be a dict")
    else:
        for key in ("strategist_every", "critic_every", "council_every"):
            _check_bound(rounds, key, f"rounds.{key}", errors, minimum=1)

    pilot = m.get("pilot")
    if not isinstance(pilot, dict):
        errors.append("pilot must be a dict")
    else:
        _check_bound(pilot, "rounds", "pilot.rounds", errors, minimum=1)
        pilot_rounds = pilot.get("rounds")
        max_rounds = budget.get("max_rounds") if isinstance(budget, dict) else None
        if _is_number(pilot_rounds) and _is_number(max_rounds) and pilot_rounds > max_rounds:
            errors.append(f"pilot.rounds ({pilot_rounds}) must be <= budget.max_rounds ({max_rounds})")

    locked_files = m.get("locked_files")
    if not isinstance(locked_files, list) or any(not isinstance(f, str) for f in locked_files):
        errors.append("locked_files must be a list of strings")
    elif repo_ok and baseline_ok:
        workdir = m.get("workdir") or "."
        for rel in locked_files:
            path = rel if workdir in ("", ".") else f"{workdir}/{rel}"
            proc = util.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{baseline_commit}:{path}"],
                check=False,
            )
            if proc.returncode != 0:
                errors.append(f"locked_files entry not found at baseline_commit: {rel}")

    strip_env = m.get("strip_env")
    if not isinstance(strip_env, list) or any(not isinstance(s, str) for s in strip_env):
        errors.append("strip_env must be a list of strings")

    return errors
