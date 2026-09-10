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
