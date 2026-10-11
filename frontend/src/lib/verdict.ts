/** Verdict → tag mapping, centralised and tested. Tags always carry a text label. */

export type TagTone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral'

export interface Tag {
  tone: TagTone
  label: string
}

/** `authorizations.status` — a closed enum on the wire. */
export function verdictTag(status: string): Tag {
  switch (status) {
    case 'approved':
      return { tone: 'ok', label: 'Approved' }
    case 'blocked':
      return { tone: 'bad', label: 'Blocked' }
    case 'review':
      return { tone: 'warn', label: 'Flagged' }
    case 'human_hold':
      return { tone: 'warn', label: 'Held' }
    default:
      // Unknown status: show it verbatim rather than guess it is fine.
      return { tone: 'info', label: status }
  }
}

export interface TransactionLike {
  estimated?: boolean | null
  is_reversed?: boolean | null
}

/** A committed transaction's tag; precedence: reversed, estimate, settled. */
export function transactionTag(txn: TransactionLike): Tag {
  if (txn.is_reversed) return { tone: 'warn', label: 'Reversed' }
  if (txn.estimated) return { tone: 'info', label: 'Estimated' }
  return { tone: 'ok', label: 'Captured' }
}

/** Agent lifecycle status. */
export function agentTag(status: string): Tag {
  switch (status) {
    case 'active':
      return { tone: 'ok', label: 'Active' }
    case 'paused':
      return { tone: 'warn', label: 'Paused' }
    case 'disabled':
      return { tone: 'bad', label: 'Disabled' }
    default:
      return { tone: 'neutral', label: status }
  }
}

/** Budget utilisation label, matching the heat bar thresholds. */
export function budgetTag(pct: number): Tag {
  if (pct > 100) return { tone: 'bad', label: 'Over budget' }
  if (pct >= 88) return { tone: 'warn', label: 'Near limit' }
  return { tone: 'ok', label: 'On track' }
}

/** Reconciliation exception status. */
export function reconTag(status: string): Tag {
  switch (status) {
    case 'open':
      return { tone: 'warn', label: 'Open' }
    case 'resolved':
      return { tone: 'ok', label: 'Resolved' }
    case 'dismissed':
      return { tone: 'neutral', label: 'Dismissed' }
    default:
      return { tone: 'neutral', label: status }
  }
}
