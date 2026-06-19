# Runbook: auth-service — login latency above SLO / session failures

**Service:** auth-service
**Owning team:** Identity On-Call (identity-oncall@example.com)
**Depends on:** user-db
**Severity guidance:** P1 if logins are failing platform-wide; P2 if login latency is elevated but succeeding.

## Symptoms
- `auth_login_latency_p99_seconds` above the 1s SLO; users report slow or failed logins.
- auth-service logs show `user_db_timeout` or connection-pool exhaustion (`pool_wait_timeout`).
- Mobile clients report session cookies not refreshing.

## Likely root causes
1. **user-db read-replica lag or failover** — auth-service validates credentials and sessions against user-db; a replica failover or lag stalls auth calls. Most common cause of a sudden latency jump.
2. **Connection-pool exhaustion** — a traffic spike or a slow query holds connections, so new login requests queue behind the pool.
3. **Token-signing key rotation** — if the session-signing key was rotated without warming the cache, the first requests after rotation pay a cold-start cost and sessions fail to refresh.

## Diagnosis
1. Check `user_db_replica_lag_seconds` and recent failover events; lag or a recent failover points to cause 1.
2. Inspect the auth-service connection-pool metrics (`auth_pool_active`, `auth_pool_wait_timeout`); sustained waits point to cause 2.
3. Correlate the latency jump with any key-rotation deploy in the change log for cause 3.

## Resolution
1. If user-db replica issue: route auth reads to the primary until the replica is healthy, then revert; page Identity On-Call.
2. If pool exhaustion: raise the pool size, kill the slow query holding connections, and shed non-critical traffic.
3. If key rotation: warm the signing-key cache and re-issue sessions; latency normalizes once the cache is hot.

## Verification
- `auth_login_latency_p99_seconds` back under the 1s SLO.
- No `user_db_timeout` markers for 15 minutes and mobile session refresh works.
