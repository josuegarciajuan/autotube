# ADR-20260901: Robust scheduling and delivery admission

## Status

Accepted

## Context

Scheduling previously used one status field for public targets, generation
throughput, and upload admission. Candidate selection was also separate from
the state transition, so two scheduler ticks could dispatch the same slot or
video. Manual pacing exceptions could survive a safety-state change.

## Decision

- Persist state/overrides in `channel_delivery_state`; persist state caps in
  the idempotently seeded `delivery_profiles` table.
- Resolve channel policy centrally; changing state clears manual overrides.
- Expose independent public target, generation capacity, and upload capacity
  fields in planning configuration.
- Admit planned slots and upload videos with conditional SQLite transactions.
- Route scheduled uploads through a single publication-policy boundary that
  forces private visibility with a future `publish_at`.
- Store new claims in UTC and retain existing compatibility fallbacks for old
  naive scheduling data.

## Trade-offs

SQLite `BEGIN IMMEDIATE` briefly serializes claim writers, but avoids duplicate
dispatch without adding infrastructure. Upload capacity can exceed the public
target so private warm-up backlog can be prepared; the publication cap remains
the safety control. Existing config keys remain supported, at the cost of a
small migration/compatibility surface.

## Consequences

Positive: duplicate dispatch is rejected atomically, state changes are
auditable and predictable, and factory throughput can be tuned independently
from public cadence. Negative: stale legacy rows still require operational
replanning, and an upload worker that dies after claiming relies on existing
recovery scans.

## Operations

The normal startup migration runs through `migrate_v2()` and installs v51.
After deployment, run a dry-run/full replan from the Scheduling UI or:

```bash
python3 -c "from database.db_extended import migrate_v2; migrate_v2()"
curl -X POST http://localhost:8000/api/planning/full-replan/preflight
```

Only apply the returned confirmation token after reviewing its impact.
