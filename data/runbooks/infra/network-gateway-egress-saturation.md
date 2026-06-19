# Runbook: network-gateway — egress saturation / upstream timeouts

**Service:** network-gateway
**Owning team:** Platform On-Call (platform-oncall@example.com)
**Downstream consumers:** payment-gateway, notification-service
**Severity guidance:** P1 if multiple downstream services are timing out to external endpoints; P2 for a single consumer.

## Symptoms
- payment-gateway and notification-service report `processor_timeout` / `webhook_timeout` to external endpoints at the same time.
- `network_gateway_egress_saturation` above 90%; `network_gateway_active_connections` near the configured ceiling.
- Increased retransmits and 504s on outbound calls.

## Likely root causes
1. **Connection-pool / NAT exhaustion** — a traffic surge or a leaked connection pool exhausts the egress NAT table, so new outbound connections queue or drop. This is the most common cause when several downstream services fail together.
2. **Recent firewall / NAT rule change** — a config push narrowed an egress allow-list or changed the SNAT pool, silently throttling a subset of destinations.
3. **Noisy-neighbor batch job** — a bulk export or backfill saturates shared egress bandwidth.

## Diagnosis
1. Correlate the timeout spike across downstream services — simultaneous failure across consumers points to network-gateway (cause 1/2) rather than any one service.
2. Inspect the egress connection-table utilization and recent change-log entries for NAT/firewall edits.
3. Check for large outbound transfers from batch workloads in the same window.

## Resolution
1. If pool/NAT exhaustion: raise the SNAT pool / connection ceiling and recycle leaked connections; page Platform On-Call.
2. If a config change: roll back the offending NAT/firewall change; egress recovers immediately.
3. If a noisy batch job: throttle or reschedule it off-peak and add an egress rate limit for batch workloads.

## Verification
- `network_gateway_egress_saturation` back under 60% and downstream timeout rates at baseline.
