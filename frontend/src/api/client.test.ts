/**
 * The token-refresh race, and error normalisation.
 *
 * **Concurrent 401s must trigger one refresh, not N** (`frontend/PLAN.md` §13).
 * `/auth/refresh` rotates the cookie, so a second concurrent refresh presents a
 * token the server has already rotated away and logs the user out. The bug only
 * appears after the access token expires — fifteen idle minutes, i.e. exactly
 * when someone is mid-sentence on stage.
 */

import MockAdapter from 'axios-mock-adapter'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { api, describeFailure, refreshAccessToken, refreshClient } from './client'
import { useAuthStore } from '@/store/authStore'

let mock: MockAdapter
let refreshMock: MockAdapter

beforeEach(() => {
  mock = new MockAdapter(api)
  refreshMock = new MockAdapter(refreshClient)
  useAuthStore.getState().clear()
})

afterEach(() => {
  mock.restore()
  refreshMock.restore()
})

describe('describeFailure', () => {
  it('names the role problem on a 403', () => {
    const failure = describeFailure({
      isAxiosError: true,
      response: { status: 403, data: {}, headers: {} },
    })
    expect(failure.status).toBe(403)
    expect(failure.message).toMatch(/role/i)
  })

  it('surfaces retry-after on a 429', () => {
    const failure = describeFailure({
      isAxiosError: true,
      response: { status: 429, data: {}, headers: { 'retry-after': '30' } },
    })
    expect(failure.retryAfter).toBe(30)
  })

  it('carries the request id on a 5xx so a log line can be found', () => {
    const failure = describeFailure({
      isAxiosError: true,
      response: { status: 500, data: {}, headers: { 'x-request-id': 'abc123' } },
    })
    expect(failure.requestId).toBe('abc123')
  })

  it('turns FastAPI validation detail into field errors', () => {
    const failure = describeFailure({
      isAxiosError: true,
      response: {
        status: 422,
        headers: {},
        data: { detail: [{ loc: ['body', 'monthly_budget'], msg: 'must be positive' }] },
      },
    })
    expect(failure.fieldErrors['monthly_budget']).toBe('must be positive')
  })

  it('says the API is unreachable rather than "something went wrong"', () => {
    const failure = describeFailure({ isAxiosError: true, response: undefined })
    expect(failure.message).toMatch(/backend/i)
  })
})

describe('refreshAccessToken', () => {
  it('collapses concurrent callers into ONE network request', async () => {
    let calls = 0
    refreshMock.onPost('/auth/refresh').reply(() => {
      calls += 1
      return [200, { access_token: 'fresh' }]
    })

    const results = await Promise.all([
      refreshAccessToken(),
      refreshAccessToken(),
      refreshAccessToken(),
    ])

    // The whole point: three callers, one rotation. `/auth/refresh` rotates the
    // cookie, so a second concurrent call would present a token the server has
    // already invalidated and would log the user out.
    expect(calls).toBe(1)
    expect(results).toEqual(['fresh', 'fresh', 'fresh'])
    expect(useAuthStore.getState().accessToken).toBe('fresh')
  })

  it('starts a fresh request for the NEXT expiry', async () => {
    let calls = 0
    refreshMock.onPost('/auth/refresh').reply(() => {
      calls += 1
      return [200, { access_token: `tok-${calls}` }]
    })
    await refreshAccessToken()
    await refreshAccessToken()
    expect(calls).toBe(2)
  })

  it('clears the session when the cookie is gone', async () => {
    useAuthStore.getState().setToken('stale')
    refreshMock.onPost('/auth/refresh').reply(401)
    expect(await refreshAccessToken()).toBeNull()
    expect(useAuthStore.getState().accessToken).toBeNull()
  })
})

describe('401 handling', () => {
  it('retries the original request once with the new token', async () => {
    refreshMock.onPost('/auth/refresh').reply(200, { access_token: 'fresh' })
    const seen: (string | undefined)[] = []
    mock.onGet('/agents').reply((config) => {
      seen.push(config.headers?.['Authorization'] as string | undefined)
      return seen.length === 1 ? [401, {}] : [200, { items: [], total: 0 }]
    })

    const response = await api.get('/agents')
    expect(response.status).toBe(200)
    expect(seen).toHaveLength(2)
    expect(seen[1]).toBe('Bearer fresh')
  })

  it('gives up after one retry rather than looping', async () => {
    refreshMock.onPost('/auth/refresh').reply(200, { access_token: 'fresh' })
    let calls = 0
    mock.onGet('/agents').reply(() => {
      calls += 1
      return [401, {}]
    })
    await expect(api.get('/agents')).rejects.toBeTruthy()
    expect(calls).toBe(2)
  })
})

describe('request interceptor', () => {
  it('attaches the access token when there is one', async () => {
    useAuthStore.getState().setToken('tok-123')
    mock.onGet('/agents').reply((config) => {
      expect(config.headers?.['Authorization']).toBe('Bearer tok-123')
      return [200, { items: [], total: 0 }]
    })
    await api.get('/agents')
  })

  it('sends no Authorization header when signed out', async () => {
    mock.onGet('/agents').reply((config) => {
      expect(config.headers?.['Authorization']).toBeUndefined()
      return [200, { items: [], total: 0 }]
    })
    await api.get('/agents')
  })

  it('does not try to refresh a failed login', async () => {
    // A 401 from /auth/login means "wrong password". Refreshing on it would
    // turn a failed login into a loop.
    let calls = 0
    mock.onPost('/auth/login').reply(() => {
      calls += 1
      return [401, { detail: 'Invalid email or password' }]
    })
    await expect(api.post('/auth/login', {})).rejects.toBeTruthy()
    expect(calls).toBe(1)
  })
})
