# code-skills

Personal collection of [Claude Code](https://claude.com/claude-code) skills, an agent, an output style, and a
hook. Everything here is generic — no private data, credentials, or project-specific paths.

## Contents

### Skills (`skills/`)

| Skill | What it does |
|-------|--------------|
| **ralph-loop** | Start (or resume) a fresh-context "ralph" loop on a spec or plan, gated by an executable definition of done. `ralph-loop.sh` runs `claude -p` repeatedly — one plan task per iteration, fresh context each time — until `dod.sh` is green or the loop is blocked. |
| **swarm-loop** | Review whether a goal suits a swarm, then initialise and run parallel research lanes — open-ended search over any target with a locked scorer. |
| **swarm-collab** | Run many communicating `claude -p` agents (strategist / implementor / critic) that share a bounded blackboard to search an unplannable build-and-measure problem against a locked scorer. Includes the `swarmcollab` Python package and its test suite. |
| **concise** | Make replies short and literal with ASD-STE100 (Simplified Technical English) rules: short sentences, complete facts, a fixed answer shape, no filler. |
| **python-code-standards** | Bring a Python repo (especially FastAPI/SQLAlchemy/pydantic) into line with a structuring ethos: mechanical rules by packaged checks (ruff, import-linter, greps — nothing written to the target repo), judgement rules by a reviewed, approval-gated refactor plan. Includes an optional `PostToolUse`/`Stop` hook snippet. Requires [`uv`](https://docs.astral.sh/uv/). |

### Agent (`agents/`)

| Agent | What it does |
|-------|--------------|
| **silent-failure-hunter** | Reviews code for silent failures, swallowed errors, bad fallbacks, and missing error propagation. Adapted from [affaan-m/ecc](https://github.com/affaan-m/ecc) (MIT). |

### Output style (`output-styles/`)

| Output style | What it does |
|--------------|--------------|
| **concise** (`Concise STE`) | The `concise` skill rules as an always-on output style. Select it with `/output-style`. |

### Hook (`hooks/`)

| Hook | What it does |
|------|--------------|
| **block-dangerous-git.sh** | `PreToolUse(Bash)` guard that blocks destructive git commands (`push --force`, `reset --hard`, `git clean -f`, `branch -D`, `checkout .`) and git-hook bypasses (`--no-verify`, `core.hooksPath`, `commit -n`). Based on [mattpocock/skills](https://github.com/mattpocock/skills) git-guardrails, extended so agents can't skip pre-commit checks. |

## Install

These live under `~/.claude/`. To use them, copy (or symlink) each directory into place:

```sh
# Skills
cp -R skills/ralph-loop skills/swarm-loop skills/swarm-collab skills/concise skills/python-code-standards ~/.claude/skills/

# Agent
cp agents/silent-failure-hunter.md ~/.claude/agents/

# Output style
mkdir -p ~/.claude/output-styles
cp output-styles/concise.md ~/.claude/output-styles/

# Hook
cp hooks/block-dangerous-git.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/block-dangerous-git.sh
```

Then register the hook in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/block-dangerous-git.sh" }
        ]
      }
    ]
  }
}
```

The `block-dangerous-git.sh` hook requires [`jq`](https://jqlang.github.io/jq/).

## swarm-collab development

```sh
cd skills/swarm-collab
uv venv && uv pip install -e .   # or your preferred setup
python -m pytest
```

## Notes

- `ralph-loop`, `swarm-loop`, and `swarm-collab` are built to work alongside
  [superpowers](https://github.com/obra/superpowers) specs/plans, but the loop mechanics are
  independent of it.
- The worked examples in the MANIFEST files use placeholder repo paths (`/Users/you/dev/...`).
