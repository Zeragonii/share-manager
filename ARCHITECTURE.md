# Share Manager Architecture — v0.3.0

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

`subscriptions.manual_access_end` is a temporary access guarantee. It is intentionally separate from `current_period_end`: billing history remains truthful while operators retain fine-grained entitlement control. Before the override timestamp the subscription is forced active; after it expires, normal paid-through/grace billing logic resumes automatically.

## v0.2.8 UI filtering

Customer status filtering and text search are intentionally client-side because the full customer collection is already rendered for management actions. Payment customer selection uses a searchable client-side picker while submitting the canonical numeric customer ID to the existing payment endpoint; no billing or persistence semantics changed in this release.

## v0.2.10 operational controls

- Payments support audited edit/void workflows. Coverage-affecting edits/voids are allowed only for the latest entitlement event so later payment/credit history cannot be silently invalidated.
- `payments.voided_at` and `payments.voided_by` preserve deleted-payment provenance; voided rows are excluded from revenue calculations.
- Customer history is assembled from subscription, payment, complimentary-credit and related audit events.
- The application image carries PostgreSQL 17 client utilities and exposes an authenticated UI endpoint that produces custom-format `pg_dump` backups.


## v0.3.0 notification architecture

`NotificationEndpoint` stores notification adapters independently of Plex entitlement integrations. Endpoint kinds are `home_assistant`, `discord`, and `webhook`; each endpoint stores its selected event set, minimum severity, enabled state, destination URL and adapter-specific secret/target fields.

`NotificationDelivery` is an append-only delivery audit containing event, severity, title/message, success state, response code and a sanitized diagnostic. An optional `event_key` provides per-endpoint de-duplication for recurring conditions such as `subscription.due_soon`. Notification delivery failures are recorded but never allowed to roll back billing, payment, backup, or Plex state transitions.

Home Assistant uses its authenticated REST service-call pipeline (`/api/services/notify/<service>`) with a Bearer long-lived access token, matching the Uptime Kuma integration pattern.


## v0.3.1 notification timing

`NotificationEndpoint.due_reminder_days` stores a normalized comma-separated set of calendar-day offsets. Renewal reminder generation is endpoint-specific; each successful threshold delivery receives an event key containing endpoint, subscription, expiry date and day offset, making scheduled checks idempotent. Grace and suspension notifications continue to be emitted by billing-state transitions rather than reminder polling.

## v0.3.2 onboarding

Brand-new Plex users can be created directly from the Customers page. Onboarding persists the customer and subscription first, then runs the normal reconciliation engine. This deliberately reuses the same invitation, entitlement, audit and notification paths as suspension/reactivation rather than introducing a second Plex-sharing implementation. Failed Plex invitations do not roll back the local customer record, allowing correction and retry through normal reconciliation.


## v0.3.3
- Added a mobile-friendly responsive UI with an off-canvas navigation drawer, improved phone/tablet spacing, touch-friendly stacked controls, and horizontal-scroll wrappers for wide tables without disrupting the desktop layout.


## v0.3.4
- Refined the mobile navigation into a dedicated top bar so the menu control no longer overlaps drawer branding.
- Added mobile-only compact customer summaries showing name, package/tier and status; tapping a summary expands the existing full customer controls. Desktop customer tiles are unchanged.


## v0.3.5
- Added a forward-looking Dashboard revenue forecast based on the current Active/Grace subscription distribution. It shows monthly-tier revenue, yearly-tier revenue, annualised total, and per-package breakdowns while excluding exempt/suspended/cancelled customers.


## v0.3.6
- Fixed a desktop Customers-page layout regression so each customer tile again behaves as a top/bottom flex layout, keeping summary content aligned at the top and action controls pinned to the bottom of equal-height cards.
