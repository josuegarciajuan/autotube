# ADR-0001: Supervise API background loops conservatively

**Status:** Accepted  
**Date:** 2026-08-25

## Context

The FastAPI process owns multiple scheduling and monitoring loops, while long
video generation runs in independent subprocesses. A crashed loop can silently
stop scheduling; conversely, restarting a task whose `asyncio.to_thread` work
is still running can create duplicate dispatchers and unsafe concurrent work.

## Decision

Run API background loops under a bounded watchdog. The watchdog restarts loops
that exit unexpectedly, with exponential backoff and a rolling restart budget.
It treats a stale heartbeat as an incident, cancels the child, and only starts a
replacement after cancellation completes. If cancellation cannot be confirmed,
it stops supervision rather than creating duplicate work. Subprocess workers
remain outside the watchdog and are reconnected by PID-aware startup recovery.

## Trade-offs

- **Chosen:** bounded in-process supervision; low deployment complexity and no
  second service, at the cost of a loop being disabled after repeated failures.
- **Rejected:** unconditional restart on stale heartbeat; it can duplicate
  thread-backed scheduler operations and violate upload/render concurrency.
- **Rejected:** API-level restart for every loop failure; it is slower and can
  disrupt clients while providing no safer handling for surviving workers.

## Consequences

- Loop failures recover automatically when cancellation is observable.
- Repeated failures become an explicit critical log condition instead of a hot
  restart loop.
- A process-table probe failure no longer marks a surviving worker as dead.
- Operators still need to investigate loops that exceed the restart budget.
