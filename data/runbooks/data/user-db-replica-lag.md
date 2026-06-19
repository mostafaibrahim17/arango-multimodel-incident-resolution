# Runbook: user-db — read-replica lag / failover

**Service:** user-db
**Owning team:** Identity On-Call (identity-oncall@example.com)
**Upstream consumers:** auth-service, merchant-service
**Severity guidance:** P1 if a failover is in progress and auth is failing; P2 for lag without failures.

## Symptoms
- `user_db_replica_lag_seconds` rising; auth-service and merchant-service report stale reads or `user_db_timeout`.
- Pagination stalls and "user not found" errors immediately after a write.

## Likely root causes
1. **Long-running query on the primary** — a heavy analytical query or a missing index causes replication to fall behind, so replicas serve stale data to auth-service.
2. **Replica failover** — a primary failover briefly makes reads inconsistent and connections reset.
3. **Write burst** — a bulk user-import job outpaces replica apply.

## Diagnosis
1. Check the primary slow-query log and active locks; a heavy query points to cause 1.
2. Check recent failover events in the user-db cluster for cause 2.
3. Correlate lag with any bulk-import job for cause 3.

## Resolution
1. If a long query: kill it and add the missing index; lag drains.
2. If failover: route critical reads (auth) to the primary until replicas catch up, then revert.
3. If a write burst: throttle the import and let replicas apply.

## Verification
- `user_db_replica_lag_seconds` back under 5s; auth-service `user_db_timeout` markers gone.
