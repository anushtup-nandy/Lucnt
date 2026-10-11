/** The only polling wrapper: one cadence, paused while the tab is hidden. */

import {
  useQuery,
  type QueryKey,
  type UseQueryOptions,
  type UseQueryResult,
} from '@tanstack/react-query'

export const LIVE_INTERVAL_MS = 4_000

/** Configuration-shaped data (policies, entities, users) changes rarely. */
export const STATIC_STALE_MS = 5 * 60_000

type LiveOptions<T> = Omit<
  UseQueryOptions<T, Error, T, QueryKey>,
  'queryKey' | 'queryFn' | 'refetchInterval' | 'refetchIntervalInBackground'
>

/** A polled query that pauses while the tab is hidden. */
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
