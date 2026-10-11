/** Route guards. UX only, not security: the server enforces `require_nav`. */

import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'

import { useAuthStore } from '@/store/authStore'

function Splash(): ReactNode {
  return (
    <div className="login-wrap">
      <div className="state">
        <div className="st-t serif">LUCNT</div>
        <div>Restoring your session…</div>
      </div>
    </div>
  )
}

export function ProtectedRoute({ children }: { children: ReactNode }): ReactNode {
  const session = useAuthStore((s) => s.session)
  const bootstrapping = useAuthStore((s) => s.bootstrapping)
  // Wait for the silent refresh, or a reload briefly redirects to /login.
  if (bootstrapping) return <Splash />
  if (!session) return <Navigate to="/login" replace />
  return children
}

export function RoleGate({ navKey, children }: { navKey: string; children: ReactNode }): ReactNode {
  const perms = useAuthStore((s) => s.session?.perms)
  const { pathname } = useLocation()
  if (!perms) return null
  if (!perms.nav.some((item) => item.key === navKey)) {
    return (
      <div className="page">
        <div className="state" role="alert">
          <div className="st-t serif">Your role cannot see this page</div>
          <div>
            <code className="mono">{pathname}</code> is not available to the{' '}
            <b>{perms.role.replace('_', ' ')}</b> role.
          </div>
          <div className="st-d">the endpoint behind it refuses this role too</div>
        </div>
      </div>
    )
  }
  return children
}

/** Land on the role's own page rather than a hardcoded `/overview`. */
export function LandingRedirect(): ReactNode {
  const session = useAuthStore((s) => s.session)
  const bootstrapping = useAuthStore((s) => s.bootstrapping)
  if (bootstrapping) return <Splash />
  return <Navigate to={session ? session.perms.landing : '/login'} replace />
}
