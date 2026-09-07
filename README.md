# Share Manager

Share Manager is a Dockerised subscription, payment and entitlement manager. Plex is the first entitlement integration; the core model is intentionally integration-agnostic.

## v0.2.2 highlights

- Package and billing-tier management, including per-tier grace periods.
- Customer subscriptions with explicit start/current-period dates.
- Manual payment ledger with payment-received date, coverage attribution, and multi-period prepayments.
- Payment duration can be auto-calculated from amount ÷ tier price or manually overridden.
- Customer cards are displayed in one vertical column for easier scanning.
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

Required environment values. In Portainer, add these under the stack's **Environment variables** section (the Compose file passes them into the app container):

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

An applied payment creates an immutable payment history row and advances the customer's current subscription by one or more billing periods.

- Leave **Billing periods** blank to calculate automatically from `amount / tier price` (for example, £30 on a £10 monthly tier buys 3 monthly periods).
- Enter **Billing periods** manually to override the calculation for discounts or special arrangements.
- Automatic calculation requires a whole-number multiple of the tier price; otherwise the UI asks for a manual period count rather than guessing.
- Untick **Apply to current subscription** when entering historical ledger data that should not alter current entitlement dates.

Payment-provider integrations can later feed this same payment model without changing the subscription engine.

## Complimentary access

v0.2.5 adds complimentary subscription credits for grandfathered users, donor recognition, goodwill extensions, and other non-cash access grants. On a managed customer card, choose **Grant complimentary access**, enter the number of billing periods and an optional reason. The current billing tier defines the period length (for example, three periods on a monthly tier grants three months; one period on an annual tier grants one year).

Complimentary access is deliberately separate from the payments ledger. Each grant records its period count, coverage start/end, reason, grant date and actor, without creating a fake £0 payment or inflating revenue. Grants made before expiry or during grace extend from the existing expiry; grants made after grace has elapsed start from the grant date. A grant reactivates the subscription and triggers Plex reconciliation when automation is enabled.


### v0.2.5
Customer cards now resolve their subscription centrally in Python instead of duplicating subscription-state filtering in the template. Complimentary access can also reactivate a customer on their most recent historical tier, so the Grant control is available for any customer with subscription history, not only customers whose current row happens to be in a specific live state.


## Manual access end override

Each subscription can optionally have a **Manual access end** date from the customer card's **Edit billing** panel. While set, this is a hard entitlement cutoff and overrides the normal paid-through/grace calculation without altering payment or complimentary-credit history. Clear the field to return the subscription to normal billing enforcement. Saving the override immediately recalculates the customer's state and reconciles Plex when enabled.
