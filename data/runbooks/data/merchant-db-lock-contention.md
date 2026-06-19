# Runbook: merchant-db — lock contention

**Service:** merchant-db
**Owning team:** Payments On-Call (payments-oncall@example.com)
**Severity guidance:** P2.

## Checklist
1. Find blocking sessions (`SHOW PROCESSLIST` / pg_locks).
2. Identify the long-held lock and its query.
3. Confirm impact on payment-gateway and merchant-service reads.
4. Kill the blocking session if it is a runaway query.
5. Add the missing index or batch the offending write.
6. Confirm payment-gateway p99 recovers.

## Notes
- Common trigger: a bulk merchant-config update without batching.
