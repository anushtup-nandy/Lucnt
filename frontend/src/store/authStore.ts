/**
 * Session state: the access token, the user, and their resolved permissions.
 *
 * **The access token lives in memory only**. Not
 * `localStorage`, not `sessionStorage`: both are readable by any script that
 * gets injected, and a token that outlives the tab is a token that outlives the
 * user's intent. The cost is that a hard refresh has no token — which is why
 * `bootstrap()` exists: it spends one silent `POST /auth/refresh` against the
 * httpOnly cookie to get a new one.
 */

import { create } from 'zustand'

export interface NavItem {
  key: string
  label: string
  path: string
}

export interface Perms {
  role: string
  nav: NavItem[]
  landing: string
  agent_scope: 'all' | 'owned' | string
  can_write_budgets: boolean
  can_write_policies: boolean
  can_approve: boolean
  can_read_books: boolean
}

export interface SessionUser {
  id: string
  email: string
  name: string
  role: string
  is_active: boolean
}

export interface Session {
  user: SessionUser
  companyId: string
  companyName: string
  perms: Perms
}

interface AuthState {
  accessToken: string | null
  session: Session | null
  /** `true` until the initial silent refresh has settled, either way. */
  bootstrapping: boolean
  setToken: (token: string | null) => void
  setSession: (session: Session | null) => void
  clear: () => void
  setBootstrapped: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  session: null,
  bootstrapping: true,
  setToken: (accessToken) => set({ accessToken }),
  setSession: (session) => set({ session }),
  clear: () => set({ accessToken: null, session: null }),
  setBootstrapped: () => set({ bootstrapping: false }),
}))

/**
 * Read the token outside React.
 *
 * The axios request interceptor needs it, and an interceptor is not a component
 * — it cannot call a hook. Reading the store imperatively is the supported
 * escape hatch, and it keeps exactly one copy of the token in the process.
 */
export const getAccessToken = (): string | null => useAuthStore.getState().accessToken
