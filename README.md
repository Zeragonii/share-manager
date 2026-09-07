# Share Manager v0.1.0

A self-hosted customer, subscription and entitlement manager with Plex as the first supported access target.

## What this first pass does

- Runs as a one-stack Docker/Portainer deployment with bundled PostgreSQL.
- Password-protected administrator UI.
- Customer records with Plex identities and lifecycle states (`active`, `grace`, `suspended`, `cancelled`).
- Packages separated cleanly from billing tiers.
- Any package can have multiple billing tiers (for example £10/month and £100/year).
- Plex integration test, library discovery and Plex-user import.
- Package → Plex-library entitlement mapping from the UI.
- Subscription assignment to customers.
- Desired-state Plex reconciliation using Python-PlexAPI.
- Manual payment ledger designed for future payment-provider adapters.
- Audit log for important management and reconciliation actions.
- Health endpoint at `/health`.


## Releases and container images

The repository includes a GitHub Actions release workflow. `VERSION` is the source of truth.

1. Change `VERSION` (for example from `0.1.0` to `0.1.1`) and push it to `main`.
2. **Build and publish release** validates the version and creates `v0.1.1` if that tag does not already exist.
3. The same workflow immediately builds multi-architecture `linux/amd64` + `linux/arm64` images, publishes them to GHCR, and creates a GitHub Release with generated notes.
4. The image is tagged with the full version, major/minor, major, and `latest`.

Tag creation and image publication intentionally happen in the same workflow. GitHub suppresses most follow-on workflow events caused by a repository `GITHUB_TOKEN`, so splitting automatic tag creation and release publication into separate workflows can result in the release workflow never running.

The GHCR image name is derived from the GitHub repository at build time and forced to lowercase, avoiding invalid Docker references when the GitHub owner or repository has uppercase characters. No PAT is required: the workflow uses the repository `GITHUB_TOKEN` with `packages: write`.

GitHub Container Registry package visibility is independent of repository visibility. For a public pullable image, after the first successful publish open the package in GitHub and set its visibility to **Public** once. Subsequent workflow releases remain available through the same package.

`docker-compose.yml` defaults to `ghcr.io/zeragonii/share-manager:latest`. If the repository was created under a different name, set `SHARE_MANAGER_IMAGE` in `.env` to the image shown by the release workflow.

## Deploy in Portainer

1. Extract/clone this repository on the Docker host, or build/publish the image and point the stack at it.
2. Copy `.env.example` to `.env` and change all secrets/passwords.
3. Deploy `docker-compose.yml` as a stack.
4. Open `http://<docker-host>:8080`.
5. Sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
6. Add the Plex integration under **Integrations** using a Plex server URL reachable from the container and a Plex token belonging to the server owner.
7. Import Plex users.
8. Create packages and billing tiers.
9. Select the Plex libraries each package grants.
10. Assign customers to billing tiers and reconcile.

### Example environment

```env
DB_PASSWORD=use-a-long-random-value
APP_SECRET=use-another-long-random-value
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me
RECONCILE_ON_ASSIGN=true
```

## Example package model

`Package 1` may define Plex access to Movies + TV while having two billing tiers:

- Monthly — £10 every 1 month
- Annual — £100 every 1 year

`Package 2` could grant Movies + TV + 4K libraries with £15/month and £150/year tiers. Billing terms do not duplicate or define the actual access rules.

## Reconciliation behaviour

For each enabled Plex integration, Share Manager calculates the libraries the customer should currently have from active subscriptions.

- `active` / `grace`: package library entitlements are applied.
- `suspended` / `cancelled`: no library sections are shared.
- Customer-level **Automation Exempt** toggle: reconciliation intentionally skips the customer and leaves Plex access untouched.
- no linked Plex identity: reconciliation intentionally skips the customer.

The implementation calls Python-PlexAPI `updateFriend()`, using the desired section list or `removeSections=True` when the desired set is empty.

## Persistence and backups

The important persistent state is the named Docker volume `postgres_data`. `app_data` is reserved for future application-side persistent data/imports.

Back up the PostgreSQL database like any other PostgreSQL service. Do **not** rely on backing up the application container filesystem.

## Security notes

This is a first-pass homelab/admin application, not an internet-facing SaaS product. Put it behind your normal authenticated reverse proxy/VPN if exposing it beyond your LAN. The Plex token is currently stored in the application database so the reconciliation worker can use it. A future hardening milestone should encrypt integration secrets at rest and support secret rotation.

Change the default admin password **before first deployment**.

## Planned next milestone

- CSV import from an existing payment spreadsheet.
- Proper subscription periods, due dates and grace-period calculation.
- Payment → subscription-state automation.
- Editable/deletable customer/package/payment records.
- Payment source/provider adapter interface and webhook event model.
- Tautulli adapter and activity metadata.
- Notification adapters.
- Background scheduled reconciliation.
- Database migrations (Alembic) instead of `create_all` once schema changes begin shipping.
- Integration-secret encryption at rest.

## Development

Python 3.12 is the target runtime. For a quick local SQLite development run:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=sqlite:///./sharemanager.db
export APP_SECRET=dev-secret
export ADMIN_PASSWORD=dev-password
python -m app.init_db
uvicorn app.main:app --reload --port 8080
```

## Version

`0.1.0` — foundation + Plex entitlement first pass.

## v0.1.3

- Added package editing for name and description.
- Added in-place billing tier editing.
- Added guarded delete controls for billing tiers and packages.
- Packages or tiers referenced by subscriptions cannot be deleted.

## v0.1.4
- Reworked package management UI into a compact overview-first layout.
- Package details are hidden behind an explicit editor instead of being permanently expanded.
- Billing tiers now render as compact summary rows with per-tier edit controls.
- Plex entitlements render as library chips until the library editor is opened.
- Package deletion moved into the package editor/danger area.

### v0.1.6
- Restored the Packages page to the original v0.1 minimalist layout.
- Added a small per-tier Edit control for price, interval, count and name.
- Retained guarded tier/package deletion and live-only subscription counts.
- Retained all backend fixes and existing data model behavior from v0.1.5.

### v0.1.7
- Package/tier edit fields are hidden until their Edit button is pressed.
- Static CSS is cache-busted using the application version.
- Deleting a tier/package that has historical subscriptions archives it instead of violating subscription foreign-key history.
- Tiers/packages with current subscriptions remain protected from deletion.


## v0.1.8

- Fixed Plex suspension/reconciliation when the desired library set is empty.
- Suspended/cancelled users now have their share to the configured Plex server explicitly removed while preserving the Plex friend relationship.
- Plex entitlement changes are verified against fresh plex.tv share state before Share Manager reports success.
