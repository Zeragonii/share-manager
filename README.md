# Share Manager

Share Manager is a Dockerised subscription, payment and entitlement manager. Plex is the first entitlement integration; the core model is intentionally integration-agnostic.

## v0.5.2

Failed Plex updates now survive restarts and retry automatically using the customer's current access rules. Payment coverage edits and voids refresh only the affected customer's billing, preserving other customers' scheduled access updates. See [release notes](RELEASE_NOTES.md) for upgrade details, retry timing, and regression tests.

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


## v0.3.x notifications

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

`NOTIFICATION_DUE_SOON_DAYS` is now the default reminder offset used when creating/migrating notification destinations. In v0.3.1 each destination has its own **Due reminder days** schedule, for example `7,3,1,0` (seven, three and one day before renewal plus the due date). Reminder deliveries are de-duplicated per destination, subscription, expiry date and threshold, so the billing worker does not repeat the same reminder.

### Home Assistant

The Home Assistant adapter intentionally mirrors Uptime Kuma's native Home Assistant notifier. Configure the Home Assistant base URL and a long-lived access token. Share Manager POSTs to:

```text
<HA URL>/api/services/notify/<notification action>
```

The optional **Notification action** is the service name without the `notify.` prefix, for example `mobile_app_pixel_9`. If left blank, Share Manager uses `notify`, matching Kuma's default behaviour. The payload includes `title`, `message`, and an additional `data` object containing the Share Manager event name and severity.

### Generic webhook payload

Generic endpoints receive JSON containing `event`, `severity`, `title`, `message`, `data`, and `sent_at`. Discord endpoints use the standard Discord webhook endpoint. Webhook URLs and HA tokens should be treated as secrets; Share Manager masks webhook URLs in the UI and does not persist secret-bearing exception URLs in delivery errors.


## v0.3.1 staged notification rules

Each notification destination can independently configure renewal reminder offsets. Example:

```text
Home Assistant: 3,1,0
Discord:        7,3,1,0
```

`0` means the paid-through date itself. Grace and suspension alerts remain state-transition events: if those events are enabled for a destination, Share Manager sends them when the customer actually enters grace or becomes suspended. This provides a staged sequence such as 3-day warning → grace alert → suspension alert without repeated notifications every billing cycle.

The legacy `NOTIFICATION_DUE_SOON_DAYS` environment setting is retained as the default for new notification destinations and is copied into existing destinations during the v0.3.0 → v0.3.1 schema upgrade.

## New customer onboarding (v0.3.2)

The Customers page includes **Invite new Plex customer** for people who have never been synced before. The onboarding flow creates the customer and first subscription period, then reconciles the selected package immediately. If no accepted Plex share exists, Share Manager sends an invitation containing the package's mapped Plex libraries.

Fields include the customer name, contact email, Plex username/account email, billing tier, subscription start date, and notes. If the Plex identity field is blank, the contact email is used as the Plex invitation target. If both are supplied, the explicit Plex identity wins.

If the Plex invitation fails, the customer and subscription are retained and the error is shown in the UI. Correct the Plex identity if needed and use **Reconcile Plex** to retry. Packages without an enabled Plex library mapping are not offered in the onboarding selector.


## v0.3.3
- Added a mobile-friendly responsive UI with an off-canvas navigation drawer, improved phone/tablet spacing, touch-friendly stacked controls, and horizontal-scroll wrappers for wide tables without disrupting the desktop layout.


## v0.3.4
- Refined the mobile navigation into a dedicated top bar so the menu control no longer overlaps drawer branding.
- Added mobile-only compact customer summaries showing name, package/tier and status; tapping a summary expands the existing full customer controls. Desktop customer tiles are unchanged.


## v0.3.5
- Added a forward-looking Dashboard revenue forecast based on the current Active/Grace subscription distribution. It shows monthly-tier revenue, yearly-tier revenue, annualised total, and per-package breakdowns while excluding exempt/suspended/cancelled customers.


## v0.3.6
- Fixed a desktop Customers-page layout regression so each customer tile again behaves as a top/bottom flex layout, keeping summary content aligned at the top and action controls pinned to the bottom of equal-height cards.


## v0.3.7
- Added client-side customer sorting by effective access expiry, name, or status. Effective expiry prefers an active manual-access-until date, then grace-until, then paid-through; customers without a date sort last.


## v0.3.8
- Fixed the mobile hamburger icon so its bars render vertically.
- Tightened the mobile Dashboard with a two-column headline-stat grid and smaller stat cards.
- Added a native mobile revenue-package distribution layout, removing the need to horizontally scroll the desktop revenue table on phones.


## v0.3.9
- Added a client-side Package filter to the Customers page. Package filtering composes with status, text search and sorting, and the package list is generated from currently displayed customer assignments.


## v0.3.10
- Added an Edit customer modal beside Reconcile Plex. Friendly name, contact email, Plex username/email and notes can be updated without changing customer IDs, subscription history, payment history or package assignments. Changing Plex identity clears the cached numeric Plex user ID so future reconciliation safely resolves the new account.


## v0.4.0 — Disaster recovery

Share Manager 0.4 adds persistent scheduled backups and a restore pipeline.

### Backup schedule

The app checks for a missing daily automatic backup and creates one after the configured UTC hour. If the app was offline at the scheduled hour, it catches up after the next start instead of silently skipping the day.

Portainer/environment settings:

```env
BACKUP_HOST_PATH=/path/on/independent/storage/share-manager
```

`BACKUP_HOST_PATH` is mounted at `/backups` inside the app container. For genuine disaster recovery, put this on storage independent from the PostgreSQL volume (for example an Unraid/NFS location or another physical disk/server). The backup schedule and retention policy are configured from **Disaster Recovery → Backup automation** in the web UI.

Automatic retention uses one set of dump files and keeps the union of:
- the newest N daily restore points;
- one representative restore point from each of the newest N ISO weeks;
- one representative restore point from each of the newest N calendar months.

Manual backups and automatic pre-restore safety backups are not pruned automatically.

### Restore pipeline

The Backups page can restore either a stored backup or an uploaded `.dump`. A PostgreSQL dump is first validated with `pg_restore --list`. Immediately before the destructive restore, Share Manager creates a fresh safety backup of the current database. PostgreSQL sessions are disconnected and the dump is restored with `--clean --if-exists --no-owner --no-privileges --exit-on-error`.

After a successful restore, restart the Share Manager app container so all workers and connection pools start cleanly against the restored database.

The database backup does **not** contain Portainer environment variables, passwords, secrets, or the stack definition. Back up that deployment configuration separately.


## v0.4.1 — in-app backup automation settings
- Backup schedule, scheduler check interval, scheduled-backup enable/disable, and daily/weekly/monthly retention are now stored in PostgreSQL and editable on the Disaster Recovery page.
- Existing `BACKUP_SCHEDULE_HOUR`, `BACKUP_CHECK_INTERVAL_MINUTES`, and retention environment variables are retained only as first-run defaults for compatibility.
- `BACKUP_HOST_PATH` remains a deployment/Compose setting because Docker must mount the host/NAS path before the application starts.
- Scheduler settings are re-read at runtime; changing the policy does not require an app restart (the check interval itself updates after the current sleep finishes).


## v0.4.2
- Polished the Disaster Recovery automation UI by grouping schedule and retention settings into aligned rows with consistent control heights and helper text, while keeping the existing backup behaviour unchanged.

## v0.5.0 — Tautulli customer intelligence
- Connect one Tautulli instance from Integrations using URL + API key.
- Cached historical sync for user matching, last streamed, latest title, 30-day plays/watch time, and lifetime plays/watch time.
- Configurable historical sync interval (5 minutes to 24 hours) and live refresh interval (10/15/30/60 seconds).
- Live `get_activity` heartbeat is served through Share Manager with a short shared server-side cache; browsers never call Tautulli directly.
- Customer cards show usage summaries and update to **Watching now** asynchronously without a page reload.
- Customer Activity filter: watching now, active 7/30 days, inactive 30/90 days, never streamed.
- Customer sorting adds last streamed / most active / least active.
- Dashboard Plex activity summary and live sessions panel.
- Customer History gains a detailed Plex activity panel.
- Notification events for sync failure, unmatched users, 90+ day inactivity, never-streamed customers, and suspended customers streaming.
- Tautulli is advisory only: usage never changes billing or entitlement state automatically.

### Tautulli setup
Open **Integrations → Tautulli** and enter the URL Share Manager can reach (for example `http://tautulli:8181` on a shared Docker network, or the Tautulli host/LAN URL) plus the Tautulli API key. Save, use **Test connection**, then **Sync now** for the initial customer match. No additional Docker environment variables are required.

Matching prefers the stored Plex numeric user ID and falls back to Plex username/email. Historical analytics are cached in PostgreSQL; the UI never waits for Tautulli during normal page loads. Live activity is fetched through Share Manager's `/api/tautulli/live` endpoint and is server-cached so multiple open browsers share one lightweight Tautulli `get_activity` sample per refresh interval.


## v0.5.1
- Moved the customer-card Tautulli usage/live activity summary out of the upper identity/billing content and into the package/control area so it stays visually anchored directly above the package selector. No Tautulli sync or heartbeat logic changed.

## v0.5.3 correctness and recovery hardening

v0.5.3 hardens the v0.5.2 review changes. Backup storage errors (including stale NFS/CIFS file handles) are shown cleanly in the Disaster Recovery UI; PostgreSQL restores are transactional and run in maintenance mode with post-restore schema verification; payment voids restore captured pre-payment state; pending Plex invitations follow current entitlement state; admin sessions expire server-side and are invalidated when credentials change; and release CI runs pytest before publishing.

For HTTPS deployments, set `SESSION_COOKIE_SECURE=true`. The backup mount itself is still controlled by Docker/Portainer through `BACKUP_HOST_PATH`; if `/backups` reports a stale file handle, repair/remount the host storage and recreate/restart the app container before relying on backups again.


## v0.5.4 — PostgreSQL-only hardening

Share Manager now requires PostgreSQL at runtime. `DATABASE_URL` has no SQLite fallback and startup fails clearly if it is missing, malformed, or points to a non-PostgreSQL backend. Disaster Recovery accepts only PostgreSQL custom-format `.dump` files and uses `pg_dump`/`pg_restore` exclusively. SQLite may still appear in unit-test fixtures as a fast isolated SQLAlchemy test backend; it is not a supported deployment/runtime database.

## v0.5.5
- Desktop navigation sidebar now stays anchored to the viewport while main page content scrolls. Mobile off-canvas navigation is unchanged.
