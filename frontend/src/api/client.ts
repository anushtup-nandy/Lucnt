/**
 * The HTTP client: access-token injection, 401 → one shared silent refresh.
 * Shared because `/auth/refresh` rotates the cookie; parallel refreshes log out.
 */

import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'

import { getAccessToken, useAuthStore } from '@/store/authStore'

export const API_BASE_URL: string =
  (import.meta.env['VITE_API_BASE_URL'] as string | undefined) ?? 'http://localhost:8000'

/** Header the backend stamps on every response; shown in 5xx states. */
export const REQUEST_ID_HEADER = 'x-request-id'

export const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  // Sends the refresh cookie cross-origin; requires exact CORS_ORIGINS.
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
})

/** Bare client for the refresh call so it can't recurse; exported for test mocks. */
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

/** Exchange the refresh cookie for an access token; concurrent callers share one request. */
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

    // Never retry auth endpoints: a login 401 is a wrong password, not an expiry.
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

/** What the UI needs to render a failure. */
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

/** Normalise an axios error into a renderable, status-specific failure. */
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
