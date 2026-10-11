// Package observability adds request correlation and structured stdout logs
// alongside PocketBase's database-backed logger.
package observability

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/pocketbase/pocketbase/apis"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/hook"
	"github.com/pocketbase/pocketbase/tools/router"
	"github.com/pocketbase/pocketbase/tools/types"
)

const (
	RequestIDHeader = "X-Request-ID"
	requestIDKey    = "observability_request_id"
	healthPath      = "/api/health"
)

var safeRequestID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// Config controls stdout forwarding. PocketBase's database retention and
// minimum log level are still controlled by its Dashboard > Settings > Logs.
type Config struct {
	Service     string
	Environment string
	Level       slog.Level
	JSON        bool
	Writer      io.Writer // defaults to stdout
	Sentry      SentryConfig
}

// ConfigFromEnv reads logging and optional SENTRY_* settings. Invalid logging
// settings fail startup; invalid Sentry settings disable only remote reporting.
func ConfigFromEnv() (Config, error) {
	config := Config{
		Service:     envDefault("LOG_SERVICE", "pocketbase-template"),
		Environment: envDefault("APP_ENV", "development"),
		JSON:        false,
		Sentry: SentryConfig{
			DSN:     strings.TrimSpace(os.Getenv("SENTRY_DSN")),
			Release: strings.TrimSpace(os.Getenv("SENTRY_RELEASE")),
		},
	}
	if value := strings.TrimSpace(os.Getenv("SENTRY_TRACES_SAMPLE_RATE")); value != "" {
		rate, err := strconv.ParseFloat(value, 64)
		config.Sentry.TracesSampleRate = rate
		config.Sentry.invalid = err != nil
	}
	if err := config.Level.UnmarshalText([]byte(envDefault("LOG_LEVEL", "info"))); err != nil {
		return Config{}, fmt.Errorf("LOG_LEVEL: %w", err)
	}
	if raw := os.Getenv("LOG_JSON"); raw != "" {
		value, err := strconv.ParseBool(raw)
		if err != nil {
			return Config{}, fmt.Errorf("LOG_JSON: %w", err)
		}
		config.JSON = value
	}
	return config, nil
}

func envDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

// Register must be called before app.Start. It retains the built-in logger and
// redacts known credential and request-payload attributes before persistence. Application
// logs are mirrored after PocketBase's normal batching delay; HTTP logs are
// emitted immediately and use route patterns rather than raw URLs.
func Register(app core.App, config Config) error {
	if app == nil {
		return fmt.Errorf("observability: app is required")
	}
	if config.Service == "" {
		config.Service = "pocketbase-template"
	}
	if config.Environment == "" {
		config.Environment = "development"
	}
	if config.Writer == nil {
		config.Writer = os.Stdout
	}
	output := slog.New(slog.NewJSONHandler(config.Writer, &slog.HandlerOptions{
		Level: config.Level,
	})).With("service", sanitizeString(config.Service), "environment", sanitizeString(config.Environment))
	reporter, err := newSentryReporter(config)
	if err != nil {
		// SDK configuration errors can contain the DSN. Never log their text.
		if config.JSON {
			output.Warn("Sentry disabled: invalid configuration", "type", "telemetry_config")
		} else {
			log.Print("Sentry disabled: invalid configuration")
		}
	}
	app.Store().Set(sentryReporterKey, reporter)
	if reporter != nil {
		app.OnTerminate().Bind(&hook.Handler[*core.TerminateEvent]{
			Id: "observability_sentry_flush",
			Func: func(e *core.TerminateEvent) error {
				// Next clears PocketBase's bootstrap state and flushes DB logs.
				// Flush Sentry afterwards so the final log batch is included.
				defer reporter.close()
				err := e.Next()
				if err != nil {
					reporter.hub.CaptureException(err)
				}
				return err
			},
		})
	}

	app.OnModelCreate(core.LogsTableName).Bind(&hook.Handler[*core.ModelEvent]{
		Id: "observability_logs",
		Func: func(e *core.ModelEvent) error {
			entry, ok := e.Model.(*core.Log)
			if !ok {
				return e.Next()
			}
			entry.Message = sanitizeString(entry.Message)
			entry.Data = types.JSONMap[any](sanitizeMap(map[string]any(entry.Data), 0))
			alreadyCaptured := popSentryCaptured(map[string]any(entry.Data))
			if err := e.Next(); err != nil {
				return err
			}
			if reporter != nil && entry.Level >= int(slog.LevelError) && entry.Data["type"] != "request" {
				// Logger(e) captures request logs immediately. Their persisted
				// copies must not generate another error event later.
				if !alreadyCaptured {
					reporter.captureLog(entry)
				}
			}
			// Our middleware emits request logs once, immediately. The built-in
			// activity logger continues storing its request records in the DB.
			if config.JSON && entry.Data["type"] != "request" {
				level := slog.Level(entry.Level)
				if output.Enabled(e.Context, level) {
					record := slog.NewRecord(entry.Created.Time(), level, entry.Message, 0)
					record.AddAttrs(slog.String("type", "application"), slog.String("log_id", entry.Id), slog.Any("data", entry.Data))
					if id, ok := entry.Data["request_id"].(string); ok && safeRequestID.MatchString(id) {
						record.AddAttrs(slog.String("request_id", id))
					}
					_ = output.Handler().Handle(e.Context, record)
				}
			}
			return nil
		},
	})

	app.OnServe().Bind(&hook.Handler[*core.ServeEvent]{
		Id: "observability_routes",
		Func: func(e *core.ServeEvent) error {
			e.Router.Bind(requestMiddleware(output, config.JSON))
			// Keep successful probes of PocketBase's built-in route out of
			// its database activity logs without registering another endpoint.
			skipSuccess := apis.SkipSuccessActivityLog()
			e.Router.Bind(&hook.Handler[*core.RequestEvent]{
				Id: "observability_health_activity",
				Func: func(request *core.RequestEvent) error {
					if request.Request.URL.Path == healthPath {
						return skipSuccess.Func(request)
					}
					return request.Next()
				},
			})
			if reporter != nil {
				e.Router.Bind(sentryPanicMiddleware())
			}
			return e.Next()
		},
	})
	return nil
}

// RequestID returns the correlation ID assigned by the middleware.
func RequestID(e *core.RequestEvent) string {
	value, _ := e.Get(requestIDKey).(string)
	return value
}

// Logger returns PocketBase's database-backed logger enriched with request_id.
// Prefer this over e.App.Logger() for custom request handler logs.
func Logger(e *core.RequestEvent) *slog.Logger {
	logger := e.App.Logger()
	if state, ok := e.Get(sentryRequestKey).(*sentryRequest); ok {
		logger = slog.New(&sentryLogHandler{next: logger.Handler(), state: state})
	}
	return logger.With("request_id", RequestID(e))
}

func requestMiddleware(output *slog.Logger, enabled bool) *hook.Handler[*core.RequestEvent] {
	return &hook.Handler[*core.RequestEvent]{
		Id: "observability_request",
		// Run outside PocketBase's redirect, activity, recovery, and rate-limit
		// middleware so redirects and rejected requests receive IDs too.
		Priority: apis.DefaultWWWRedirectMiddlewarePriority - 1,
		Func: func(e *core.RequestEvent) error {
			started := time.Now()
			id := e.Request.Header.Get(RequestIDHeader)
			if !safeRequestID.MatchString(id) || len(e.Request.Header.Values(RequestIDHeader)) != 1 {
				var err error
				id, err = newRequestID()
				if err != nil {
					return e.InternalServerError("", err)
				}
			}
			e.Set(requestIDKey, id)
			e.Response.Header().Set(RequestIDHeader, id)
			// Expose the ID to cross-origin SDK clients as well.
			e.Response.Header().Add("Access-Control-Expose-Headers", RequestIDHeader)
			meta := map[string]any{}
			if existing, ok := e.Get(apis.RequestEventKeyLogMeta).(map[string]any); ok {
				for key, value := range existing {
					meta[key] = value
				}
			}
			meta["request_id"] = id
			e.Set(apis.RequestEventKeyLogMeta, meta)
			var sentryState *sentryRequest
			if e.App != nil {
				if reporter, _ := e.App.Store().Get(sentryReporterKey).(*sentryReporter); reporter != nil {
					sentryState = reporter.startRequest(e)
					e.Set(sentryRequestKey, sentryState)
				}
			}

			err := e.Next()
			status := e.Status()
			if status == 0 {
				if err != nil {
					status = router.ToApiError(err).Status
				} else {
					status = http.StatusOK
				}
			}
			if sentryState != nil {
				sentryState.finish(e, status, err)
			}
			if !enabled || (e.Request.URL.Path == healthPath && status < http.StatusBadRequest) {
				return err
			}
			level := slog.LevelInfo
			if status >= http.StatusInternalServerError {
				level = slog.LevelError
			} else if status >= http.StatusBadRequest {
				level = slog.LevelWarn
			}
			if err != nil && status < http.StatusBadRequest {
				level = slog.LevelError
			}
			var apiError *router.ApiError
			if err != nil && !errors.As(err, &apiError) {
				// PocketBase maps plain errors to HTTP 400. Keep that response
				// behavior but surface unexpected application errors for alerts.
				level = slog.LevelError
			}
			attrs := []slog.Attr{
				slog.String("type", "request"),
				slog.String("request_id", id),
				slog.String("method", e.Request.Method),
				slog.String("route", e.Request.Pattern),
				slog.Int("status", status),
				slog.Float64("duration_ms", float64(time.Since(started))/float64(time.Millisecond)),
			}
			if err != nil {
				// Raw error strings can contain payloads or credentials. Custom
				// handler logs should supply safe, structured diagnostic attributes.
				attrs = append(attrs, slog.String("error_type", fmt.Sprintf("%T", err)))
			}
			output.LogAttrs(e.Request.Context(), level, "http_request", attrs...)
			return err
		},
	}
}

func newRequestID() (string, error) {
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return "", fmt.Errorf("generate request ID: %w", err)
	}
	return hex.EncodeToString(bytes[:]), nil
}
