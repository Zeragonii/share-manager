# 0.10.4a

- Customer news visibility now follows severity: Info, Advisory, and Warning banners display on Account only; Critical banners remain global across the customer portal.
- The live banner refresh endpoint respects the same Account/global scope so scheduled transitions remain correct without a page refresh.
- Scheduling and overlap validation are unchanged.

# 0.10.4 — Scheduled customer news banners

- Adds an admin News page for scheduled portal announcements.
- Banners have title, body, severity, local start/end date-times, and conflict validation.
- Multiple future banners can be queued, but overlapping active windows are rejected.
- Active banners are shown prominently across the customer portal with subtle Info/Advisory/Warning/Critical styling.
- Customer PWAs poll once per minute while visible so scheduled banners can appear/end without a full navigation refresh.
- Admins can end an active banner or cancel a queued banner while preserving audit/history.

## v0.10.3 — Mobile UI consistency pass

- Added a mobile-only set of shared control/layout tokens for consistent sizing and spacing.
- Standardised primary/secondary action height, padding, radius and typography while preserving compact utility controls.
- Standardised input/select/textarea sizing and 16px mobile form text to avoid inconsistent controls and browser zoom behaviour.
- Harmonised mobile card padding/radius, headings, status badges, empty states, action gaps and collapsible section headers.
- Normalised customer and admin bottom-navigation touch areas and label alignment.
- Desktop presentation and application behaviour are unchanged.
- Bumped admin and customer PWA cache versions to 0.10.3.

## v0.10.2 — Requests Platform integration

- Added database-backed Requests Platform settings with enable/disable, friendly name, external URL and button label.
- Added admin configuration under Integrations.
- Added a desktop customer-portal Request sidebar link.
- Added a mobile Account-page Request Content card while preserving the existing four-item bottom navigation.
- Validates configured URLs as HTTP/HTTPS and hides customer links when disabled or unconfigured.
- Bumped admin and customer PWA cache versions to 0.10.2.

# 0.10.1 — Live Ticket Updates

- Added 5-second pseudo-realtime polling to open admin and customer ticket conversations.
- Poll endpoints return only messages newer than the last message already rendered.
- Added live ticket status/priority metadata refresh without replacing the reply composer or wiping drafted text.
- Added a 10-second admin ticket-summary poll so the sidebar active-ticket count stays current across admin pages.
- Added unread emphasis to the live admin ticket badge.
- Polling pauses for hidden tabs/PWAs and resumes immediately on visibility return.
- Customer polling remains portal-session/customer scoped and excludes private internal notes.
- No database schema changes are required.
- Bumped admin and customer PWA caches to 0.10.1.

# 0.10.0b

- Fixed excess blank scroll space below the admin ticket composer on mobile.
- Scoped off-canvas/sticky navigation CSS to `#site-sidebar` so the ticket Workflow/Customer `<aside>` no longer inherits navigation-sidebar viewport sizing.
- Preserved the 0.10.0a ticket-count badge and mobile drawer shadow behavior.
- Bumped admin and customer PWA cache versions to 0.10.0b.

# v0.10.0 — Support Tickets

- Added customer support-ticket creation with category, subject and description.
- Added human-friendly ticket references (`TKT-000001`).
- Added threaded customer/admin replies, per-ticket push subscriptions and deep links.
- Added Open, Reviewed, In Progress, Resolved and Closed workflow states.
- Customer replies automatically reopen Resolved tickets.
- Added admin active queue and separate Closed archive view with status/category/priority/search filters.
- Added Low/Normal/High/Critical priorities, unread markers and last-activity sorting.
- Added private internal admin notes and audit-log entries for ticket actions.
- Added notification events for new tickets/customer replies and direct customer pushes for admin replies/status changes.
- Added customer-level default for subscribing newly-created tickets.
- Added database-backed rate limits for customer ticket creation and replies.
- Added Support navigation to admin and customer PWAs.
- Bumped admin/customer PWA caches to 0.10.0.

# v0.9.3 — Notification Operations Hardening

- Added scheduled critical customer broadcasts. Admins choose a browser-local date/time; the UI converts it to UTC for storage and delivery.
- Scheduled broadcasts resolve their eligible customer/device audience at send time and can be cancelled before they are due.
- Scheduled broadcasts use stable event keys so a worker/container interruption can safely resume without re-sending to devices that already accepted the same broadcast.
- Added automatic retry/backoff for transient notification failures: 1 minute, 5 minutes and 15 minutes after the initial attempt.
- HTTP 429, HTTP 5xx, transport/network errors and temporary Web Push errors are retried. Permanent endpoint failures and expired Web Push subscriptions are terminal; 404/410 push subscriptions remain automatically disabled.
- Test notifications are never retried automatically.
- Added a dedicated Notification History admin page with filters for event, channel and result, pagination, attempt counts, next-retry visibility, response details, and scheduled-broadcast history.
- Added a 30-second notification worker for due scheduled broadcasts and retry processing.
- Existing notification records remain valid; additive PostgreSQL columns provide retry state and a new scheduled broadcast table stores future announcements.
- Bumped admin and customer PWA cache versions to 0.9.3.

# v0.9.2 — Admin Push Preferences & Mobile Tautulli Polish

- Added admin Web Push event preferences with a master enable/disable switch.
- Existing installs default to all admin push event categories enabled, preserving 0.9.x behavior until the admin saves narrower preferences.
- Admin push test remains available regardless of the master preference switch.
- Mobile Integrations UI now collapses the Tautulli section beneath its heading; desktop remains expanded.
- PWA cache versions bumped to 0.9.2.

# v0.9.1a — Mobile Watch-History Spacing

- Added a small bottom gap beneath the collapsed mobile Watch History Filters control so its spacing matches the history-card list.
- Expanded filter-panel spacing and desktop layouts are unchanged.
- Bumped PWA cache versions so installed clients pick up the CSS hotfix.

# v0.9.1 — Critical Customer Broadcasts

- Added an admin-only Critical customer broadcast composer under Integrations → Notifications.
- Broadcasts fan out through Web Push to every eligible customer device with the master push-notification setting enabled.
- Critical broadcasts intentionally bypass category-level customer notification choices so maintenance/service-disruption notices cannot be accidentally hidden while push remains enabled.
- Archived, Cancelled, portal-disabled customers and disabled/stale subscriptions are excluded.
- The composer shows the current eligible customer/device audience before sending, requires confirmation, and supports deep-linking to Account, Activity or History.
- Each broadcast is recorded as a canonical `system.critical_broadcast` NotificationEvent; per-device attempts continue to use NotificationDelivery and expired subscriptions retain the existing 404/410 cleanup behavior.
- Sending a broadcast writes an admin audit entry with customer/device/delivery counts.
- Critical Web Push notifications request persistent interaction where supported by the browser.
- Customer notification settings now explain that service-status/maintenance broadcasts remain enabled whenever the master push setting is enabled.

# v0.8.5e

Watch-history backfill progress hotfix.

- Stops treating partially discovered per-library totals as the final history denominator.
- During total discovery the Tautulli integration now shows cached rows plus measured library-history targets instead of misleading `X / X` progress.
- The overall processed/total row count and percentage bar appear only after every active customer/library checkpoint has a known total.
- The status badge distinguishes the discovery phase (`scanning`) from normal measured backfill (`syncing`).
- No history reset or re-sync is required; existing backfill checkpoints continue normally after upgrade.

# v0.5.3

This release continues the v0.5.2 correctness work and hardens disaster recovery, payment rollback, pending Plex invitations, authentication sessions, CI, and worker failure visibility.

## Backups / restore

- Backup storage failures such as an NFS/CIFS `ESTALE` (stale file handle) are now surfaced as an actionable Backups-page error instead of a raw 500.
- The scheduled backup worker now catches storage/settings-read failures inside its retry boundary and logs worker exceptions.
- PostgreSQL restore now uses `pg_restore --single-transaction` together with the existing validation and `--exit-on-error` flags, so restore changes are atomic where PostgreSQL supports them.
- Restore enters application maintenance mode: new non-health requests return 503, new billing/backup/Tautulli cycles do not start, and restore waits for any already-running serialized background DB cycle to finish.
- After restore, Share Manager verifies that core application tables exist before reporting success.
- SQLite backup creation now uses SQLite's online backup API instead of a raw file copy. SQLite restore validation now runs `PRAGMA integrity_check` and verifies core Share Manager tables.

A stale `/backups` mount still requires the host/container mount to be repaired. Application code can report this cleanly but cannot remount host NFS/CIFS storage from inside the container.

## Billing / payment maintenance

- New applied payments capture the exact subscription/customer state they replaced.
- Voiding the latest payment restores that captured state, including manually initialized coverage that was not represented by an earlier ledger event.
- For pre-v0.5.3 legacy payments with no recoverable prior state, Share Manager now refuses the destructive void and presents a repair message instead of clearing dates and granting indefinite active access.

## Plex pending invitations

- Pending invitations are now treated as entitlement state.
- Suspending a not-yet-accepted customer removes that server's pending invitation/share.
- Changing package before acceptance replaces the pending server invitation with one carrying the current desired libraries.
- Server-specific deletion is preferred; if Plex does not expose a server share id, Share Manager only falls back to cancelling the whole invite when it is safe to do so without affecting another server.

## Authentication

- Sessions are now server-expiring signed tokens with a seven-day default maximum age.
- Session validity includes a derived version of the configured admin credentials, so changing the admin password invalidates existing sessions.
- `SESSION_COOKIE_SECURE=true` can be used for HTTPS-only deployments. It remains false by default for local HTTP compatibility.

## Release pipeline

- GitHub release publishing now runs the Python test suite before creating/pushing a version tag or publishing the container.

## Validation

The repository contains 58 tests after this release (53 from v0.5.2 plus five additional regression tests). In the build sandbox, the full suite passed with a lightweight local PlexAPI import stub because external package installation was unavailable; service logic and all new regression tests passed. GitHub CI now installs the real pinned dependencies and runs the full suite before publishing.


## v0.5.4

- Removed the runtime SQLite fallback. `DATABASE_URL` is now mandatory and must resolve to PostgreSQL.
- Removed SQLite creation, validation, restore and upload handling from Disaster Recovery. Only PostgreSQL `.dump` files are accepted.
- Removed SQLite-specific SQLAlchemy runtime connection arguments.
- Kept SQLite only in isolated unit-test fixtures; it is not a deployable Share Manager backend.
- Fixed a v0.5.3 regression where downloading a stored backup incorrectly entered restore maintenance mode and acquired the database worker lock.

## v0.5.5
- Desktop navigation sidebar now stays anchored to the viewport while main page content scrolls. Mobile off-canvas navigation is unchanged.


## v0.5.6 — Customer archiving
- Added reversible customer archiving so stale customers can be removed from the operational Customers view without deleting payments, subscriptions, credits, audit history, or Tautulli activity.
- Customers must be Cancelled before they can be archived, preventing active Plex access from being hidden accidentally.
- Added an Archived customers view with History, Edit, and Restore controls.
- Archived customers are excluded from dashboard operational counts, revenue forecasts, billing processing, payment-entry customer selection, Tautulli matching, and Tautulli dashboard aggregates.

## v0.5.7
- Added customer multi-select and a sticky bulk-action bar.
- Added Select visible and Clear selection controls that work with current filters.
- Added bulk status updates, package/tier changes, Plex reconciliation, and safe archival.
- Bulk package changes require an explicit start date and use the same new-period semantics as the existing Change package action.
- Bulk archive preserves history and skips any customer that is not Cancelled and non-exempt.


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

## 0.7.0
- Added installable PWA manifest and standalone display metadata.
- Added standard, maskable, Apple touch and favicon icon assets.
- Added root-scoped service worker with versioned static-only caching.
- Added a deliberate offline/unreachable page instead of caching live admin data.
- Added standalone safe-area handling for mobile status/gesture areas.


## v0.7.1 — Mobile quick navigation
- Added a persistent mobile bottom navigation bar for Dashboard, Customers, and Payments.
- Added current-page highlighting to both the bottom navigation and sidebar drawer.
- Added safe-area-aware bottom spacing so navigation does not cover page controls on installed PWAs or gesture-navigation devices.
- Kept all secondary areas in the existing hamburger drawer.
- Bumped the PWA service-worker cache version so updated navigation/CSS replaces the 0.7.0 shell cleanly.

## v0.7.2 — Phone-first Dashboard
- Tightened the mobile Dashboard header and primary billing/customer stat cards.
- Reworked forecast totals into a compact two-up mobile layout with annualised revenue spanning the row.
- Moved mobile package distribution behind an expandable section to reduce default scroll length.
- Prioritised live Plex activity with a dedicated compact Watching now/7-day/30-day strip.
- Moved secondary Tautulli usage metrics behind an expandable mobile section.
- Converted Recent Activity into an expandable mobile card feed while preserving the desktop table.
- Preserved desktop Dashboard behaviour and live Tautulli heartbeat semantics.

## 0.7.3
Customers mobile UX polish: compact filter sheet, improved selection/bulk controls, denser customer cards and stronger live-stream visibility. Desktop behavior remains unchanged.


## v0.7.5
- Stream Limits mobile-first UI: compact stats, quick date filters, collapsible filter panel, mobile enforcement cards, clearer success/failure presentation, tappable customers, and no horizontal scrolling.


## v0.7.5a
- Fixed Stream Limits filtering when `All customers` submits an empty `customer_id`; blank values now mean no customer filter instead of triggering FastAPI integer validation.
- Invalid non-numeric customer filter values now redirect safely back to the unfiltered Stream Limits page.


## v0.7.6
- Phone-first Backups and Integrations pass: compact DR status/settings, mobile backup cards, touch-friendly restore controls, denser Plex/Tautulli/notification integration cards, and mobile notification-delivery cards without horizontal scrolling. Desktop behavior remains unchanged.

## v0.8.0
Customer portal foundation: opt-in per-customer credentials, one-time temporary-password display, bcrypt hashing, separate timed portal sessions, login throttling, cancellation/session revocation, and a read-only subscription/account dashboard.


### 0.8.0 build compatibility fix
Pinned `bcrypt==4.0.1` alongside Passlib 1.7.4. Newer bcrypt releases are incompatible with Passlib 1.7.4's backend self-test on Python 3.12 and can fail before hashing otherwise-valid portal passwords.


## v0.8.1 — Customer activity
- Added a read-only customer Activity portal page backed by the existing cached Tautulli analytics.
- Customers can see last streamed/title, 30-day and lifetime watch time/play counts, current stream allowance, live sessions, and their own recent stream-limit enforcement history.
- Live sessions refresh asynchronously using the configured Tautulli live interval.
- Customers can stop only their own currently active Plex sessions. Ownership is re-verified server-side against a fresh Tautulli activity response before termination; arbitrary session keys cannot be used to stop another customer's playback.
- Customer-initiated stops are recorded in the admin audit log but are not counted as stream-limit enforcement events.

## 0.8.2
- Added customer-facing History tab to the portal.
- Added read-only payment history with amount, date, source, billing periods, and coverage.
- Reversed payments remain visible and are excluded from lifetime-paid totals.
- Added complimentary-access history with coverage and customer-facing reason.
- Added subscription/package history including tier, price, interval, status, and recorded coverage.
- Internal audit entries, admin/payment notes, external references, reconciliation details, and system logs remain admin-only.
- Expanded portal bottom navigation to Account / Activity / History.

## 0.8.3 — Customer portal UX/PWA polish
- Added a desktop customer-portal sidebar with Account, Activity, History and sign-out; mobile keeps the compact bottom navigation.
- Added active navigation states and shared portal iconography across desktop/mobile.
- Added a dedicated customer-portal PWA manifest and `/portal/`-scoped service worker so customer installs launch directly into the portal rather than the admin interface.
- Added customer self-service password changes. Password changes rotate the portal session version, revoke other customer sessions, and refresh the current session safely.
- Tightened desktop content width, mobile safe-area spacing, touch targets and portal security-form layout.


## v0.8.4 — Detailed watch history
- Added PostgreSQL-cached Tautulli viewing-history rows per customer.
- First detailed sync backfills up to 500 recent rows per matched user; later syncs refresh the newest 100 rows to stay lightweight.
- Customer Activity now exposes title, watched date/time, library, device/player, platform, media type and playback duration.
- Added customer-scoped filters for title search, device, library, media type and date range.
- Portal queries always derive customer ownership from the authenticated portal session; watch history cannot be queried for another customer.


## v0.8.5 — Full asynchronous Tautulli history backfill
- Replaces the 500-row initial watch-history cap with a resumable full-history backfill for every matched, non-archived customer.
- Backfill runs independently of normal Tautulli analytics sync in 500-row pages, so Sync Now and portal requests remain responsive.
- History is imported oldest-first to keep pagination stable while new plays continue to arrive; normal syncs continue refreshing the newest 100 rows.
- Backfill checkpoints and totals are persisted per customer, allowing imports to resume after container restarts or temporary Tautulli failures.
- Integrations now shows live backfill progress, cached row counts, customer completion counts, and the latest backfill error.


## v0.8.5a
- Fixed the desktop customer-portal watch-history filter layout so the controls stay within the Activity card. Search/device/library/type remain on the first row, while date range and Reset/Apply actions align cleanly on the second row. No sync or filtering logic changed.

## v0.8.5c
Watch-history usability hotfix: server-side 10/25/50 row pagination and stable metadata alignment, including browser platform normalization to Web.


### v0.8.5c library history hotfix
Detailed Tautulli watch history now resolves library names by syncing history per Plex library section. Existing cached history with missing library names is repaired asynchronously by the resumable full-history worker.

## v0.8.5d

### Force full watch-history re-sync hotfix
- Adds an admin-only **Force full re-sync** action under Integrations → Tautulli.
- The action deletes only Share Manager's cached Tautulli watch-history rows and per-library backfill checkpoints.
- Customer, package, billing, payment, subscription, Plex entitlement, stream-limit and audit history data are left untouched.
- Legacy per-customer history checkpoint fields are reset for upgrade consistency.
- The operation is serialized against background DB workers to prevent a backfill page racing with the cache reset.
- The existing asynchronous backfill worker automatically reseeds library checkpoints on its next cycle and rebuilds the full history from Tautulli.
- Adds a destructive-action confirmation and records the operation in the audit log.

## 0.9.0a — Mobile watch-history filter UX hotfix

- Replaces the cramped mobile Watch History filter grid with a collapsible Filters panel.
- Stacks Search, Device, Library, Type, From, To, Reset and Apply vertically at full width on narrow screens.
- Automatically leaves the panel expanded after a filtered page load so active criteria stay visible.
- Desktop Watch History filtering is unchanged.
- Bumps both PWA cache names so installed admin/customer PWAs pick up the updated stylesheet.

## 0.9.0 — Notification Platform

- Added canonical notification event records as the common source for all notification delivery channels.
- Added native Web Push for the admin PWA and customer portal PWA using VAPID.
- Added automatic persistent VAPID key generation.
- Added per-device push subscription storage, failure tracking and stale-subscription disabling.
- Added customer notification preferences for payment, billing status, renewal, Plex invite and stream-limit events.
- Added customer and admin push test actions plus current-device enable/disable controls.
- Added push notification deep links through the service workers.
- Extended the notification delivery ledger with channel/recipient metadata.
- Preserved Home Assistant, Discord and generic webhook behavior.
- During Tautulli history discovery, the progress bar now uses measured library histories as the best available progress analogue until the final row denominator is known.
