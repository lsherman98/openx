import { useEffect } from 'react'
import { Button } from '@/components/ui/button'
import { reportError } from '@/lib/telemetry'

export function ErrorPage({ error, reset }: { error: unknown; reset: () => void }) {
  useEffect(() => { reportError(error, 'route') }, [error])
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 className="text-xl font-semibold">Something went wrong</h1>
      <p className="text-sm text-muted-foreground">Please try again or reload the page.</p>
      <div className="flex gap-2">
        <Button onClick={reset}>Try again</Button>
        <Button variant="outline" onClick={() => location.reload()}>Reload</Button>
      </div>
    </main>
  )
}
