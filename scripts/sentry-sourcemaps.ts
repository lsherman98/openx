type UploadOptions = NonNullable<Parameters<typeof import('@sentry/vite-plugin')['sentryVitePlugin']>[0]>

// Build-only configuration: no upload or source maps without every credential.
export function sentrySourceMapOptions(env: Record<string, string>, building: boolean): UploadOptions | undefined {
  const names = ['SENTRY_AUTH_TOKEN', 'SENTRY_ORG', 'SENTRY_PROJECT', 'SENTRY_URL'] as const
  if (!building || !names.every(name => env[name]?.trim())) return undefined
  const endpoint = new URL(env.SENTRY_URL)
  if (!['https:', 'http:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error('SENTRY_URL must be an HTTP(S) server URL without credentials, query, or fragment.')
  }
  return {
    authToken: env.SENTRY_AUTH_TOKEN, org: env.SENTRY_ORG, project: env.SENTRY_PROJECT, url: endpoint.toString(),
    telemetry: false,
    release: { name: env.VITE_TELEMETRY_RELEASE || undefined },
    sourcemaps: { assets: './dist/**', filesToDeleteAfterUpload: './dist/**/*.map' },
    errorHandler: () => { throw new Error('Sentry source map upload failed. Check the build-only Sentry configuration.') },
  }
}
