# Logging and request correlation

PocketBase's database-backed logs remain available by default. Set `LOG_JSON=true` when a collector consumes the additional structured stdout events. Under systemd, stdout is captured by journald.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOG_SERVICE` | `pocketbase-template` | Service label used by log searches and alerts. |
| `APP_ENV` | `development` | Environment label, usually `development` or `production`. |
| `LOG_LEVEL` | `info` | Minimum stdout severity: `debug`, `info`, `warn`, or `error`. |
| `LOG_JSON` | `false` | Set `true` when a collector consumes structured stdout logs. |
| `SENTRY_DSN` | empty | Opt into backend error reporting to Sentry or a self-hosted Sentry-compatible endpoint. |
| `SENTRY_RELEASE` | empty | Optional deployed version or commit used to group regressions. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0` | Sample of HTTP transactions to report, from `0` to `1`; `0` disables tracing. |

PocketBase's Dashboard > Settings > Logs controls database retention (5 days by default, with cleanup every 6 hours) and its logger's minimum severity. Application logs must pass that minimum before they can be forwarded. A zero database retention period disables application-log forwarding because there are no log-write events; HTTP stdout logs continue independently when `LOG_JSON=true`. The host's journald policy independently controls stdout log storage; see [log storage and retention](../../docs/observability.md#log-storage-and-retention).

HTTP events include `service`, `environment`, `request_id`, `method`, `route`, `status`, and `duration_ms`. `route` is a registered route pattern, such as `GET /api/collections/{collection}/records/{id}`, so user record IDs and query strings are excluded from stdout. Successful probes of PocketBase's built-in `GET /api/health` route are excluded from JSON output, database activity logs, and Sentry tracing; failed probes remain visible.

Each request accepts a single `X-Request-ID` containing 1–128 ASCII letters, digits, dots, underscores, or dashes, with a letter/digit first. Missing or invalid values are replaced with a random 128-bit ID. The response carries the same header, and PocketBase's stored activity log includes it at `data.meta.request_id`. IDs are correlation hints supplied by clients; never use them for authorization.

Use the correlated logger in custom handlers:

```go
observability.Logger(e).Error(
    "invoice creation failed",
    "operation", "create_invoice",
    "error", err,
)
```

Application events expose structured attributes at `data` and a top-level `request_id` when supplied by this logger. Pass `observability.RequestID(e)` into background work and include it as a structured `request_id` attribute when logging there. No request body, authorization header, cookie, or raw error string is included in the immediate HTTP stdout event. Unexpected plain Go errors are logged at error severity even if PocketBase converts their HTTP response to 400; explicit API errors use their response status for severity.

Before persistence, known password/token/secret/cookie/API-key fields and payload/body/query/header attributes are recursively redacted. URL query strings and fragments are stripped, and common credentials embedded in text are scrubbed. This is a best-effort guard: do not log customer content, raw requests, full records, or secrets in arbitrary messages. Stored request logs still follow PocketBase's IP and auth-ID settings. Review those settings for your project.

Application-log mirroring uses PocketBase's documented `_logs` model hook and therefore follows its batching: around three seconds after the last log write, at the batch threshold, or during graceful shutdown. Events filtered or lost before database persistence cannot be mirrored, so this is not a durable error-delivery guarantee. HTTP events are emitted immediately. PocketBase startup/CLI console messages may also appear in stdout and should be filtered by the collector's JSON parser.

With `SENTRY_DSN` set, the official [Sentry Go SDK](https://github.com/getsentry/sentry-go) reports unexpected handler errors, server error responses, panics, and `observability.Logger(e).Error` events immediately. The first error event per HTTP request is captured; a logger event, the HTTP error it describes, and the eventual database log copy produce one report. Exceptions retain diagnostic stack frames. Expected explicit HTTP 4xx errors are not automatically reported. Ordinary background application error logs also report after PocketBase persists them, including logs with a structured `request_id`; they follow PocketBase's logging threshold and retention settings. Request IDs appear in event context, rather than an indexed tag. `LOG_JSON=false` leaves Sentry enabled.

For startup or background errors that need an exception stack without relying on database log persistence, call `observability.CaptureError(app, err)`. Use one reporting path for a background failure: explicitly capturing and also logging the same error can produce two events. Graceful termination flushes after PocketBase cleanup, with a two-second limit; the main program also flushes startup failures. Production delivery uses a bounded asynchronous queue of 64 events, so request handlers do not wait for remote ingestion. A full queue, a hard process kill, or an unavailable endpoint can lose events. The integration is an error-reporting aid, rather than durable delivery. An empty DSN is a no-op; a malformed DSN or invalid sampling value disables reporting with a generic warning and lets the app start.

Remote events omit automatic request URLs, bodies, headers, cookies, identity, breadcrumbs, attachments, source snippets, local variables, and absolute source paths. Known credential and personal-data attribute names are recursively scrubbed, and exception messages use the same best-effort text redaction as local application logs. Arbitrary customer content or personal information inside free-form error messages cannot be reliably identified: keep messages and custom attributes safe to export. The standard DSN determines the endpoint, so self-hosted compatible services can be used; compatibility and delivery should be verified with the chosen server.

For centralized searches, request-rate/error-rate analytics, and notifications, configure [Grafana Alloy's journal reader](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/) to ship this service's JSON logs to your chosen backend. Configure alerts in that backend, for example a sustained increase in `level=ERROR` or `status>=500`, filtered by `service` and `environment`. Keep retention, credentials, rate limits, and notification destinations in deployment configuration.

Request IDs provide request correlation. Setting `SENTRY_TRACES_SAMPLE_RATE`, for example `0.1`, additionally reports sampled HTTP transactions named by route pattern. Incoming trace headers and baggage are not propagated, and database/provider spans are not automatically instrumented. Custom Sentry spans can use the handler's request context; keep names and data bounded and safe to export. Add OpenTelemetry HTTP/database/provider instrumentation and an OTLP exporter when a project needs broader cross-service traces; preserve request IDs so existing logs remain searchable. The [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) can receive and export that telemetry. PocketBase's hook and batching behavior are described in its [Go logging documentation](https://pocketbase.io/docs/go-logging/).
