/**
 * Login. On success, redirect to `perms.landing` — **not** a hardcoded `/`.
 *
 * Which page a role lands on is a product decision the backend already made
 * (`resolve_perms`); repeating it here would be a second source of truth that
 * silently disagrees the first time a role's landing page changes.
 */

import { useState, type FormEvent, type ReactNode } from 'react'
import { Navigate, useNavigate } from 'react-router'

import { describeFailure } from '@/api/client'
import { login } from '@/api/queries/auth'
import { useAuthStore } from '@/store/authStore'

export function Login(): ReactNode {
  const session = useAuthStore((s) => s.session)
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  if (session) return <Navigate to={session.perms.landing} replace />

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault()
    setPending(true)
    setError(null)
    login(email, password)
      .then((next) => navigate(next.perms.landing, { replace: true }))
      .catch((err: unknown) => setError(describeFailure(err).message))
      .finally(() => setPending(false))
  }

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={onSubmit}>
        <div className="brand">
          <span className="mark" aria-hidden />
          <span className="wm">LUCNT</span>
        </div>
        <h1>Sign in</h1>
        <p className="sub">Financial infrastructure for agent-initiated spend.</p>

        {error ? (
          <div className="err" role="alert">
            {error}
          </div>
        ) : null}

        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <button type="submit" className="btn prim" disabled={pending}>
          {pending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
