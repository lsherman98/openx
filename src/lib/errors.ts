import { ClientResponseError } from 'pocketbase'

export function isCancelled(error: unknown): boolean {
  return error instanceof ClientResponseError && error.isAbort
}

export function errorStatus(error: unknown): number | undefined {
  return error instanceof ClientResponseError ? error.status : undefined
}

export function errorMessage(error: unknown, fallback = 'Something went wrong. Please try again.'): string {
  const status = errorStatus(error)
  if (status === 0) return 'Unable to connect. Check your connection and try again.'
  if (status === 401) return 'Please sign in to continue.'
  if (status === 403) return 'You do not have permission to do that.'
  if (status === 404) return 'The requested item could not be found.'
  if (status === 429) return 'Too many requests. Please try again shortly.'
  // Unexpected provider/database messages belong in diagnostics, not the UI.
  return fallback
}

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isCancelled(error)) return false
  const status = errorStatus(error)
  if (status && status >= 400 && status < 500) return false
  return failureCount < 2
}
