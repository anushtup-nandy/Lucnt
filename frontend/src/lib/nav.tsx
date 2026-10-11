/**
 * Nav rendering from server `perms.nav` (same list `require_nav` gates), never a
 * hardcoded array. Unknown keys render with a neutral icon rather than vanish.
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
