# Runbook: document-service — e-sign envelope generation degraded

**Service:** document-service
**Owning team:** Onboarding On-Call (onboarding-oncall@example.com)
**Depends on:** merchant-db
**Upstream consumers:** onboarding-api
**Severity guidance:** P1 if e-sign is fully down (blocks onboarding-api); P2 for elevated latency.

## Symptoms
- onboarding-api reports HTTP 502 from document-service at the e-sign step.
- `document_service_envelope_latency_p99` above 2s; `document_service_5xx_total` rising.
- PDF/e-sign envelopes time out or fail to render.

## Likely root causes
1. **PDF render worker saturation** — envelope generation is CPU-bound; a burst of new merchants saturates the render pool and envelopes queue. Most common cause.
2. **merchant-db read failure** — document-service resolves merchant metadata from merchant-db; a slow query or lock there stalls envelope generation.
3. **Template cache miss after deploy** — a deploy invalidated the agreement-template cache, so the first requests pay a cold render cost.

## Diagnosis
1. Check the render-pool queue depth and CPU; sustained saturation points to cause 1.
2. Check merchant-db latency/locks (see the merchant-db runbook) for cause 2.
3. Correlate with a recent template/deploy change for cause 3.

## Resolution
1. If render saturation: scale the render workers and shed retries; page Onboarding On-Call.
2. If merchant-db: resolve the contention there, then document-service recovers.
3. If cache cold-start: warm the template cache; latency normalizes.

## Verification
- onboarding-api e-sign step succeeds; `document_service_envelope_latency_p99` back under 500ms.
