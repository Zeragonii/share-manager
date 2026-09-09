# v0.5.2

This release fixes missed Plex access updates after failures and after payment maintenance. It keeps the existing renewal calculations, grace periods, complimentary credits, manual access overrides, and legacy uninitialized-subscription behavior.

## Fixes

- **Persistent Plex retries.** Every attempted customer/server reconciliation is recorded before contacting Plex. Successful work is removed; failed work remains in the database and is retried by the billing scheduler, including after an application restart. One failing server no longer prevents attempts on the customer's other enabled servers.
- **Current access on retry.** Jobs store customer and server IDs, not an old access decision. Each attempt reloads current customer, subscription, package, and identity data. Exempt or unlinked customers are skipped and their outstanding work is cleared. Disabled servers are not contacted; their jobs wait until re-enabled.
- **Bounded retry frequency.** Consecutive failures defer attempts by 1, 2, 4, 8, 16, 32, then at most 60 minutes. Due jobs are checked during the configured billing cycle, so actual retry timing is rounded up to a scheduler run (15 minutes by default). Explicit reconciliation and normal immediate actions can retry sooner. Retries do not repeat billing transition notifications.
- **Scoped payment updates.** Editing coverage or voiding an applied payment refreshes billing only for that payment's customer. Other customers remain available to the regular billing cycle, which performs their status notifications and Plex reconciliation. This local billing refresh also runs for customers without a linked Plex account.

## Upgrade

Deploy using the existing procedure. The startup initializer automatically creates the new `plex_reconcile_jobs` table. No existing billing or payment columns are rewritten, and no new environment settings are required.

The supplied deployment runs a single application worker. An in-process lock serializes immediate and scheduled Plex attempts in that worker; multi-worker/distributed job claiming is not introduced in this release.

The retry queue starts empty on upgrade. It does not infer unresolved failures from old audit history. If a customer already has incorrect Plex access from a v0.5.1 failure, use their existing **Reconcile Plex** action once; failed attempts from then on are retained for automatic retry.

Pending invitations retain their existing success/pending semantics. This release does not change payment rollback rules, pending-invitation management, restore behavior, or authentication.

## Verification

Run from the repository root:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The release was checked with 53 passing tests on Python 3.12: all 37 existing tests plus 16 new regression cases. The new cases cover failed suspension recovery without a new status transition, persistence across a recreated database engine, interrupted attempts, payment reactivation and package changes before retry, exemptions/unlinking, disabled servers, retry delays, multiple servers, pending-invitation compatibility, scoped payment edit/void handling, and repeatable startup table creation.

Tests use disposable SQLite databases and mocked Plex calls. No live Plex server or PostgreSQL restore was used, and no container build was performed as part of this release preparation.
