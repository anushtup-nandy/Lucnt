/**
 * Nav rendering. **The sidebar is data, never a hardcoded array.**
 *
 * `CLAUDE.local.md` §Step 4 names this non-negotiable, and `frontend/PLAN.md`
 * §11.1 repeats it: the list comes from `perms.nav` on `/auth/me`. The server
 * gates the same tuple (`require_nav`), so a link that is not rendered is also
 * an endpoint that 403s — the two cannot drift apart.
 *
 * The only thing hardcoded here is the *icon* per domain key, which is
 * decoration. An unknown key still renders, with a neutral glyph, rather than
 * disappearing: a nav item the backend sent and the frontend silently dropped
 * would be the same class of bug in the other direction.
 */

import type { NavItem } from '@/store/authStore'

const ICONS: Record<string, string> = {
  overview: '◈',
  agents: '◑',
  transactions: '⇄',
  books: '▤',
  budgets: '◧',
  policies: '⚖',
  approvals: '✓',
  recon: '⟳',
  insights: '✦',
  memory: '❧',
  developers: '⌘',
  settings: '⚙',
}

/** The mockup's two sidebar sections. Anything else falls into "Workspace". */
const OPERATE = new Set(['overview', 'agents', 'transactions', 'approvals'])
const FINANCE = new Set(['books', 'budgets', 'policies', 'recon', 'insights', 'memory'])

export interface NavSection {
  label: string
  items: NavItem[]
}

export const navIcon = (key: string): string => ICONS[key] ?? '·'

/**
 * Group `perms.nav` into the mockup's sections, preserving the server's order
 * within each and dropping any section that ends up empty.
 */
export function groupNav(nav: NavItem[]): NavSection[] {
  const sections: NavSection[] = [
    { label: 'Operate', items: nav.filter((i) => OPERATE.has(i.key)) },
    { label: 'Finance', items: nav.filter((i) => FINANCE.has(i.key)) },
    {
      label: 'Workspace',
      items: nav.filter((i) => !OPERATE.has(i.key) && !FINANCE.has(i.key)),
    },
  ]
  return sections.filter((section) => section.items.length > 0)
}

/** Initials for the sidebar avatar. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')
}
