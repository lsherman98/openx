# OpenX

React, TanStack Router/Query, Tailwind, and PocketBase with Go.

## Run locally

```sh
make install
make pb   # backend on 127.0.0.1:8090
make dev  # frontend on localhost:5173
```

Root `.env` holds frontend settings; `pocketbase/.env` holds backend settings. See their `.env.example` files for available keys. Configure SMTP and OAuth in the PocketBase dashboard as needed.

## Schema and types

After changing collections in the local PocketBase dashboard:

```sh
make migrate # capture collection changes as a committed Go migration
make types   # refresh Go names/enums, schema snapshot, and TypeScript SDK types
```

Development automigration is enabled only in PocketBase development mode and outside `APP_ENV=production`. Commit and review Go migrations in `pocketbase/migrations`; production applies pending committed migrations at startup. `make sync` marks migration history as already applied; use it deliberately, never as a substitute for running migrations.

`make types` reads `_collections` from the local database through a read-only SQLite connection, bypassing PocketBase startup and migrations. SQLite may update WAL coordination sidecars; use `make types ARGS='--schema /path/to/export.json'` when the source directory must remain entirely untouched. Without a local database it uses the checked-in `pocketbase/schema.json`. That JSON is a sanitized **typing snapshot**, not a complete migration or an importable backup of rules/settings. Both languages use the same snapshot, so frontend and backend field names stay aligned. Hidden/password fields are excluded from frontend response types.

```go
import (
    "github.com/pocketbase/pocketbase/core"
    "github.com/my-org/my-app/pocketbase/collections"
)

collection, err := app.FindCollectionByNameOrId(collections.Users)
if err != nil { return err }
record := core.NewRecord(collection)
record.Set(collections.UsersFieldName, "Ada")
record.Set(collections.UsersFieldEmail, "ada@example.com")
// Set all required fields before saving.
return app.Save(record)
```

For an `orders.status` select field with `pending` and `paid` options, generation also produces `OrdersStatus`, `OrdersStatusPending`, and `OrdersStatusPaid` constants. Use `record.Set(collections.OrdersFieldStatus, collections.OrdersStatusPaid)`. Constants prevent misspelled names in normal use; PocketBase's dynamic `Set` API still permits arbitrary strings and values. For compile-time value checking, use [record proxies](https://pocketbase.io/docs/go-record-proxy/) with typed setters in domain packages. Generated constants keep those setters free of repeated schema strings.

Frontend types are in `src/lib/pocketbase-types.ts`. Use `Collections.Users` and `UsersResponse` with the typed `pb` client. When adding features, create `src/lib/api/api.ts` for data access, `queries.ts` for query options, and `mutations.ts` for mutations. Route loaders receive the same typed `context.queryClient` as the React provider. A query with local error presentation can set `meta: { silentError: true }` to suppress the global toast.

## Errors, logs, and analytics

The frontend has route recovery and not-found pages, global query/mutation error handling, safe user-facing messages, request cancellation handling, and bounded retries. Mutations never retry automatically. Sign-in/signup guard pending actions, honor safe internal return paths, and show a retry action when authentication settings cannot be loaded.

The backend retains PocketBase dashboard logs. Structured JSON output to systemd/journald is off by default and can be enabled for a log collector. When enabled, request events include service, environment, route pattern, request ID, status, and duration. Every response has `X-Request-ID`; application handlers can use `observability.Logger(e)` to include it in their logs. Known credential keys and request payloads are redacted before persistence/forwarding. Avoid putting secrets or personal data into arbitrary log messages; redaction is best effort.

`LOG_SERVICE`, `APP_ENV`, `LOG_LEVEL`, and `LOG_JSON` configure additional JSON output; `LOG_JSON=false` by default. PocketBase's dashboard settings independently control database log retention/level. Application log forwarding follows PocketBase's normal batching delay. See [the observability package guide](pocketbase/observability/README.md) for examples and limitations.

Sentry SDKs and PostHog are integrated but disabled with empty settings. Sentry supports a self-hosted DSN and optional sampled tracing; PostHog captures explicit events, named route views, and Web Vitals after analytics consent. Frontend SDKs load only when enabled. Autocapture, replay, and person profiles are off. A provider-neutral `configureTelemetry` adapter remains available for customization.

`make setup-observability` configures an optional self-hosted Grafana/Alloy/Loki stack beside the app, with persistent storage, log retention, a dashboard, and an error alert. It can also deploy lightweight, open-source GlitchTip using the Sentry SDK protocol. `make observability` previews the setup; `make observability ARGS=--apply` installs it explicitly. PostHog's larger unsupported hobby server is kept separate; its SDK can point at your existing self-hosted instance. See [integration settings, consent, alerts, and service deployment](docs/observability.md).

## Deploy to a VPS

```sh
make setup-vps # prompt for SSH host, domain, slug; auto-select a backend port
make deploy-check
make provision # preview the proposed server setup
make provision ARGS=--apply
make deploy # preview the release
make deploy ARGS=--apply
```

After provisioning, run `make build-deploy` (or `pnpm build:deploy`) to build and deploy in one command. It builds the frontend and Linux backend once, uploads the release, and restarts the configured VPS service. Use `make build` to build only the frontend and local backend binary.

The generated files are in ignored `deploy/generated/`. Auto port selection reads the VPS over SSH and saves a concrete port for review; supply a number to skip discovery. Provisioning creates an app user, systemd unit, protected environment file, and a Caddy site while preserving other Caddy sites. It checks for existing service names, Caddy domains, and localhost port listeners before applying. Existing root-run services and data directories need a deliberate migration. It assumes an existing Linux VPS with working Caddy/systemd, trusted SSH host keys, and root or passwordless sudo. The commands use SSH keys. They do not install server prerequisites or change DNS/firewalls.

Releases build a Linux binary and frontend, upload a new release directory, stop the app, back up remote `pb_data`, switch the `current` symlink, restart, and check health. They never upload local `pb_data`. A failed start leaves the app stopped with its backup and release paths reported; database rollback is deliberately manual because migrations can mutate data. Read [setup and deployment details](docs/setup-deployment.md) before the first provision/release, including custom env files, retained backups, and recovery.

## Checks and upkeep

```sh
make check # lint, JavaScript/Python syntax, Go vet, both builds, type drift, diff whitespace
pnpm typecheck # native TypeScript type check
pnpm lint:fix  # apply Oxlint's safe automatic fixes
make help
```

GitHub Actions runs the same checks. This project includes no tests or test runner; add project-specific coverage when you need it. Dependabot proposes dependency updates; run checks and review PocketBase migration/release notes before accepting backend upgrades. Builds replace stale `pb_public` assets. Generated schema/type files are committed so fresh projects build without a running database.

Application code lives in `src/` and `pocketbase/`; setup and deployment tools live in `scripts/`; guides live in `docs/`. Reusable UI components stay in `src/components/ui/`. Add custom Go routes and jobs in domain packages as your app needs them.

Keep the project small: add billing, email providers, background jobs, and role rules in the projects that need them. The highest-value next improvement is off-VPS scheduled backups with a tested restore process; per-release backups on the VPS do not protect against losing the VPS itself.
