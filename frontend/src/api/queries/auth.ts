/** Auth calls: login, the bootstrap refresh, `/auth/me`, logout. */

import { api, refreshAccessToken } from '@/api/client'
import { useAuthStore, type Perms, type Session, type SessionUser } from '@/store/authStore'

interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}

interface MeResponse {
  user: SessionUser
  company_id: string
  company_name: string
  perms: Perms
}

/** Fetch `/auth/me` and store the resolved session. */
export async function loadSession(): Promise<Session> {
  const { data } = await api.get<MeResponse>('/auth/me')
  const session: Session = {
    user: data.user,
    companyId: data.company_id,
    companyName: data.company_name,
    perms: data.perms,
  }
  useAuthStore.getState().setSession(session)
  return session
}

/** Exchange credentials for a token, then load the session behind it. */
export async function login(email: string, password: string): Promise<Session> {
  const { data } = await api.post<TokenResponse>('/auth/login', { email, password })
  useAuthStore.getState().setToken(data.access_token)
  return loadSession()
}

/** Restore a session on cold load via the refresh cookie; failure just means logged out. */
export async function bootstrapSession(): Promise<Session | null> {
  const store = useAuthStore.getState()
  try {
    const token = await refreshAccessToken()
    if (!token) return null
    return await loadSession()
  } catch {
    store.clear()
    return null
  } finally {
    useAuthStore.getState().setBootstrapped()
  }
}

/** Clear the cookie server-side, then drop all local session state. */
export async function logout(): Promise<void> {
  try {
    await api.post('/auth/logout')
  } finally {
    useAuthStore.getState().clear()
  }
}
