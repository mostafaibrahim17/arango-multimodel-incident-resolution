# Runbook: api-gateway — elevated 5xx to clients

**Service:** api-gateway
**Owning team:** Frontend On-Call (frontend-oncall@example.com)
**Routes to:** onboarding-api, auth-service
**Severity guidance:** P1 if a large share of client requests 5xx; P2 for a single route.

## Symptoms
- Clients (web-portal) see HTTP 502/503; `api_gateway_5xx_total` spikes.
- Errors concentrate on routes to onboarding-api or auth-service.

## Likely root causes
1. **Upstream service degraded** — api-gateway 5xx usually mirrors a degraded upstream (onboarding-api or auth-service). Identify which route is failing first. Most common cause.
2. **Upstream connection-pool exhaustion at the gateway** — the gateway's per-upstream pool is exhausted, queuing requests until they time out.
3. **Bad deploy / route config** — a recent gateway route change points to an unhealthy target.

## Diagnosis
1. Break `api_gateway_5xx_total` down by upstream route to find the failing service for cause 1.
2. Check the gateway's per-upstream pool saturation for cause 2.
3. Correlate with a recent gateway deploy/route change for cause 3.

## Resolution
1. If an upstream is degraded: follow that service's runbook (onboarding-api / auth-service); the gateway recovers once the upstream does.
2. If pool exhaustion: raise the per-upstream pool and add circuit-breaking.
3. If a bad route: roll back the gateway route change.

## Verification
- `api_gateway_5xx_total` back to baseline; client error rate normal.
