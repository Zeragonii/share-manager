# Share Manager Architecture — v0.2

## Core model

`Customer -> Subscription -> BillingTier -> Package -> PackageEntitlement -> Integration`

Payments are append-only ledger entries. An applied payment can reference a subscription and stores the exact `coverage_start` and `coverage_end` it purchased.

## Billing dates

A subscription stores:

- `started_at`
- `current_period_start`
- `current_period_end`
- `grace_until`
- lifecycle `status`

Each BillingTier defines price, interval/count and `grace_period_days`.

### Renewal rule

If payment is received on/before `grace_until`, the next coverage period begins at the previous `current_period_end`. If payment arrives after grace has elapsed, the next period begins on the payment date. This prevents grace days becoming free paid-service days.

## State engine

For billing-initialised subscriptions:

- before `current_period_end` -> active
- after expiry but before `grace_until` -> grace
- after grace -> suspended

`active` and `grace` retain Plex entitlement. `suspended` removes it. An applied payment reactivates the subscription and the Plex integration reconciles access.

Exempt customers remain outside automatic status/Plex enforcement.

## Upgrade safety

v0.2 performs additive schema migration at container startup. Existing v0.1 subscriptions with a NULL `current_period_end` are intentionally skipped by automatic billing until an administrator initialises their dates.
