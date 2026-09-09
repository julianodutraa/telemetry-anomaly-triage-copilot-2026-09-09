---
id: replication-watcher-stall
title: Replication Watcher Process Stall (False-Healthy Lag Reading)
tags: [database, replication, replica, dependency-stall, monitoring-gap]
---

## Symptom signature

`replication_lag_seconds` collapses toward zero and flatlines there,
in a way that is disconnected from the metric's normal seasonal
pattern, rather than trending down smoothly the way lag does when a
replica genuinely catches up. This is a case where the metric looking
"good" is itself the anomaly.

## Likely root causes, in order of frequency

1. **The lag-reporting agent or watcher process has stopped**, so the
   last known value is being repeated or a default value is being
   emitted instead of a fresh measurement. The replica may or may not
   actually be behind; the metric is no longer telling you.
2. **The replication stream itself has stalled silently** (a stuck
   WAL/binlog reader, a dropped connection to the primary that was not
   detected), and the watcher is reporting zero lag against a stale
   position rather than erroring out.
3. **A monitoring pipeline gap** (the exporter or scrape target for this
   metric changed and is now returning a static or default value).

## Recommended triage steps

1. Check the watcher/exporter process's own liveness and its
   last-successful-scrape timestamp, independent of the value it last reported.
2. Confirm actual replica data freshness directly (e.g. compare a
   recent row's timestamp on primary vs. replica) rather than trusting
   the lag metric during the suspected stall window.
3. Restart the watcher process if it is confirmed hung, and add a
   dead-man's-switch alert (metric-not-reporting, not just
   metric-out-of-range) so this class of failure pages directly next
   time.

## Why this matters

A metric that fails by going quiet rather than by spiking is invisible
to a naive threshold alert on the raw value, which is exactly why it
needs a dedicated signature here rather than being treated as "all
clear."
