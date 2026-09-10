# Share Manager Architecture — v0.5.2

## v0.5.2 reliable Plex reconciliation

`plex_reconcile_jobs` records outstanding work with a composite customer/integration primary key, an attempt count, next-attempt timestamp, and sanitized error type. The existing startup `create_all` creates the table on upgrade without changing historical billing records.

`reconcile_customer` persists jobs for all selected enabled Plex servers before network calls. Each successful server clears its own job; failures retain work with exponential backoff capped at 60 minutes and do not stop attempts on other servers. The billing cycle retries due jobs even when there are no new status transitions. Explicit actions bypass backoff. The existing single-worker deployment serializes reconciliation calls with an in-process lock.

Retries reload committed customer/subscription/package data instead of storing desired libraries in a job. Disabled integrations wait; exempt or unlinked customers have their work cleared without contacting Plex. A returned invitation/pending result retains its existing successful reconciliation meaning. Outstanding work is durable across restarts, but no historical audit failures are automatically backfilled.

`process_billing` accepts an optional `customer_id` scope. Payment edit/void routes use it for their immediate coverage refresh. The scheduled cycle keeps its global behavior, including notifications and reconciliation for every changed customer.

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


## v0.4 disaster recovery architecture

- `app/services/backups.py` owns database dump creation, validation, restore, filesystem discovery, schedule-due checks, and grandfather/father/son-style retention selection.
- PostgreSQL backups use `pg_dump --format=custom`; validation uses `pg_restore --list`; restores use `pg_restore --clean --if-exists --exit-on-error`.
- `/backups` is a bind-mounted persistence boundary. The host-side path is selected by `BACKUP_HOST_PATH` in Compose.
- The FastAPI lifespan starts a backup scheduler alongside the billing scheduler. It catches up if the configured daily backup is absent after the scheduled UTC hour.
- Automatic retention operates only on `share-manager-auto-*` dumps. Manual and pre-restore safety dumps are intentionally preserved.
- Restore is deliberately two-stage from an operator perspective: validate source, create safety backup, then destructive restore. The UI requires the explicit confirmation token `RESTORE`.
- Backup success/failure and restore completion integrate into the existing notification event system.


## v0.4.1 — in-app backup automation settings
- Backup schedule, scheduler check interval, scheduled-backup enable/disable, and daily/weekly/monthly retention are now stored in PostgreSQL and editable on the Disaster Recovery page.
- Existing `BACKUP_SCHEDULE_HOUR`, `BACKUP_CHECK_INTERVAL_MINUTES`, and retention environment variables are retained only as first-run defaults for compatibility.
- `BACKUP_HOST_PATH` remains a deployment/Compose setting because Docker must mount the host/NAS path before the application starts.
- Scheduler settings are re-read at runtime; changing the policy does not require an app restart (the check interval itself updates after the current sleep finishes).


## v0.4.2
- Polished the Disaster Recovery automation UI by grouping schedule and retention settings into aligned rows with consistent control heights and helper text, while keeping the existing backup behaviour unchanged.

## v0.5.0 Tautulli integration
Tautulli is an observational integration, not an entitlement authority. Historical usage is cached in `tautulli_activity` by a background sync. Live sessions use Tautulli `get_activity` through a short in-process cache exposed by `/api/tautulli/live`; browser heartbeats query Share Manager rather than Tautulli directly. User matching prefers stored Plex numeric user ID, then Plex username/email identity matching. Inactivity can notify administrators but never changes customer subscription status or Plex access.


## v0.5.1
- Moved the customer-card Tautulli usage/live activity summary out of the upper identity/billing content and into the package/control area so it stays visually anchored directly above the package selector. No Tautulli sync or heartbeat logic changed.

## v0.5.3 reliability hardening

- PostgreSQL restore uses a validated custom dump, exclusive application maintenance mode, `--single-transaction`, and post-restore schema verification.
- Background database workers are serialized with restore operations so an already-running cycle completes before restore and no new cycle starts during maintenance.
- Applied payments snapshot prior entitlement coverage so a latest-payment void can restore exact previous state.
- Pending Plex invitations are managed as entitlement state rather than treated as a passive duplicate-invite guard.
- Admin sessions use timed signed tokens and a credential-derived session version.


## v0.5.4 runtime database policy

Production/runtime Share Manager is PostgreSQL-only. Configuration validation rejects non-PostgreSQL `DATABASE_URL` values before the application engine is created. Backup and restore support is PostgreSQL-only (`pg_dump` custom format and transactional `pg_restore`). SQLite usage is limited to isolated unit-test fixtures and is not reachable from deployed application configuration.

## v0.5.5
- Desktop navigation sidebar now stays anchored to the viewport while main page content scrolls. Mobile off-canvas navigation is unchanged.


## v0.5.6 — Customer archiving
- Added reversible customer archiving so stale customers can be removed from the operational Customers view without deleting payments, subscriptions, credits, audit history, or Tautulli activity.
- Customers must be Cancelled before they can be archived, preventing active Plex access from being hidden accidentally.
- Added an Archived customers view with History, Edit, and Restore controls.
- Archived customers are excluded from dashboard operational counts, revenue forecasts, billing processing, payment-entry customer selection, Tautulli matching, and Tautulli dashboard aggregates.

## v0.5.7 — customer bulk operations
Bulk customer changes are handled by `/customers/bulk`. Selected customer IDs are resolved server-side against non-archived records. Status/package changes preserve the existing per-customer subscription state transitions and reconciliation behavior. Archive is non-destructive and only applies to Cancelled, non-exempt customers; ineligible selections are reported as skipped.


## v0.5.8 — bulk action performance
- Bulk status and package changes no longer wait for sequential Plex API calls.
- Bulk manual Reconcile Plex queues durable work and returns immediately.
- Added a dedicated 5-second Plex reconciliation queue worker.
- Queued work stores identities only and recalculates the latest desired entitlement at execution time.
- Fresh operator changes reset any existing retry backoff so they are picked up promptly, while failures continue to use the existing exponential retry policy.
- Bulk archive remains synchronous because it is database-only.


## v0.5.9
- Archived-customer onboarding recovery: Invite new Plex customer now detects archived identity/email matches and offers to restore the existing historical record instead of returning a generic duplicate error.
- Added Restore & reassign package, preserving the customer ID/history while creating a fresh subscription using the originally selected tier/start date and reconciling Plex access.
- The stored archived Plex identity is deliberately preserved; identity changes remain an explicit Edit customer operation.


## v0.6.0 — Concurrent Stream Enforcement
- Billing tiers now define a concurrent stream limit; `0` means unlimited and existing tiers migrate to `1`.
- Tautulli live activity drives server-side enforcement even when no browser is open.
- An over-limit customer must be observed in two distinct live samples before enforcement, preventing transient session flapping from killing playback.
- Newest excess sessions are terminated first; billing-exempt customers remain subject to fair-use stream limits.
- Unmatched/admin Tautulli sessions are never automatically terminated.
- Optional `stream.limit_enforced` and `stream.limit_enforcement_failed` notification events are available through the existing notification adapters.
- Dedicated Stream Limits history and per-customer enforcement history preserve successful and failed termination attempts.

## v0.7.0 — PWA shell
The web UI exposes `/manifest.webmanifest` and a root-scoped `/service-worker.js`. The service worker intentionally does not cache authenticated HTML/API responses; only static assets are cached. Navigation is network-first with `/static/offline.html` as the offline fallback. PWA cache names are release-versioned so new releases discard older static caches. Standard and maskable Android icons plus Apple touch/favicons live under `app/static/icons/`.


## v0.7.1 — Mobile quick navigation
- Added a persistent mobile bottom navigation bar for Dashboard, Customers, and Payments.
- Added current-page highlighting to both the bottom navigation and sidebar drawer.
- Added safe-area-aware bottom spacing so navigation does not cover page controls on installed PWAs or gesture-navigation devices.
- Kept all secondary areas in the existing hamburger drawer.
- Bumped the PWA service-worker cache version so updated navigation/CSS replaces the 0.7.0 shell cleanly.

### v0.7.2 Dashboard presentation
Dashboard mobile rendering remains server-rendered Jinja with progressive disclosure through native `<details>` elements. Historical and live data sources are unchanged; the Tautulli live count updates both desktop and mobile presentation nodes from the same asynchronous heartbeat.


## v0.7.5
- Stream Limits mobile-first UI: compact stats, quick date filters, collapsible filter panel, mobile enforcement cards, clearer success/failure presentation, tappable customers, and no horizontal scrolling.


## v0.7.5a
- Fixed Stream Limits filtering when `All customers` submits an empty `customer_id`; blank values now mean no customer filter instead of triggering FastAPI integer validation.
- Invalid non-numeric customer filter values now redirect safely back to the unfiltered Stream Limits page.


## v0.7.6
- Phone-first Backups and Integrations pass: compact DR status/settings, mobile backup cards, touch-friendly restore controls, denser Plex/Tautulli/notification integration cards, and mobile notification-delivery cards without horizontal scrolling. Desktop behavior remains unchanged.

## v0.8.0 customer portal
Customer portal authentication is intentionally isolated from admin authentication. Each customer may have an optional unique portal username, bcrypt password hash, enable state, session-version integer, and login timestamps. The `sm_portal_session` signed cookie contains only the customer ID and current portal session version. Every portal request re-loads the customer from PostgreSQL and rejects disabled, archived, cancelled, or version-mismatched sessions.

Passwords are never stored reversibly. Enable/reset operations display the submitted/generated temporary password exactly once, then only the hash remains. Cancellation disables portal access and rotates the portal session version. Portal routes are read-only in 0.8.0 and derive the customer exclusively from the authenticated session rather than URL/query customer IDs.


### 0.8.0 build compatibility fix
Pinned `bcrypt==4.0.1` alongside Passlib 1.7.4. Newer bcrypt releases are incompatible with Passlib 1.7.4's backend self-test on Python 3.12 and can fail before hashing otherwise-valid portal passwords.
