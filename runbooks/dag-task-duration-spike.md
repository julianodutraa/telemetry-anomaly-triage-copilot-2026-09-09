---
id: dag-task-duration-spike
title: Sudden Transient Spike in DAG Task Duration
tags: [orchestration, dag, scheduler, task-duration, transient-spike, kubernetes]
---

## Symptom signature

`dag_task_duration_seconds` jumps well above baseline for a short,
bounded window (typically under a day) and then returns to normal on
its own, without a corresponding change in data volume or job logic.
This is the classic shape of infrastructure-level interference rather
than a data or code regression.

## Likely root causes, in order of frequency

1. **Node-level noisy-neighbor contention.** Another workload scheduled
   onto the same node is competing for CPU or I/O, slowing every task
   that lands there during the window.
2. **Pod preemption or eviction and retry.** The task pod was evicted
   (resource pressure, node scale-down) and the orchestrator's retry
   added the eviction-to-retry gap on top of normal runtime.
3. **A brief upstream API or dependency slowdown** that the task waits
   on synchronously, without a data-volume increase to explain it.

## Recommended triage steps

1. Pull node events for the window: look for `Evicted`, `OOMKilled`, or
   scheduler pressure events on the nodes that ran the slow tasks.
2. Compare task duration against input row/record counts for the same
   run; if duration is up but volume is flat, this points away from a
   data-driven cause.
3. Check the latency dashboard of any external API or service the task
   calls synchronously.
4. If the spike is isolated to one node, cordon it for inspection rather
   than restarting the whole node pool.

## Why this matters

Because the symptom self-resolves, it is tempting to close it without
a cause. Repeated unexplained transient spikes are usually an early
signal of a node pool nearing its resource limits, well before that
shows up as a hard failure.
