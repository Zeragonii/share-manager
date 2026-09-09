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
