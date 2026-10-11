# Optional observability

Integrations are disabled until configured. Set frontend and build values in root `.env`, backend values in `pocketbase/.env`, and production backend values in `deploy/production.env`. Apply reviewed deployment changes with the commands below.

## Error reporting with Sentry SDKs

Set these in the root `.env` before building the frontend:

```dotenv
VITE_SENTRY_DSN=https://PUBLIC_PROJECT_KEY@errors.example.com/PROJECT_ID
VITE_TELEMETRY_ENVIRONMENT=production
VITE_TELEMETRY_RELEASE=my-app@revision
VITE_SENTRY_TRACES_SAMPLE_RATE=0
```

Set the backend environment in `pocketbase/.env` locally or
`/etc/<app>/<app>.env` on the VPS:

```dotenv
SENTRY_DSN=https://PUBLIC_PROJECT_KEY@errors.example.com/PROJECT_ID
SENTRY_RELEASE=my-app@revision
SENTRY_TRACES_SAMPLE_RATE=0
```

A DSN's public project key may be bundled in the frontend. Auth tokens,
administrator passwords, and SMTP credentials must never use `VITE_` names.
Separate frontend/backend projects are useful for independent alerts and issue
ownership; they can also share a project. Restart the backend after changing its
environment and rebuild the frontend after changing `VITE_` settings.

The React integration captures existing route/query/global error reports,
retaining stack locations while removing exception messages, account data,
request URLs, breadcrumbs, and source contents. PocketBase's `X-Request-ID` links
failed SDK requests to backend JSON logs. Expected validation/authentication 4xx
errors and cancelled requests stay out of frontend monitoring. The first ten
errors during an enabled Sentry SDK import are held briefly in memory and
scrubbed before capture; nothing is queued with error reporting disabled.
Backend unexpected errors, HTTP 5xx,
panics, and application ERROR logs are reported asynchronously. Use
`observability.Logger(e)` in handlers for immediate reporting and request
correlation. Known sensitive backend attributes are redacted, but arbitrary
error/log messages still need care: do not embed personal data or secrets.

Tracing defaults to zero. Set either sample rate to a fraction such as `0.1` to
enable sampled browser or HTTP server timing. Request IDs provide correlation;
automatic distributed trace propagation and database instrumentation are not
configured.

For readable frontend source locations, the Vite build can upload hidden source
maps using the official Sentry plugin when **all** of these build-only variables
are present:

```dotenv
SENTRY_URL=https://errors.example.com
SENTRY_ORG=your-organization-slug
SENTRY_PROJECT=frontend-project-slug
SENTRY_AUTH_TOKEN=your-upload-token
```

The uploader uses that self-hosted URL and disables its own telemetry. Maps are
deleted after successful upload and never intentionally served with the app.
A configured upload failure fails the build. Without complete upload settings,
no source maps are generated. Keep the upload token in an ignored environment
file or CI secret; normal application builds need no token. Source-map upload and
tracing compatibility depend on the receiving server's API version.

## Product analytics with PostHog

Use your self-hosted PostHog project key and origin:

```dotenv
VITE_POSTHOG_KEY=phc_your_public_project_key
VITE_POSTHOG_HOST=https://analytics.example.com
VITE_ANALYTICS_CONSENT=denied
```

Analytics stays off until granted. Connect your app's consent preference to:

```ts
import { setAnalyticsConsent, setTelemetryEnabled } from '@/lib/telemetry-providers'
import { trackEvent } from '@/lib/telemetry'

await setAnalyticsConsent(true) // false revokes analytics consent
trackEvent('project_created', { source: 'dashboard', plan: 'free' })
await setTelemetryEnabled(false) // disable both frontend integrations
```

For a deployment that has already obtained consent, set
`VITE_ANALYTICS_CONSENT=granted`. A saved user choice takes precedence. Do Not
Track also disables analytics. Sentry error reporting is controlled independently
of analytics consent. Development telemetry is off unless
`VITE_TELEMETRY_IN_DEV=true`; `VITE_TELEMETRY_ENABLED=false` disables all frontend
telemetry in a build.

Only explicit custom events, named route page views, and Web Vitals are captured.
Autocapture, session replay, automatic exception capture, feature flags, surveys,
and person profiles are disabled. Anonymous IDs live in memory; there is no
cross-reload visitor tracking. Custom properties must be bounded categorical
values. Obvious sensitive fields/URLs/email-like values are dropped, but this is
not a substitute for choosing safe event properties.

PostHog's self-hosted hobby stack is officially unsupported and currently calls
for roughly 4 vCPU, 16 GB RAM, and over 30 GB storage. Its installer manages its
own Caddy listeners on ports 80/443. Use a separate VPS or a separately reviewed
proxy deployment; it is not included in the small app-side Compose bundle. The
SDK works with an existing self-hosted instance without any cloud service.
See [PostHog's current self-hosting instructions](https://posthog.com/docs/self-host).

## Self-host Grafana, Alloy, Loki, and optional GlitchTip

After `make setup-vps`, generate and review the optional services:

```sh
make setup-observability
make observability-check
make observability # preview; no SSH or running containers
make observability ARGS=--apply
make observability-status
```

For a repeatable configuration:

```sh
make setup-observability ARGS='--non-interactive --grafana-domain logs.example.com --retention-days 14 --error-domain errors.example.com --error-admin-email owner@example.com'
```

Leave the error domain blank to skip GlitchTip. Leave the Grafana domain blank
to keep its UI private through an SSH tunnel. The root `.env` and backend `.env`
remain independent of the service configuration.

The deployment reuses `deploy/config.json`, an existing working Caddy/systemd
installation, SSH keys/trusted host keys, and root or passwordless sudo. Install
Docker Engine and its Compose plugin on the VPS first. Alloy journal collection
requires Linux with a rootful Docker daemon and no user namespace remapping. DNS
records for configured domains must already point at the VPS.
Alloy runs as root inside its container to read the mounted host journal, with
read-only mounts and dropped capabilities. Its filter forwards only the app's
unit; the underlying journal files can contain other units, so keep access to
Docker and the generated configuration restricted to trusted administrators.

Generated files and initial passwords are in ignored `deploy/observability/`.
They are not printed by commands. Configuration reruns retain existing secrets;
remote reruns preserve `/etc/<app>-observability/secrets.env`. Retrieve credentials
privately on the host where they were initially generated. Changing the local
password file does not rotate an existing service's password.

The stack includes:

- Grafana with authentication, a provisioned Loki data source, request/error/latency
  panels, structured logs, and an application error alert.
- Alloy reading only `<app>.service` from the existing host journal, preserving
  JSON data and `request_id` as structured metadata rather than an indexed label.
- Private single-binary Loki with persistent storage and 14-day retention by
  default. Retention is configurable; it does not enforce a disk-size quota.
- Optional MIT-licensed GlitchTip using the Sentry SDK protocol, PostgreSQL, and
  an embedded worker. Its initial administrator is created before its Caddy site
  is exposed; repeated setup preserves the existing account/password.

Only Grafana and optional GlitchTip have loopback host ports. Loki and Alloy have
no public host ports. Caddy exposes configured UI/ingestion domains using TLS.
Docker data lives in named volumes; applying configuration never replaces app
`pb_data`. A failed apply restores service/proxy configuration and retains data
volumes, leaving the optional stack stopped for inspection. Database migrations
within Grafana or GlitchTip are not reversed by configuration rollback. Choose a
compatible version before restarting; back up service volumes and the private
environment before upgrades.

Grafana/Alloy/Loki are capped at about 1.5 GiB total container memory. GlitchTip
and PostgreSQL add about 1 GiB of caps; these are limits, not measured consumption
or guaranteed sizing. Allow headroom for PocketBase, Caddy, Docker, and the OS.
Monitor available disk and start with short retention on a shared VPS.

After GlitchTip starts, sign in with the configured administrator email and
`GLITCHTIP_ADMIN_PASSWORD`, create an organization/project, and copy its DSN to
the frontend/backend settings above. To enable email alerts, securely edit
`GLITCHTIP_EMAIL_URL` in the remote
`/etc/<app>-observability/secrets.env`, then reapply. Existing remote values take
precedence over the local file, including this URL. The default writes email to
the service console. Configure Grafana contact points/notification
policies to deliver its alert externally. No email or webhook destination is
enabled by default.

Image versions are pinned in `scripts/observability.py` and
`scripts/observability_errors.py`. Review upstream release notes and back up data
when updating them. `make observability-check ARGS=--runtime` additionally
validates Alloy/Loki with their pinned images using a local Docker daemon;
remote apply always validates those configurations before starting services.

References: [GlitchTip installation](https://glitchtip.com/documentation/install/),
[Sentry SDK compatibility](https://glitchtip.com/sdkdocs/),
[Loki monolithic deployments](https://grafana.com/docs/loki/latest/get-started/deployment-modes/),
[Alloy journal collection](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/).
