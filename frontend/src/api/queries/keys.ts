/**
 * Query keys. Every key carries user id and entity code so switching either never
 * serves another scope's cached rows. Always build keys via `scope()`.
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
