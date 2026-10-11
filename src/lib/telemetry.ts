import { ClientResponseError } from 'pocketbase'
import { errorStatus, isCancelled } from './errors'

export interface TelemetryErrorEvent { name: string; source: string; status?: number; request_id?: string }
export type EventProperties = Record<string, string | number | boolean | null>

// Supply an adapter at startup for your chosen provider. No data leaves the app by default.
export interface TelemetryAdapter {
  error: (event: TelemetryErrorEvent) => void
  // Diagnostic adapters must scrub the original exception before sending it.
  exception: (error: unknown, event: TelemetryErrorEvent) => void
  page: (route: string) => void
  metric: (event: { name: string; value: number; rating: string }) => void
  event: (name: string, properties: EventProperties) => void
}

let adapter: Partial<TelemetryAdapter> = {}
const responseRequestIds = new WeakMap<object, string>()

export function rememberRequestId(response: unknown, requestId: string | null) {
  if (response && typeof response === 'object' && requestId && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(requestId)) {
    responseRequestIds.set(response, requestId)
  }
}

export function configureTelemetry(next: Partial<TelemetryAdapter>) {
  adapter = next
}

function emit(callback: (() => void) | undefined) {
  // An unavailable analytics provider must never break the application.
  try { callback?.() } catch { /* telemetry is best effort */ }
}

export function reportError(error: unknown, source: string) {
  if (isCancelled(error)) return
  // Deliberately omit messages, request URLs, payloads, and account data.
  const event: TelemetryErrorEvent = { name: error instanceof Error ? error.name : 'UnknownError', source, status: errorStatus(error) }
  if (error instanceof ClientResponseError) {
    const requestId = responseRequestIds.get(error.response)
    if (requestId) event.request_id = requestId
  }
  if (import.meta.env.DEV) console.error('Application error', event)
  emit(adapter.error ? () => adapter.error?.(event) : undefined)
  emit(adapter.exception ? () => adapter.exception?.(error, event) : undefined)
}

export function trackPage(route: string) {
  emit(adapter.page ? () => adapter.page?.(route) : undefined)
}

export function trackMetric(event: { name: string; value: number; rating: string }) {
  emit(adapter.metric ? () => adapter.metric?.(event) : undefined)
}

// Use fixed event names and categorical properties, never account/form values.
export function trackEvent(name: string, properties: EventProperties = {}) {
  emit(adapter.event ? () => adapter.event?.(name, properties) : undefined)
}
