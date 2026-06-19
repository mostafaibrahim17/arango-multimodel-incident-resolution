# Runbook: host-infra — disk pressure

**Service:** host-infra
**Owning team:** Platform On-Call (platform-oncall@example.com)
**Severity guidance:** P2; P1 if a node is at risk of eviction.

## Checklist
1. Identify the node firing `node_filesystem_avail_bytes` below 10%.
2. Confirm which mount is full (`df -h`).
3. Clear rotated logs older than 7 days.
4. Prune dangling container images and stopped containers.
5. If still high, expand the volume or cordon and drain the node.
6. Re-check the metric; confirm it is back above 30%.

## Notes
- Most disk-pressure pages are log accumulation or image churn.
- Do not delete application data volumes.
