# Share Manager Architecture — v0.2.2

## Core model

`Customer -> Subscription -> BillingTier -> Package -> PackageEntitlement -> Integration`

Payments are append-only ledger entries. An applied payment can reference a subscription and stores the exact `coverage_start`, `coverage_end`, and number of `billing_periods` it purchased. The period count can be inferred from amount ÷ tier price or explicitly overridden by an administrator.

## Billing dates

A subscription stores:

- `started_at`
- `current_period_start`
- `current_period_end`
- `grace_until`
- lifecycle `status`

Each BillingTier defines price, interval/count and `grace_period_days`.

### Renewal rule

If payment is received on/before `grace_until`, purchased coverage begins at the previous `current_period_end`. If payment arrives after grace has elapsed, purchased coverage begins on the payment date. One or more complete tier intervals are then added according to the payment's resolved billing-period count. This prevents grace days becoming free paid-service days while supporting prepayment for multiple periods.

## State engine

For billing-initialised subscriptions:

- before `current_period_end` -> active
- after expiry but before `grace_until` -> grace
- after grace -> suspended

`active` and `grace` retain Plex entitlement. `suspended` removes it. An applied payment reactivates the subscription and the Plex integration reconciles access.

Exempt customers remain outside automatic status/Plex enforcement.

## Upgrade safety

v0.2 performs additive schema migration at container startup. Existing v0.1 subscriptions with a NULL `current_period_end` are intentionally skipped by automatic billing until an administrator initialises their dates.
