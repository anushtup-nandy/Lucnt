/**
 * Query keys. **Every key carries the user id and the entity code.**
 *
 * Switching either must not serve the other scope's
 * cached rows. Without the user id, logging out as a CFO and in as an
 * `agent_owner` on the same machine shows the CFO's agent list until the first
 * refetch lands — a scoping leak produced entirely in the browser, against a
 * backend that scoped the query correctly.
 *
 * `scope()` is therefore the only way keys are built here, and pages call these
 * helpers rather than writing array literals.
 */

import { useAuthStore } from '@/store/authStore'
import { useEntityStore } from '@/store/entityStore'

export type Scoped = readonly [string, string, ...unknown[]]

/** The (user, entity) pair every key is prefixed with. */
export function useScope(): { userId: string; entity: string | null } {
  const userId = useAuthStore((s) => s.session?.user.id ?? 'anonymous')
  const entity = useEntityStore((s) => s.current)
  return { userId, entity }
}

/** Build a scoped key: `[domain, userId, entity, ...rest]`. */
export function scope(
  domain: string,
  userId: string,
  entity: string | null,
  ...rest: unknown[]
): readonly unknown[] {
  return [domain, userId, entity ?? 'all', ...rest]
}

/** The `entity=` query param, omitted entirely for the consolidated view. */
export function entityParam(entity: string | null): Record<string, string> {
  return entity ? { entity } : {}
}
