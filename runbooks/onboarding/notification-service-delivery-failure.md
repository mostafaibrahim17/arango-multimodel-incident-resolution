# Runbook: notification-service — delivery failures

**Service:** notification-service
**Owning team:** Platform On-Call (platform-oncall@example.com)
**Depends on:** network-gateway
**Severity guidance:** P3; P2 if onboarding confirmations are blocked.

## Checklist
1. Check `notification_delivery_failed_total` by channel (email/SMS/webhook).
2. If all channels fail, suspect network-gateway egress (see that runbook).
3. If one channel fails, check that provider's status and credentials.
4. Drain and replay the dead-letter queue once delivery recovers.
5. Confirm onboarding confirmation messages are arriving.

## Notes
- Most single-channel failures are expired provider credentials.
