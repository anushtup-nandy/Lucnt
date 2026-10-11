/**
 * The ONE polling wrapper.
 *
 * Polling lives here and nowhere else. `refetchInterval` sprinkled across pages
 * drifts to a different cadence per page and — the part that actually bites —
 * nothing pauses when the tab is hidden, so a laptop left on the dashboard
 * overnight bills a request every four seconds until morning.
 *
 * Polling rather than SSE, deliberately: at demo cadence the two are visually
 * identical, and SSE costs a streaming endpoint, long-lived-connection auth,
 * nginx buffering config and client reconnect logic — which is precisely the
 * machinery that breaks live on stage.
 */

import {
  useQuery,
  type QueryKey,
  type UseQueryOptions,
  type UseQueryResult,
} from '@tanstack/react-query'

/** Fast enough that a trickling demo looks alive; slow enough to be invisible. */
export const LIVE_INTERVAL_MS = 4_000

/** Configuration-shaped data (policies, entities, users) changes rarely. */
export const STATIC_STALE_MS = 5 * 60_000

type LiveOptions<T> = Omit<
  UseQueryOptions<T, Error, T, QueryKey>,
  'queryKey' | 'queryFn' | 'refetchInterval' | 'refetchIntervalInBackground'
>

/**
 * A polled query that stops when nobody is looking.
 *
 * `refetchIntervalInBackground: false` is the whole point: React Query then
 * suspends the interval while the tab is hidden and refetches once on focus,
 * so the view is current the instant it is looked at and idle otherwise.
 */
export function useLiveQuery<T>(
  queryKey: QueryKey,
  queryFn: () => Promise<T>,
  options: LiveOptions<T> = {},
): UseQueryResult<T, Error> {
  return useQuery<T, Error, T, QueryKey>({
    queryKey,
    queryFn,
    refetchInterval: LIVE_INTERVAL_MS,
    refetchIntervalInBackground: false,
    ...options,
  })
}

/** A non-polled query for data that only changes when someone edits it. */
export function useStaticQuery<T>(
  queryKey: QueryKey,
  queryFn: () => Promise<T>,
  options: LiveOptions<T> = {},
): UseQueryResult<T, Error> {
  return useQuery<T, Error, T, QueryKey>({
    queryKey,
    queryFn,
    staleTime: STATIC_STALE_MS,
    ...options,
  })
}
