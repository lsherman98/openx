import { createLazyFileRoute, useNavigate } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { ClientResponseError } from 'pocketbase'
import type { AuthMethodsList } from 'pocketbase'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { pb } from '@/lib/pocketbase'
import { appConfig } from '@/lib/app-config'
import { getRedirectAfterSignIn } from '@/lib/auth'
import { errorMessage, isCancelled } from '@/lib/errors'
import { reportError } from '@/lib/telemetry'

export const Route = createLazyFileRoute('/signin')({ component: LoginForm })

type Mode = 'signin' | 'signup' | 'reset'

function LoginForm() {
  const navigate = useNavigate()
  const [methods, setMethods] = useState<AuthMethodsList | null>(null)
  const [methodsError, setMethodsError] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [mode, setMode] = useState<Mode>('signin')
  const [pending, setPending] = useState(false)

  useEffect(() => {
    let active = true
    pb.collection('users').listAuthMethods({ requestKey: null }).then((result) => {
      if (active) setMethods(result)
    }).catch((error: unknown) => {
      if (active && !isCancelled(error)) {
        reportError(error, 'auth-methods')
        setMethodsError(true)
      }
    })
    return () => { active = false }
  }, [attempt])

  async function finishSignIn() {
    // Invalidate protected loaders after the auth store changes.
    await navigate({ to: getRedirectAfterSignIn(), replace: true })
  }

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    const data = new FormData(event.currentTarget)
    const email = String(data.get('email') ?? '').trim()
    const password = String(data.get('password') ?? '')
    setPending(true)
    try {
      if (mode === 'reset') {
        await pb.collection('users').requestPasswordReset(email)
        toast.success('Check your inbox for a password reset link.')
        setMode('signin')
      } else {
        if (mode === 'signup') {
          await pb.collection('users').create({
            name: String(data.get('name') ?? '').trim(), email, password, passwordConfirm: password,
          })
        }
        await pb.collection('users').authWithPassword(email, password)
        await finishSignIn()
      }
    } catch (error: unknown) {
      if (isCancelled(error)) return
      reportError(error, `auth-${mode}`)
      let message = errorMessage(error, mode === 'signin'
        ? 'Unable to sign in. Check your email and password.'
        : 'Unable to complete the request. Please try again.')
      if (mode === 'signup' && error instanceof ClientResponseError) {
        const fields = error.response?.data
        if (fields?.email) message = 'Please use a valid, available email address.'
        else if (fields?.password) message = 'Password does not meet the requirements.'
      }
      toast.error(message)
    } finally { setPending(false) }
  }

  const hasPassword = methods?.password.enabled
  const providers = methods?.oauth2.providers ?? []
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardDescription>{appConfig.name}</CardDescription>
          <CardTitle className="text-2xl">{mode === 'signup' ? 'Create account' : mode === 'reset' ? 'Reset password' : 'Sign in'}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4">
          {methodsError && <div role="alert" className="grid gap-3 text-sm"><p>Unable to connect. Please try again.</p><Button variant="outline" onClick={() => {
            setMethodsError(false)
            setAttempt((value) => value + 1)
          }}>Retry</Button></div>}
          {mode !== 'reset' && providers.map((provider) => (
            <Button key={provider.name} disabled={pending} variant="outline" onClick={async () => {
              if (pending) return
              setPending(true)
              try {
                await pb.collection('users').authWithOAuth2({ provider: provider.name })
                await finishSignIn()
              } catch (error: unknown) {
                if (!isCancelled(error)) {
                  reportError(error, 'auth-oauth')
                  toast.error(errorMessage(error, 'Unable to sign in. Please try again.'))
                }
              } finally { setPending(false) }
            }}>Sign in with {provider.displayName}</Button>
          ))}
          {hasPassword && providers.length > 0 && mode !== 'reset' && <Separator />}
          {hasPassword && <form onSubmit={submit} className="grid gap-4">
            {mode === 'signup' && <div className="grid gap-2"><Label htmlFor="name">Name</Label><Input id="name" name="name" autoComplete="name" required disabled={pending} /></div>}
            <div className="grid gap-2"><Label htmlFor="email">Email</Label><Input id="email" type="email" name="email" autoComplete="email" required disabled={pending} /></div>
            {mode !== 'reset' && <div className="grid gap-2"><Label htmlFor="password">Password</Label><Input id="password" type="password" name="password" autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} minLength={mode === 'signup' ? 8 : undefined} required disabled={pending} /></div>}
            <Button type="submit" disabled={pending}>{mode === 'signup' ? 'Create account' : mode === 'reset' ? 'Send reset link' : 'Sign in'}</Button>
            <div className="flex flex-wrap justify-between gap-2">
              <Button variant="link" type="button" className="px-0" disabled={pending} onClick={() => setMode(mode === 'signin' ? 'reset' : 'signin')}>{mode === 'signin' ? 'Forgot password?' : 'Back to sign in'}</Button>
              {mode === 'signin' && <Button variant="link" type="button" disabled={pending} onClick={() => setMode('signup')}>Create an account</Button>}
            </div>
          </form>}
          {methods && !hasPassword && providers.length === 0 && <p role="alert" className="text-sm">Sign-in methods have not been configured.</p>}
        </CardContent>
      </Card>
    </main>
  )
}
