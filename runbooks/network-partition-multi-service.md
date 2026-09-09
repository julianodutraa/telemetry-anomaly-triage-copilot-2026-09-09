---
id: network-partition-multi-service
title: Network Partition Causing Correlated Multi-Service Degradation
tags: [network, partition, multi-service, correlated-incident, timeout]
---

## Symptom signature

Multiple, otherwise unrelated services degrade at the same moment: task
durations lengthen, queues back up, and database-facing metrics wobble
simultaneously, all starting within the same few-minute window across
different hosts or availability zones.

## Likely root causes, in order of frequency

1. **A network partition or routing change** between availability zones
   or between the cluster and a shared dependency (database, object
   store), which affects every service that crosses that path at once.
2. **A shared load balancer or service mesh control-plane issue**
   introducing latency or dropped connections across many services
   simultaneously.
3. **A DNS resolution failure** for a shared internal endpoint.

## Recommended triage steps

1. Look for a single onset timestamp shared across multiple, otherwise
   independent services; this is the strongest signal that the root
   cause is shared infrastructure rather than any one service's code.
2. Check cloud-provider status pages and internal network change logs
   for the exact window.
3. Rule out a coincidental deploy before escalating to the network team.

## Why this matters

This is the pattern to reach for only when the anomaly genuinely spans
independent services at the same instant; a single metric on a single
service drifting on its own is almost never a network partition and
should be triaged against a service-specific runbook instead.
