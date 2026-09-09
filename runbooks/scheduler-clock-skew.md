---
id: scheduler-clock-skew
title: Scheduler Node Clock Skew Producing Misleading Duration Metrics
tags: [scheduler, clock-skew, ntp, dag, task-duration, false-signal]
---

## Symptom signature

Reported task durations look anomalous (either implausibly long or
briefly negative before clamping) in a way that does not track any
real change in workload, and the anomaly is confined to tasks scheduled
on one specific node or node pool.

## Likely root causes, in order of frequency

1. **NTP drift on a single scheduler or worker node**, so start and end
   timestamps are computed against a clock that is out of sync with the
   rest of the fleet, corrupting the derived duration metric itself
   rather than reflecting real slowness.
2. **A recent node image or container runtime change** that altered how
   wall-clock time is read inside the container versus the host.

## Recommended triage steps

1. Check NTP sync status and clock offset on the specific node(s)
   involved before investigating the workload at all.
2. Compare wall-clock duration against a monotonic-clock duration for
   the same tasks if both are available; a mismatch confirms a clock
   issue rather than a real performance regression.
3. Cordon and reboot the affected node once NTP is restored; no
   workload-level remediation is needed.

## Why this matters

This is worth ruling out early because it wastes significant
investigation time on a "performance regression" that is really an
instrumentation artifact confined to one node, not the workload the
alert appears to implicate.
