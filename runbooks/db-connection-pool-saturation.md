---
id: db-connection-pool-saturation
title: Primary Database Connection Pool Saturation
tags: [database, connection-pool, capacity, saturation, postgres, oracle]
---

## Symptom signature

`db_connection_pool_utilization_pct` climbs gradually and plateaus near
its ceiling over a window of two to six hours, rather than spiking and
recovering immediately. The climb typically has an exponential-looking
shape: fast at first, then flattening as the pool approaches its
configured maximum.

## Likely root causes, in order of frequency

1. **Connection leak in a recently deployed job.** A code path that
   opens a connection but does not release it on an exception path will
   slowly starve the pool. Check deploy timestamps against the start of
   the saturation window first.
2. **A small number of long-running or blocked transactions** holding
   connections open well past their normal duration (lock waits,
   accidental full-table scans, a runaway analytical query sharing the
   OLTP pool).
3. **Genuine organic growth** in concurrent job count outpacing the
   configured pool size, most visible during backfills or reprocessing
   windows.

## Recommended triage steps

1. Query the database's active-session view for sessions older than the
   99th percentile of normal transaction duration.
2. Correlate the saturation window with the deployment log for any
   service that talks to this database.
3. If a leak is confirmed, roll back the offending deploy or restart the
   affected pods to release stuck connections, then raise the pool size
   only as a temporary mitigation, not a permanent fix.
4. If saturation recurs weekly at a predictable time, treat it as a
   capacity-planning item rather than an incident.

## Why this matters

Left unaddressed, pool saturation eventually surfaces as connection
timeouts across every service sharing that database, which is a much
noisier and more disruptive failure mode than the gradual precursor
this runbook is written to catch early.
