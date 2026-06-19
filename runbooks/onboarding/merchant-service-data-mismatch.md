# Runbook: merchant-service — merchant data displaying incorrectly

**Service:** merchant-service
**Owning team:** Onboarding On-Call (onboarding-oncall@example.com)
**Depends on:** merchant-db, user-db
**Severity guidance:** P2; P1 if incorrect data affects payments routing.

## Symptoms
- Merchant records show stale or wrong fields in the onboarding UI.
- merchant-service logs show cache/DB divergence (`stale_merchant_cache`).
- Affected after a new-merchant upload or a config change.

## Likely root causes
1. **Cache/DB divergence** — merchant-service caches merchant config; after an update the cache can serve old values until invalidated. Most common cause.
2. **user-db replica lag** — owner/role fields come from user-db; replica lag shows stale ownership.
3. **Partial write** — an interrupted merchant upload left the record half-written.

## Diagnosis
1. Compare the cached value against merchant-db of record; divergence points to cause 1.
2. Check `user_db_replica_lag_seconds` for cause 2.
3. Inspect the upload job for partial-failure markers for cause 3.

## Resolution
1. If cache divergence: invalidate the merchant's cache entry and re-fetch; data corrects immediately.
2. If replica lag: see the user-db runbook; route reads to primary until caught up.
3. If a partial write: re-run the merchant upload idempotently.

## Verification
- The merchant record renders correct fields; no `stale_merchant_cache` markers for 15 minutes.
