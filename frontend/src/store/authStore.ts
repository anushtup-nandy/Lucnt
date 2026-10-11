/**
 * Session state. The access token is memory-only (web storage is XSS-readable);
 * `bootstrap()` restores it from the httpOnly refresh cookie.
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

/** Read the token outside React (for the axios interceptor). */
export const getAccessToken = (): string | null => useAuthStore.getState().accessToken
