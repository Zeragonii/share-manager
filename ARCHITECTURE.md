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

## Complimentary subscription credits (v0.2.5)

`subscription_credits` records non-cash entitlement extensions separately from `payments`. A credit belongs to a customer and subscription and stores the number of tier billing periods granted, coverage start/end, reason, grant timestamp, and actor. This preserves financial reporting while allowing grandfathered or goodwill access to use the same renewal/grace semantics as paid coverage.


### v0.2.5
Customer cards now resolve their subscription centrally in Python instead of duplicating subscription-state filtering in the template. Complimentary access can also reactivate a customer on their most recent historical tier, so the Grant control is available for any customer with subscription history, not only customers whose current row happens to be in a specific live state.


### Manual entitlement override

`subscriptions.manual_access_end` is an optional hard access cutoff. It is intentionally separate from `current_period_end`: billing history remains truthful while operators retain fine-grained entitlement control. When present, automatic status calculation ignores grace and returns `active` before the override timestamp and `suspended` at/after it.

## v0.2.8 UI filtering

Customer status filtering and text search are intentionally client-side because the full customer collection is already rendered for management actions. Payment customer selection uses a searchable client-side picker while submitting the canonical numeric customer ID to the existing payment endpoint; no billing or persistence semantics changed in this release.
