---
id: stream-consumer-backpressure
title: Stream Consumer Backpressure and Growing Queue Depth
tags: [streaming, queue, backpressure, consumer-lag, kafka, resource-exhaustion]
---

## Symptom signature

`queue_depth_messages` (or consumer lag, if that is the metric your
platform exposes) rises steadily over several hours without a matching
drop back to baseline, indicating the consumer side is falling behind
production on a sustained basis rather than absorbing a brief burst.

## Likely root causes, in order of frequency

1. **Downstream sink slowdown.** The consumer is healthy but the system
   it writes to (a data warehouse, an API, a second queue) has gotten
   slower, so each batch takes longer to commit and the backlog grows.
2. **Under-provisioned consumer group.** Partition count or consumer
   replica count has not kept pace with produced volume, so steady-state
   throughput is below the incoming rate.
3. **Poison messages triggering repeated retries.** A small number of
   malformed records cause retry loops that consume worker capacity
   without making forward progress.

## Recommended triage steps

1. Check the downstream sink's own latency and error-rate dashboards
   first; a consumer that looks "stuck" is very often waiting on a slow
   dependency, not failing on its own.
2. Compare current consumer replica count and per-replica throughput
   against the same time last week to rule out an under-provisioning
   regression.
3. Sample the oldest unprocessed messages for a recurring dead-letter
   pattern.
4. If the backlog is purely a rate mismatch, scale consumers
   horizontally; if it is a downstream slowdown, scaling the consumer
   only makes the downstream problem worse.

## Why this matters

Sustained queue growth compounds: replay volume and end-to-end latency
both increase the longer it runs uncorrected, so this is one of the
few anomaly signatures where response time materially changes the size
of the eventual cleanup.
