# ADR-20260831: Gate operational channel identity hardcodes

**Status:** Accepted  
**Date:** 2026-08-31

## Context

Operational code had accumulated channel-specific defaults, legacy account
maps, deployment paths, and imports of production channel profiles. These
values make a new channel unsafe to introduce and can silently select the
wrong account or database when a script is run from another worktree.

Tests, fixtures, historical incident notes, and the permanently disabled
shorts-link backfill are intentionally not production routing code.

## Decision

Operational identity is resolved at runtime:

- channels and account/project ownership come from the database/runtime
  context;
- paths come from `config.settings` or `scripts.runtime_context`;
- the one-time legacy account migration accepts the explicit
  `LEGACY_CHANNEL_GOOGLE_ACCOUNTS` JSON environment value and has no embedded
  slug map;
- callers must provide a channel to the orchestrator and test configuration
  inherits shared defaults, not a production channel profile.

`scripts/check_operational_hardcodes.py` is the reproducible static gate. It
rejects concrete channel literals, numeric channel identity comparisons,
legacy config imports, slug maps, deployment paths, and channel-specific
defaults. A whitelist is permitted only with an adjacent annotation such as:

```python
# hardcode-gate: allow channel_literal migration
```

## Trade-offs

Runtime resolution adds a dependency on valid DB/config state and makes some
diagnostic scripts less convenient to run standalone. This is preferable to
silently operating on channel 1 or a developer's production filesystem.
Explicit migration configuration preserves recoverability without recreating
the permanent map the gate is designed to prevent.

## Consequences

- New channels do not require code edits in operational modules.
- CI/local tests fail before a new channel-specific default is merged.
- Historical fixtures remain readable and are not rewritten as fake runtime
  configuration.
- Operators must set `DATABASE_PATH`/the documented migration environment
  value when running against a non-default environment.
