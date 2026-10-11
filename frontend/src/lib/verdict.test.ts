/** Verdict → tag mapping, exhaustive over the backend's `VerdictStatus`. */

import { describe, expect, it } from 'vitest'

import { agentTag, budgetTag, transactionTag, verdictTag } from './verdict'

/** Mirrors `VerdictStatus` in `backend/src/lucnt_api/models/authorization.py`. */
const VERDICT_STATUSES = ['approved', 'blocked', 'review', 'human_hold'] as const

describe('verdictTag', () => {
  it('maps every backend verdict status', () => {
    for (const status of VERDICT_STATUSES) {
      const tag = verdictTag(status)
      expect(tag.label, status).toBeTruthy()
      expect(tag.tone, status).not.toBe('neutral')
    }
  })

  it('never renders a blocked spend as ok', () => {
    expect(verdictTag('blocked').tone).toBe('bad')
    expect(verdictTag('blocked').label).toBe('Blocked')
    expect(verdictTag('human_hold').tone).toBe('warn')
    expect(verdictTag('review').tone).toBe('warn')
    expect(verdictTag('approved').tone).toBe('ok')
  })

  it('shows an unknown status as itself instead of guessing', () => {
    // An unknown status must render raw, never as "Approved".
    expect(verdictTag('quarantined')).toEqual({ tone: 'info', label: 'quarantined' })
  })
})

describe('transactionTag', () => {
  it('prefers reversed over estimated', () => {
    // Reversal takes precedence over estimate.
    expect(transactionTag({ is_reversed: true, estimated: true }).label).toBe('Reversed')
  })

  it('labels an estimate as an estimate', () => {
    expect(transactionTag({ estimated: true }).label).toBe('Estimated')
    expect(transactionTag({ estimated: true }).tone).toBe('info')
  })

  it('labels a settled capture', () => {
    expect(transactionTag({ estimated: false, is_reversed: false })).toEqual({
      tone: 'ok',
      label: 'Captured',
    })
  })

  it('treats null flags as false', () => {
    expect(transactionTag({ estimated: null, is_reversed: null }).label).toBe('Captured')
  })
})

describe('agentTag', () => {
  it('maps every AgentStatus member', () => {
    for (const status of ['active', 'paused', 'disabled']) {
      expect(agentTag(status).tone, status).not.toBe('neutral')
    }
  })
})

describe('budgetTag', () => {
  it('matches the heat-bar thresholds', () => {
    expect(budgetTag(50).tone).toBe('ok')
    expect(budgetTag(88).tone).toBe('warn')
    expect(budgetTag(101).tone).toBe('bad')
  })
})
