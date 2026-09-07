# Share Manager

Share Manager is a Dockerised subscription, payment and entitlement manager. Plex is the first entitlement integration; the core model is intentionally integration-agnostic.

## v0.3.0 highlights

- Package and billing-tier management, including per-tier grace periods.
- Customer subscriptions with explicit start/current-period dates.
- Manual payment ledger with payment-received date, coverage attribution, and multi-period prepayments.
- Payment duration can be auto-calculated from amount ÷ tier price or manually overridden.
- Customer cards retain the responsive tile layout with aligned controls and client-side search/status filtering.
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
NOTIFICATION_DUE_SOON_DAYS=3
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


## Manual access until override

Each subscription can optionally have a **Manual access until** date from the customer card's **Edit billing** modal. While that date is still in the future, the customer is guaranteed active access without altering their payment or complimentary-credit history. When the override date is reached, Share Manager automatically falls back to the normal paid-through and grace calculation. Payments recorded during the override continue to update normal billing coverage, so paid access can carry on afterwards without an administrator clearing the override.

## v0.2.8

- Added instant client-side customer search on the Customers page.
- Added status filters for All, Active, Grace, Suspended, Cancelled and Exempt customers, with live counts.
- Search and status filters can be combined without reloading the page.
- Replaced the long Payments customer dropdown with a searchable customer picker that matches names, email addresses and Plex usernames while retaining billing-period previews.

## v0.2.10 operations

### Payment maintenance
Payments now have an **Edit** action. Amount, receipt date, source, reference and note can be corrected without changing the access period that was already granted. Billing-period count can also be changed, but only when that payment is the latest coverage event on the subscription; Share Manager then recalculates the current coverage end.

**Delete payment** is audit-safe rather than destructive. Ledger-only payments can be voided immediately. An applied payment can only be voided when it is the latest coverage event; Share Manager rolls the subscription back to the preceding payment/complimentary-credit coverage and reruns billing/Plex reconciliation. Voided payments remain visible in history and are excluded from dashboard revenue totals.

### Customer history
Each customer tile now has a **History** button. The timeline combines subscription assignments, payments (including voided ones), complimentary credits and relevant customer/subscription/payment audit events.

### Database backups
The **Backups** page can generate and download a PostgreSQL custom-format dump from the live database. The application image includes PostgreSQL 17 client tools so its `pg_dump` version matches the bundled PostgreSQL 17 service.

A typical restore into the `sharemanager` database is:

```bash
pg_restore --clean --if-exists --no-owner -d sharemanager share-manager-YYYYMMDD-HHMMSS.dump
```

Stop the application container while restoring. The database backup contains Share Manager data, not your Portainer stack/environment variables, so keep a copy of those separately.


## v0.3.0 notifications

Share Manager can route operational events to **Home Assistant**, **Discord**, or a **generic JSON webhook**. Notification integrations are configured under **Integrations → Notifications** and support per-endpoint event selection, a minimum severity filter, enable/disable controls, editing, deletion, and a **Send test** action. The page also shows the most recent delivery results.

Supported events in v0.3.0 are:

- `payment.received` (info)
- `customer.entered_grace` (warning)
- `customer.suspended` (critical)
- `customer.reactivated` (info)
- `subscription.due_soon` (warning)
- `plex.invite_sent` (info)
- `plex.reconcile_failed` (critical)
- `backup.created` (info)

`NOTIFICATION_DUE_SOON_DAYS` defaults to `3`. Set it to `0` to disable due-soon event generation. Due-soon notifications are de-duplicated per endpoint and paid-through date, so the 15-minute billing worker does not repeatedly notify for the same renewal.

### Home Assistant

The Home Assistant adapter intentionally mirrors Uptime Kuma's native Home Assistant notifier. Configure the Home Assistant base URL and a long-lived access token. Share Manager POSTs to:

```text
<HA URL>/api/services/notify/<notification action>
```

The optional **Notification action** is the service name without the `notify.` prefix, for example `mobile_app_pixel_9`. If left blank, Share Manager uses `notify`, matching Kuma's default behaviour. The payload includes `title`, `message`, and an additional `data` object containing the Share Manager event name and severity.

### Generic webhook payload

Generic endpoints receive JSON containing `event`, `severity`, `title`, `message`, `data`, and `sent_at`. Discord endpoints use the standard Discord webhook endpoint. Webhook URLs and HA tokens should be treated as secrets; Share Manager masks webhook URLs in the UI and does not persist secret-bearing exception URLs in delivery errors.
