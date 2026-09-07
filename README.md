# Share Manager

Share Manager is a Dockerised subscription, payment and entitlement manager. Plex is the first entitlement integration; the core model is intentionally integration-agnostic.

## v0.2.0 highlights

- Package and billing-tier management, including per-tier grace periods.
- Customer subscriptions with explicit start/current-period dates.
- Manual payment ledger with payment-received date and coverage attribution.
- Renewal semantics:
  - first/fully-lapsed payment starts coverage on the payment date;
  - early/on-time payments extend from the existing expiry;
  - payments during grace also extend from the previous expiry, so grace does not create free days.
- Automatic `active -> grace -> suspended` billing state transitions.
- Applied payment automatically returns an overdue subscription to `active` and reconciles Plex access.
- Background billing check every 15 minutes by default, plus a manual **Run billing check** button.
- Existing v0.1 subscriptions with no expiry remain untouched until their billing dates are initialised.
- Existing Plex package/reconcile/exempt behavior retained.

## One-click Docker / Portainer

The stack contains the app and PostgreSQL. Persistent data lives in named Docker volumes.

Required environment values:

```text
DB_PASSWORD=<strong-random-value>
APP_SECRET=<strong-random-value>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<strong-password>
RECONCILE_ON_ASSIGN=true
BILLING_CHECK_INTERVAL_MINUTES=15
```

The container listens on port `8080`; map any free host port to it, e.g. `8088:8080`.

## Moving existing v0.1 users onto billing

Upgrading does **not** immediately suspend existing users. Old subscription rows have no `current_period_end`, so the billing engine skips them.

For each existing customer, open **Edit billing** and set the current period start and optionally the paid-through date. If paid-through is blank, Share Manager calculates it from the customer's tier. Once those dates exist, automated billing starts managing that subscription.

## Recording payments

An applied payment creates an immutable payment history row and advances the customer's current subscription by one billing interval. Untick **Apply to current subscription** when entering historical ledger data that should not alter current entitlement dates.

Payment-provider integrations can later feed this same payment model without changing the subscription engine.
