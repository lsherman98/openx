package observability

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode"

	"github.com/getsentry/sentry-go"
	"github.com/pocketbase/pocketbase/apis"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/hook"
	"github.com/pocketbase/pocketbase/tools/router"
)

const (
	sentryReporterKey  = "observability_sentry_reporter"
	sentryRequestKey   = "observability_sentry_request"
	sentryCapturedKey  = "__sentry_captured"
	sentryFlushTimeout = 2 * time.Second
)

// SentryConfig enables optional error reporting to any Sentry-compatible DSN.
// Tracing is off unless TracesSampleRate is in (0, 1]. Transport is an injection
// point for offline tests; production uses a bounded asynchronous HTTP transport.
type SentryConfig struct {
	DSN              string
	Release          string
	TracesSampleRate float64
	Transport        sentry.Transport
	invalid          bool
}

type sentryReporter struct {
	client  *sentry.Client
	hub     *sentry.Hub
	trace   bool
	closed  sync.Once
	stopped atomic.Bool
}

func newSentryReporter(config Config) (*sentryReporter, error) {
	settings := config.Sentry
	if settings.DSN == "" {
		return nil, nil
	}
	if settings.invalid || math.IsNaN(settings.TracesSampleRate) || math.IsInf(settings.TracesSampleRate, 0) || settings.TracesSampleRate < 0 || settings.TracesSampleRate > 1 {
		return nil, errors.New("invalid Sentry configuration")
	}
	transport := settings.Transport
	if transport == nil {
		asynchronous := sentry.NewHTTPTransport()
		asynchronous.BufferSize = 64
		asynchronous.Timeout = sentryFlushTimeout
		transport = asynchronous
	}
	client, err := sentry.NewClient(sentry.ClientOptions{
		Dsn: settings.DSN, Release: settings.Release, Environment: config.Environment,
		Transport: transport, DisableTelemetryBuffer: true,
		EnableTracing: settings.TracesSampleRate > 0, TracesSampleRate: settings.TracesSampleRate,
		SendDefaultPII: false,
		DataCollection: &sentry.DataCollection{
			UserInfo: sentry.Set(false), HTTPBodies: []sentry.BodyType{},
			Cookies:     &sentry.KeyValueCollectionBehavior{Mode: sentry.CollectionOff},
			QueryParams: &sentry.KeyValueCollectionBehavior{Mode: sentry.CollectionOff},
			HTTPHeaders: &sentry.HeaderCollectionConfig{
				Request:  &sentry.KeyValueCollectionBehavior{Mode: sentry.CollectionOff},
				Response: &sentry.KeyValueCollectionBehavior{Mode: sentry.CollectionOff},
			},
		},
		BeforeSend: sanitizeSentryEvent, BeforeSendTransaction: sanitizeSentryEvent,
	})
	if err != nil {
		return nil, errors.New("invalid Sentry configuration")
	}
	scope := sentry.NewScope()
	scope.SetTag("service", sanitizeString(config.Service))
	return &sentryReporter{client: client, hub: sentry.NewHub(client, scope), trace: settings.TracesSampleRate > 0}, nil
}

func (reporter *sentryReporter) close() {
	reporter.closed.Do(func() {
		reporter.stopped.Store(true)
		reporter.client.Flush(sentryFlushTimeout)
		reporter.client.Close()
	})
}

// CaptureError reports a background or startup error without waiting for network
// delivery. Prefer Logger(e) inside HTTP handlers for correlation and deduplication.
// With no valid SENTRY_DSN configured, it does nothing.
func CaptureError(app core.App, err error) {
	if app == nil || err == nil {
		return
	}
	if reporter, _ := app.Store().Get(sentryReporterKey).(*sentryReporter); reporter != nil && !reporter.stopped.Load() {
		reporter.hub.CaptureException(err)
	}
}

// Flush waits at most timeout for already queued Sentry events. It is intended
// for an explicit process exit, never for an HTTP request handler.
func Flush(app core.App, timeout time.Duration) bool {
	if app == nil {
		return true
	}
	if reporter, _ := app.Store().Get(sentryReporterKey).(*sentryReporter); reporter != nil && !reporter.stopped.Load() {
		return reporter.client.Flush(timeout)
	}
	return true
}

func (reporter *sentryReporter) captureLog(entry *core.Log) {
	if reporter.stopped.Load() {
		return
	}
	event := sentry.NewEvent()
	event.Message, event.Level = entry.Message, sentry.LevelError
	event.Contexts["diagnostic"] = map[string]any(entry.Data)
	if id, ok := entry.Data["request_id"].(string); ok && safeRequestID.MatchString(id) {
		event.Contexts["request"] = sentry.Context{"request_id": id}
	}
	reporter.hub.CaptureEvent(event)
}

type sentryRequest struct {
	hub      *sentry.Hub
	span     *sentry.Span
	captured atomic.Bool
}

func (reporter *sentryReporter) startRequest(e *core.RequestEvent) *sentryRequest {
	hub := reporter.hub.Clone()
	hub.Scope().SetContext("request", sentry.Context{
		"request_id": RequestID(e), "route": e.Request.Pattern, "method": e.Request.Method,
	})
	state := &sentryRequest{hub: hub}
	ctx := sentry.SetHubOnContext(e.Request.Context(), hub)
		if reporter.trace && e.Request.URL.Path != healthPath {
		// Route patterns bound cardinality. Request headers/body/URL are never
		// attached; incoming baggage and sampling decisions are not trusted.
		state.span = sentry.StartTransaction(ctx, e.Request.Pattern, sentry.WithOpName("http.server"), sentry.WithTransactionSource(sentry.SourceRoute))
		hub.Scope().SetSpan(state.span)
		ctx = state.span.Context()
	}
	e.Request = e.Request.WithContext(ctx)
	return state
}

func (state *sentryRequest) capture(err error, message string, data map[string]any) {
	if !state.captured.CompareAndSwap(false, true) {
		return
	}
	event := sentry.NewEvent()
	event.Level, event.Message = sentry.LevelError, sanitizeString(message)
	if err != nil {
		event.SetException(err, 10)
	}
	if data != nil {
		event.Contexts["diagnostic"] = sanitizeMap(data, 0)
	}
	state.hub.CaptureEvent(event)
}

func (state *sentryRequest) finish(e *core.RequestEvent, status int, err error) {
	state.hub.Scope().SetContext("request", sentry.Context{
		"request_id": RequestID(e), "route": e.Request.Pattern, "method": e.Request.Method, "status": status,
	})
	var apiError *router.ApiError
	unexpected := err != nil && !errors.As(err, &apiError)
	if status >= 500 || unexpected || (err != nil && status < 400) {
		diagnostic := err
		if apiError != nil {
			if original, ok := apiError.RawData().(error); ok {
				diagnostic = original
			}
		}
		state.capture(diagnostic, "HTTP request failed", nil)
	}
	if state.span != nil {
		state.span.Status = sentry.HTTPtoSpanStatus(status)
		state.span.SetData("http.response.status_code", status)
		state.span.SetContext("request", sentry.Context{"request_id": RequestID(e)})
		state.span.Finish()
	}
}

// This middleware runs inside PocketBase's recovery middleware, capturing the
// original panic stack before rethrowing it for PocketBase to handle normally.
func sentryPanicMiddleware() *hook.Handler[*core.RequestEvent] {
	return &hook.Handler[*core.RequestEvent]{
		Id: "observability_sentry_panic", Priority: apis.DefaultPanicRecoverMiddlewarePriority + 1,
		Func: func(e *core.RequestEvent) error {
			defer func() {
				if value := recover(); value != nil {
					err, ok := value.(error)
					if !ok {
						err = fmt.Errorf("%v", value)
					}
					if !errors.Is(err, http.ErrAbortHandler) {
						if state, ok := e.Get(sentryRequestKey).(*sentryRequest); ok {
							state.capture(err, "HTTP handler panic", nil)
						}
					}
					panic(value)
				}
			}()
			return e.Next()
		},
	}
}

type sentryLogHandler struct {
	next   slog.Handler
	state  *sentryRequest
	attrs  []slog.Attr
	groups []string
}

func (handler *sentryLogHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return level >= slog.LevelError || handler.next.Enabled(ctx, level)
}

func (handler *sentryLogHandler) Handle(ctx context.Context, record slog.Record) error {
	if record.Level >= slog.LevelError {
		values := map[string]any{}
		var original error
		appendAttr := func(attr slog.Attr) {
			collectSentryAttr(values, attr, &original)
		}
		for _, attr := range handler.attrs {
			appendAttr(attr)
		}
		record.Attrs(func(attr slog.Attr) bool {
			appendAttr(wrapSentryAttr(attr, handler.groups))
			return true
		})
		handler.state.capture(original, record.Message, values)
	}
	if handler.next.Enabled(ctx, record.Level) {
		if record.Level >= slog.LevelError {
			record = record.Clone()
			record.AddAttrs(slog.Bool(sentryCapturedKey, true))
		}
		return handler.next.Handle(ctx, record)
	}
	return nil
}

// Remove our internal marker before database persistence, including markers
// nested by slog.WithGroup. Ordinary background request_id logs still forward.
func popSentryCaptured(values map[string]any) bool {
	captured, _ := values[sentryCapturedKey].(bool)
	delete(values, sentryCapturedKey)
	for _, value := range values {
		if nested, ok := value.(map[string]any); ok {
			captured = popSentryCaptured(nested) || captured
		}
	}
	return captured
}

func (handler *sentryLogHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	copy := *handler
	copy.next = handler.next.WithAttrs(attrs)
	copy.attrs = append([]slog.Attr(nil), handler.attrs...)
	for _, attr := range attrs {
		copy.attrs = append(copy.attrs, wrapSentryAttr(attr, handler.groups))
	}
	return &copy
}

func (handler *sentryLogHandler) WithGroup(name string) slog.Handler {
	if name == "" {
		return handler
	}
	copy := *handler
	copy.next = handler.next.WithGroup(name)
	copy.groups = append(append([]string(nil), handler.groups...), name)
	return &copy
}

func wrapSentryAttr(attr slog.Attr, groups []string) slog.Attr {
	for index := len(groups) - 1; index >= 0; index-- {
		attr = slog.Attr{Key: groups[index], Value: slog.GroupValue(attr)}
	}
	return attr
}

func collectSentryAttr(values map[string]any, attr slog.Attr, original *error) {
	attr.Value = attr.Value.Resolve()
	if attr.Value.Kind() == slog.KindGroup {
		nested := values
		if attr.Key != "" {
			nested, _ = values[attr.Key].(map[string]any)
			if nested == nil {
				nested = map[string]any{}
				values[attr.Key] = nested
			}
		}
		for _, child := range attr.Value.Group() {
			collectSentryAttr(nested, child, original)
		}
		return
	}
	value := attr.Value.Any()
	if err, ok := value.(error); ok && *original == nil {
		*original = err
	}
	values[attr.Key] = value
}

func sanitizeSentryEvent(event *sentry.Event, _ *sentry.EventHint) *sentry.Event {
	// Strip automatically collected identity/request/source content entirely.
	event.User = sentry.User{}
	event.Request, event.Breadcrumbs, event.Attachments = nil, nil, nil
	event.ServerName = ""
	event.DebugMeta = nil
	event.Message = sanitizeString(event.Message)
	event.Release = sanitizeString(event.Release)
	event.Environment = sanitizeString(event.Environment)
	for key, value := range event.Tags {
		if sensitiveKey(key) || sentryPIIKey(key) || key == "request_id" {
			delete(event.Tags, key)
		} else {
			event.Tags[key] = sanitizeString(value)
		}
	}
	for name, values := range event.Contexts {
		if name == "device" || name == "user" || sensitiveKey(name) {
			delete(event.Contexts, name)
		} else {
			event.Contexts[name] = sanitizeSentryData(values)
		}
	}
	for index := range event.Exception {
		event.Exception[index].Value = sanitizeString(event.Exception[index].Value)
		sanitizeSentryStack(event.Exception[index].Stacktrace)
	}
	for index := range event.Threads {
		sanitizeSentryStack(event.Threads[index].Stacktrace)
	}
	for _, span := range event.Spans {
		span.Data = sanitizeSentryData(span.Data)
		span.Description = sanitizeString(span.Description)
		for name, value := range span.Tags {
			if sensitiveKey(name) || sentryPIIKey(name) || name == "request_id" {
				delete(span.Tags, name)
			} else {
				span.Tags[name] = sanitizeString(value)
			}
		}
	}
	return event
}

func sentryPIIKey(key string) bool {
	key = strings.Map(func(value rune) rune {
		if unicode.IsLetter(value) || unicode.IsDigit(value) {
			return unicode.ToLower(value)
		}
		return -1
	}, key)
	switch key {
	case "email", "useremail", "userip", "remoteip", "ipaddress", "authid", "userid", "username", "phone", "phonenumber":
		return true
	default:
		return false
	}
}

func sanitizeSentryData(values map[string]any) map[string]any {
	result := sanitizeMap(values, 0)
	var scrub func(any)
	scrub = func(value any) {
		switch typed := value.(type) {
		case map[string]any:
			for key, item := range typed {
				if sentryPIIKey(key) {
					typed[key] = redacted
				} else {
					scrub(item)
				}
			}
		case []any:
			for _, item := range typed {
				scrub(item)
			}
		}
	}
	scrub(result)
	return result
}

func sanitizeSentryStack(stack *sentry.Stacktrace) {
	if stack == nil {
		return
	}
	for index := range stack.Frames {
		frame := &stack.Frames[index]
		frame.AbsPath, frame.Vars, frame.PreContext, frame.PostContext, frame.ContextLine = "", nil, nil, nil, ""
		frame.Filename = filepath.Base(frame.Filename)
	}
}
