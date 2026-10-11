/**
 * The one HTTP client. Access-token injection, 401 → single silent refresh.
 *
 * **One refresh, not N**. A dashboard page fires five
 * or six queries at once; when the access token expires they all 401 within a
 * few milliseconds. Without the shared `refreshInFlight` promise below, each
 * would start its own refresh, and because `/auth/refresh` rotates the cookie,
 * the later ones would present a rotated-away token and log the user out — a
 * bug that only appears after fifteen idle minutes, i.e. mid-demo.
 */

import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'

import { getAccessToken, useAuthStore } from '@/store/authStore'

export const API_BASE_URL: string =
  (import.meta.env['VITE_API_BASE_URL'] as string | undefined) ?? 'http://localhost:8000'

/** Header the backend stamps on every response; shown in 5xx states (§11). */
export const REQUEST_ID_HEADER = 'x-request-id'

export const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  // Required for the refresh cookie to be sent cross-origin. Pairs with the
  // backend's exact `CORS_ORIGINS` — a wildcard origin would be rejected here.
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
})

/**
 * A bare client for the refresh call itself, so it can never recurse.
 *
 * Exported only so tests can mount a mock adapter on it — without that seam a
 * refresh test makes a real socket connection to a backend that is not running,
 * which is slow, noisy, and tests the wrong thing.
 */
export const refreshClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  withCredentials: true,
})

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken()
  if (token) config.headers.set('Authorization', `Bearer ${token}`)
  return config
})

let refreshInFlight: Promise<string | null> | null = null

/**
 * Exchange the httpOnly refresh cookie for a new access token.
 *
 * Concurrent callers share one in-flight request; the promise is cleared only
 * once it settles, so the *next* expiry starts a fresh one.
 */
export function refreshAccessToken(): Promise<string | null> {
  refreshInFlight ??= refreshClient
    .post<{ access_token: string }>('/auth/refresh')
    .then((response) => {
      const token = response.data.access_token
      useAuthStore.getState().setToken(token)
      return token
    })
    .catch(() => {
      useAuthStore.getState().clear()
      return null
    })
    .finally(() => {
      refreshInFlight = null
    })
  return refreshInFlight
}

interface RetriableConfig extends InternalAxiosRequestConfig {
  _retried?: boolean
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RetriableConfig | undefined
    const status = error.response?.status

    // One retry, and never for the auth endpoints themselves: a 401 from
    // /auth/login means "wrong password", and refreshing on it would turn a
    // failed login into an infinite loop.
    const isAuthCall = config?.url?.startsWith('/auth/') ?? false
    if (status === 401 && config && !config._retried && !isAuthCall) {
      config._retried = true
      const token = await refreshAccessToken()
      if (token) {
        config.headers.set('Authorization', `Bearer ${token}`)
        return api.request(config)
      }
    }
    return Promise.reject(error)
  },
)

/** What the UI needs to render a failure honestly (§11). */
export interface ApiFailure {
  status: number | null
  message: string
  requestId: string | null
  retryAfter: number | null
  fieldErrors: Record<string, string>
}

interface ValidationDetailItem {
  loc?: unknown[]
  msg?: string
}

function extractFieldErrors(detail: unknown): Record<string, string> {
  if (!Array.isArray(detail)) return {}
  const fields: Record<string, string> = {}
  for (const raw of detail) {
    const item = raw as ValidationDetailItem
    const loc = Array.isArray(item.loc) ? item.loc : []
    // FastAPI's `loc` is ["body", "field"] — the last element names the field.
    const name = loc.length ? String(loc[loc.length - 1]) : 'detail'
    fields[name] = item.msg ?? 'invalid'
  }
  return fields
}

/**
 * Normalise anything thrown by axios into something a component can render.
 *
 * Every status renders as itself (§11): 403 says your role cannot do this, 429
 * surfaces the retry-after, 5xx shows the request id so a bug report and a log
 * line can be joined. Nothing is flattened into "something went wrong", and
 * nothing is swallowed into an empty table.
 */
export function describeFailure(error: unknown): ApiFailure {
  if (!axios.isAxiosError(error)) {
    return {
      status: null,
      message: error instanceof Error ? error.message : 'Unexpected error',
      requestId: null,
      retryAfter: null,
      fieldErrors: {},
    }
  }
  const response = error.response
  const status = response?.status ?? null
  const data = response?.data as { detail?: unknown } | undefined
  const detail = data?.detail
  const fieldErrors = extractFieldErrors(detail)

  let message: string
  if (typeof detail === 'string') message = detail
  else if (status === 403) message = 'Your role does not allow this.'
  else if (status === 429) message = 'Rate limit reached.'
  else if (status === null) message = 'Cannot reach the API. Is the backend running?'
  else message = `Request failed (${status}).`

  const retryAfterHeader = response?.headers?.['retry-after']
  const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : null

  return {
    status,
    message,
    requestId: (response?.headers?.[REQUEST_ID_HEADER] as string | undefined) ?? null,
    retryAfter: Number.isFinite(retryAfter) ? retryAfter : null,
    fieldErrors,
  }
}
