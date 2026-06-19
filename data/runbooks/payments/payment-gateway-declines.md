# Runbook: payment-gateway — elevated transaction declines / processor timeouts

**Service:** payment-gateway
**Owning team:** Payments On-Call (payments-oncall@example.com)
**Depends on:** network-gateway, merchant-db
**Severity guidance:** P1 if the global decline rate exceeds 5% or a processor is fully unreachable; P2 for a single-region or single-processor degradation.

## Symptoms
- Spike in `payment_declines_total` and customer reports of failed card payments.
- payment-gateway logs show `processor_timeout` or HTTP 504 from the upstream card processor via network-gateway.
- Settlement reconciliation lag; `payment_gateway_p99_latency_seconds` above 3s.

## Likely root causes
1. **network-gateway egress saturation** — payment-gateway reaches the external card processor through network-gateway; if egress is saturated or a NAT rule changed, processor calls time out. Most common cause of a sudden decline spike.
2. **Card processor incident** — the upstream processor (third party) is degraded; declines and 5xx come straight back through network-gateway.
3. **merchant-db contention** — payment-gateway reads merchant risk config from merchant-db; a long-running query or lock can stall authorization and surface as a timeout.

## Diagnosis
1. Split the decline rate by `processor` and `region` in Grafana to tell a global processor incident (cause 2) from a network egress issue (cause 1).
2. Check network-gateway egress metrics (`network_gateway_egress_saturation`) and recent config changes.
3. Check merchant-db slow-query log and active locks if latency is high but declines are not processor-coded.

## Resolution
1. If network-gateway egress is saturated: page Payments On-Call and Platform On-Call, raise egress capacity, and roll back any recent NAT/firewall change.
2. If the processor is down: fail over to the secondary processor route if available, and post status; declines will subside once traffic shifts.
3. If merchant-db contention: kill the offending query, add the missing index, and retry.

## Verification
- `payment_declines_total` returns to baseline (under 1%).
- payment-gateway p99 back under 800ms and settlement reconciliation catches up.
