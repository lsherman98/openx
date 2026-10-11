import type { ErrorEvent, StackFrame } from '@sentry/react'
import type { CaptureResult, PostHogConfig } from 'posthog-js'
import { appConfig } from './app-config'
import { configureTelemetry, type EventProperties, type TelemetryAdapter, type TelemetryErrorEvent } from './telemetry'

type SentrySdk = Pick<typeof import('@sentry/react'), 'init' | 'captureException' | 'browserTracingIntegration' | 'close'>
type StreamedSpanJSON = Parameters<NonNullable<NonNullable<Parameters<SentrySdk['init']>[0]>['beforeSendSpan']>>[0]
interface AnalyticsClient {
  capture: (name: string, properties?: Record<string, unknown>) => unknown
  opt_out_capturing: () => void
  opt_in_capturing: () => void
}
interface Loaders {
  sentry: () => Promise<SentrySdk>
  posthog: () => Promise<{ default: { init: (key: string, config: Partial<PostHogConfig>, name?: string) => AnalyticsClient | undefined } }>
}
interface BrowserContext {
  available: boolean
  doNotTrack: boolean
  storage?: Pick<Storage, 'getItem' | 'setItem'>
}
export interface TelemetryConfig {
  enabled: boolean
  development: boolean
  allowDevelopment: boolean
  sentryDsn: string
  traceSampleRate: number
  environment: string
  release?: string
  posthogKey: string
  posthogHost: string
  analyticsConsent: boolean
  consentKey: string
}

const loaders: Loaders = {
  sentry: () => import('@sentry/react'),
  posthog: () => import('posthog-js'),
}
const safeLabel = (value: unknown, fallback = 'unknown') => typeof value === 'string' && /^[A-Za-z0-9_ .:/@$()-]{1,128}$/.test(value) && !value.includes('@') ? value : fallback
const safeRoute = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_/$().:-]{1,128}$/.test(value) ? value : '/'
const safeId = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value) ? value : undefined
const safeUuid = (value: unknown) => typeof value === 'string' && /^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.test(value) ? value : undefined
const sensitiveKey = /email|name|user|token|password|auth|cookie|header|body|form|url|query|search|referr|phone|address|(^|_)ip($|_)|session/i

export function sanitizeProperties(properties: Record<string, unknown>): EventProperties {
  const result: EventProperties = {}
  for (const [key, value] of Object.entries(properties).slice(0, 32)) {
    if (!/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(key) || sensitiveKey.test(key)) continue
    if (value === null || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) result[key] = value
    else if (typeof value === 'string' && value.length <= 80 && !/[@?#]|https?:|[\r\n]/i.test(value)) result[key] = value
  }
  return result
}

function safeCodeFile(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const path = value.split(/[?#]/)[0].replace(/^https?:\/\/[^/]+/, '')
  return /^[A-Za-z0-9_./@-]+\.(?:[cm]?js|jsx|ts|tsx)$/.test(path) ? path : undefined
}

function sanitizeFrame(frame: StackFrame): StackFrame {
  return {
    filename: safeCodeFile(frame.filename),
    abs_path: safeCodeFile(frame.abs_path),
    function: typeof frame.function === 'string' && /^(?:async )?[A-Za-z_$][\w$]*(?:\.[\w$]+)*$/.test(frame.function) ? frame.function : undefined,
    lineno: frame.lineno,
    colno: frame.colno,
    in_app: frame.in_app,
    debug_id: safeUuid(frame.debug_id),
  }
}

export function sanitizeSentryEvent(event: ErrorEvent): ErrorEvent {
  const name = safeLabel(event.tags?.error_type, 'Error')
  const source = safeLabel(event.tags?.source)
  const requestId = safeId(event.tags?.request_id)
  const status = typeof event.tags?.status === 'number' && Number.isInteger(event.tags.status) && event.tags.status >= 0 && event.tags.status <= 599 ? event.tags.status : undefined
  return {
    type: undefined,
    event_id: event.event_id,
    timestamp: event.timestamp,
    level: 'error',
    platform: 'javascript',
    release: event.release,
    environment: event.environment,
    fingerprint: ['{{ default }}', source, String(status ?? '')],
    tags: { error_type: name, source, ...(status !== undefined ? { status } : {}), ...(requestId ? { request_id: requestId } : {}) },
    exception: { values: event.exception?.values?.slice(0, 3).map(exception => ({
      type: safeLabel(exception.type, name),
      value: name,
      stacktrace: { frames: exception.stacktrace?.frames?.slice(-50).map(sanitizeFrame) },
    })) },
    contexts: event.contexts?.trace ? { trace: {
      trace_id: event.contexts.trace.trace_id,
      span_id: event.contexts.trace.span_id,
      parent_span_id: event.contexts.trace.parent_span_id,
    } } : undefined,
    debug_meta: { images: event.debug_meta?.images?.flatMap(image => {
      if (image.type !== 'sourcemap') return []
      const codeFile = safeCodeFile(image.code_file)
      const debugId = safeUuid(image.debug_id)
      return codeFile && debugId ? [{ type: 'sourcemap' as const, code_file: codeFile, debug_id: debugId }] : []
    }) },
  }
}

export function sanitizeSentrySpan(span: StreamedSpanJSON, route: string): StreamedSpanJSON {
  const operation = safeLabel(span.attributes['sentry.op'], 'browser')
  return {
    trace_id: span.trace_id, span_id: span.span_id, parent_span_id: span.parent_span_id,
    start_timestamp: span.start_timestamp, end_timestamp: span.end_timestamp,
    status: span.status, is_segment: span.is_segment,
    name: span.is_segment ? safeRoute(route) : operation,
    attributes: { 'sentry.op': operation, 'app.route': safeRoute(route) },
  }
}

function validEndpoint(value: string, dsn = false): boolean {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !!url.hostname && !url.password && !url.search && !url.hash && (dsn ? !!url.username && url.pathname !== '/' : !url.username)
  } catch { return false }
}

export function telemetryConfigFromEnv(env: Record<string, unknown>): TelemetryConfig {
  const sample = Number(env.VITE_SENTRY_TRACES_SAMPLE_RATE ?? 0)
  return {
    enabled: env.VITE_TELEMETRY_ENABLED !== 'false', development: env.DEV === true,
    allowDevelopment: env.VITE_TELEMETRY_IN_DEV === 'true',
    sentryDsn: String(env.VITE_SENTRY_DSN ?? '').trim(),
    traceSampleRate: Number.isFinite(sample) && sample >= 0 && sample <= 1 ? sample : 0,
    environment: safeLabel(env.VITE_TELEMETRY_ENVIRONMENT ?? env.MODE, 'production'),
    release: typeof env.VITE_TELEMETRY_RELEASE === 'string' && /^[A-Za-z0-9_.@/-]{1,128}$/.test(env.VITE_TELEMETRY_RELEASE) ? env.VITE_TELEMETRY_RELEASE : undefined,
    posthogKey: String(env.VITE_POSTHOG_KEY ?? '').trim(), posthogHost: String(env.VITE_POSTHOG_HOST ?? '').trim(),
    analyticsConsent: env.VITE_ANALYTICS_CONSENT === 'granted', consentKey: `${appConfig.slug}:analytics-consent`,
  }
}

export function createTelemetryController(config: TelemetryConfig, browser: BrowserContext, sdkLoaders: Loaders = loaders) {
  let enabled = config.enabled
  let consent = config.analyticsConsent
  try { const saved = browser.storage?.getItem(config.consentKey); if (saved) consent = saved === 'granted' } catch { /* storage may be unavailable */ }
  let sentry: SentrySdk | undefined
  let analytics: AnalyticsClient | undefined
  let loadingSentry: Promise<void> | undefined
  let loadingAnalytics: Promise<void> | undefined
  let route = '/'
  let hasRoute = false
  const seenErrors = new WeakSet<object>()
  const pendingErrors: { error: unknown; event: TelemetryErrorEvent }[] = []
  const explicitEvents = new Map<string, Set<string>>()
  const allowed = () => enabled && browser.available && (!config.development || config.allowDevelopment)
  const analyticsAllowed = () => allowed() && consent && !browser.doNotTrack
  const captureError = (error: unknown, event: TelemetryErrorEvent) => sentry?.captureException(error instanceof Error ? error : new Error('UnknownError'), {
    tags: { error_type: safeLabel(event.name, 'Error'), source: safeLabel(event.source), status: event.status, request_id: safeId(event.request_id) },
  })
  const capture = (name: string, properties: EventProperties) => {
    if (!analytics || !analyticsAllowed()) return
    explicitEvents.set(name, new Set(Object.keys(properties)))
    analytics.capture(name, properties)
  }
  const beforeAnalyticsSend = (event: CaptureResult | null): CaptureResult | null => {
    if (!event || !analyticsAllowed() || !explicitEvents.has(event.event)) return null
    const properties: Record<string, unknown> = { token: config.posthogKey, $process_person_profile: false, $geoip_disable: true }
    for (const key of ['distinct_id', '$session_id']) {
      if (typeof event.properties[key] === 'string' && /^[0-9a-f-]{32,36}$/i.test(event.properties[key])) properties[key] = event.properties[key]
    }
    const permitted = explicitEvents.get(event.event)!
    const candidates: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(event.properties)) if (permitted.has(key)) candidates[key] = value
    Object.assign(properties, sanitizeProperties(candidates))
    return { uuid: event.uuid, event: event.event, timestamp: event.timestamp, properties }
  }
  const startSentry = () => {
    if (!allowed() || sentry || !validEndpoint(config.sentryDsn, true)) return Promise.resolve()
    if (loadingSentry) return loadingSentry
    loadingSentry = (async () => {
      const sdk = await sdkLoaders.sentry()
      if (!allowed()) return
      sdk.init({
        dsn: config.sentryDsn, environment: config.environment, release: config.release,
        defaultIntegrations: false, beforeSendLog: () => null, beforeSendMetric: () => null,
        dataCollection: { userInfo: false, cookies: false, httpHeaders: false, httpBodies: [], urlQueryParams: false,
          graphQL: { document: false, variables: false }, genAI: { inputs: false, outputs: false },
          databaseQueryData: false, queues: false, stackFrameVariables: false, frameContextLines: 0 },
        tracesSampleRate: config.traceSampleRate, tracePropagationTargets: [],
        integrations: config.traceSampleRate > 0 ? [sdk.browserTracingIntegration({ enableInp: false })] : [],
        beforeSend: event => allowed() ? sanitizeSentryEvent(event) : null,
        beforeSendSpan: span => sanitizeSentrySpan(span, route),
      })
      sentry = sdk
      for (const queued of pendingErrors.splice(0)) { try { captureError(queued.error, queued.event) } catch { /* best effort */ } }
    })().catch(() => { pendingErrors.length = 0 /* provider startup must not break the app */ }).finally(() => { loadingSentry = undefined })
    return loadingSentry
  }
  const startAnalytics = () => {
    if (!analyticsAllowed() || analytics || !config.posthogKey || !validEndpoint(config.posthogHost)) return Promise.resolve()
    if (loadingAnalytics) return loadingAnalytics
    loadingAnalytics = (async () => {
      const sdk = await sdkLoaders.posthog()
      if (!analyticsAllowed()) return
      analytics = sdk.default.init(config.posthogKey, {
        api_host: config.posthogHost, persistence: 'memory', person_profiles: 'never', ip: false, respect_dnt: true,
        autocapture: false, capture_pageview: false, capture_pageleave: false, disable_session_recording: true,
        capture_exceptions: false, capture_performance: false, capture_heatmaps: false, capture_dead_clicks: false,
        disable_scroll_properties: true, disable_external_dependency_loading: true, opt_in_site_apps: false,
        advanced_disable_flags: true, advanced_disable_feature_flags: true, advanced_disable_feature_flags_on_first_load: true,
        disable_surveys: true, remote_config_refresh_interval_ms: 0, before_send: beforeAnalyticsSend,
        on_request_error: () => {},
      }, 'appAnalytics')
      if (hasRoute) capture('$pageview', { route })
    })().catch(() => { /* analytics is best effort */ }).finally(() => { loadingAnalytics = undefined })
    return loadingAnalytics
  }
  const start = async () => { await Promise.allSettled([startSentry(), startAnalytics()]) }
  const adapter: Partial<TelemetryAdapter> = {
    exception: (error, event: TelemetryErrorEvent) => {
      if (!allowed() || !validEndpoint(config.sentryDsn, true)) return
      if (event.status !== undefined && event.status >= 400 && event.status < 500) return
      if (error && typeof error === 'object') { if (seenErrors.has(error)) return; seenErrors.add(error) }
      if (sentry) captureError(error, event)
      else if (loadingSentry && pendingErrors.length < 10) pendingErrors.push({ error, event })
    },
    page: next => { route = safeRoute(next); hasRoute = true; capture('$pageview', { route }) },
    metric: event => {
      if (Number.isFinite(event.value)) capture('web_vital', { metric: safeLabel(event.name), value: event.value, rating: safeLabel(event.rating) })
    },
    event: (name, properties) => { if (/^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/.test(name)) capture(name, sanitizeProperties(properties)) },
  }
  return {
    adapter, start,
    async setAnalyticsConsent(granted: boolean) {
      consent = granted
      try { browser.storage?.setItem(config.consentKey, granted ? 'granted' : 'denied') } catch { /* optional storage */ }
      if (!analyticsAllowed()) { try { analytics?.opt_out_capturing() } catch { /* provider may be unavailable */ } }
      else if (analytics) { try { analytics.opt_in_capturing() } catch { /* provider may be unavailable */ } }
      else await startAnalytics()
    },
    async setEnabled(next: boolean) {
      enabled = next && config.enabled
      if (!enabled) {
        pendingErrors.length = 0
        try { analytics?.opt_out_capturing() } catch { /* best effort */ }
        const previous = sentry; sentry = undefined
        try { await previous?.close(0) } catch { /* best effort */ }
      } else {
        try { if (analytics && analyticsAllowed()) analytics.opt_in_capturing() } catch { /* best effort */ }
        await start()
      }
    },
  }
}

let controller: ReturnType<typeof createTelemetryController> | undefined
function getController() {
  if (!controller) {
    let storage: BrowserContext['storage']
    try { storage = globalThis.localStorage } catch { /* blocked storage */ }
    const doNotTrack = typeof navigator !== 'undefined' && (navigator.doNotTrack === '1' || navigator.doNotTrack === 'yes')
    controller = createTelemetryController(telemetryConfigFromEnv(import.meta.env), { available: typeof window !== 'undefined', doNotTrack, storage })
    configureTelemetry(controller.adapter)
  }
  return controller
}
export async function initializeTelemetry() { await getController().start() }
export async function setAnalyticsConsent(granted: boolean) { await getController().setAnalyticsConsent(granted) }
export async function setTelemetryEnabled(enabled: boolean) { await getController().setEnabled(enabled) }
