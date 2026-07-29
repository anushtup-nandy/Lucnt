/**
 * The workspace entity switcher.
 *
 * The selected entity code is part of every live query key, so switching it
 * refetches every view rather than showing another entity's cached rows.
 * `null` means "all entities" — the consolidated view a CFO starts on.
 */

import { create } from 'zustand'

export interface EntitySummary {
  id: string
  code: string
  name: string
  full_name: string
  currency: string
  accounting_std: string
  erp: string | null
  tax_regime: string
  country: string | null
  timezone: string
  is_active: boolean
}

interface EntityState {
  /** Entity `code`, or `null` for the consolidated view. */
  current: string | null
  entities: EntitySummary[]
  setCurrent: (code: string | null) => void
  setEntities: (entities: EntitySummary[]) => void
}

export const useEntityStore = create<EntityState>((set) => ({
  current: null,
  entities: [],
  setCurrent: (current) => set({ current }),
  setEntities: (entities) => set({ entities }),
}))

/** Flag emoji per seeded entity, matching the mockup's switcher. */
export const ENTITY_FLAGS: Record<string, string> = {
  in: '🇮🇳',
  us: '🇺🇸',
  sg: '🇸🇬',
}

export const entityFlag = (code: string): string => ENTITY_FLAGS[code.toLowerCase()] ?? '◻'
