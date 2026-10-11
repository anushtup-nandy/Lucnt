/** `formatMoney` must preserve `Numeric(18,4)` precision a JS number would lose. */

import { describe, expect, it } from 'vitest'

import { barHeat, formatMoney, formatMoneyCompact, pctUsed, ratio } from './money'

describe('formatMoney', () => {
  it('never routes through a JS number', () => {
    // 17 significant digits: Number() would round the last one away.
    expect(formatMoney('60000000000000.1234', 'USD', { decimals: 4 })).toBe(
      '$60,000,000,000,000.1234',
    )
  })

  it('keeps a fraction that a float would round', () => {
    expect(formatMoney('0.1235', 'USD', { decimals: 4 })).toBe('$0.1235')
    expect(formatMoney('1234.5678', 'USD', { decimals: 4 })).toBe('$1,234.5678')
  })

  it('truncates rather than rounds to the requested precision', () => {
    // Truncates, not rounds, so displayed rows still sum to the displayed total.
    expect(formatMoney('10.9999', 'USD')).toBe('$10.99')
  })

  it('groups INR in lakhs and crores', () => {
    expect(formatMoney('600000.00', 'INR')).toBe('₹6,00,000.00')
    expect(formatMoney('12345678.00', 'INR')).toBe('₹1,23,45,678.00')
    expect(formatMoney('999.00', 'INR')).toBe('₹999.00')
  })

  it('groups other currencies in thousands', () => {
    expect(formatMoney('600000.00', 'USD')).toBe('$600,000.00')
    expect(formatMoney('1234567.00', 'SGD')).toBe('S$1,234,567.00')
  })

  it('renders an unknown currency as its code', () => {
    expect(formatMoney('10.00', 'AED')).toBe('AED 10.00')
  })

  it('distinguishes missing from zero', () => {
    expect(formatMoney(null, 'USD')).toBe('—')
    expect(formatMoney(undefined, 'USD')).toBe('—')
    expect(formatMoney('', 'USD')).toBe('—')
    expect(formatMoney('0', 'USD')).toBe('$0.00')
  })

  it('keeps the sign on a negative', () => {
    expect(formatMoney('-1234.50', 'USD')).toBe('-$1,234.50')
  })

  it('pads a short fraction rather than dropping digits', () => {
    expect(formatMoney('5.1', 'USD')).toBe('$5.10')
    expect(formatMoney('5', 'USD')).toBe('$5.00')
  })

  it('omits the symbol when asked', () => {
    expect(formatMoney('1000.00', 'USD', { bare: true })).toBe('1,000.00')
  })

  it('returns garbage unchanged rather than inventing a number', () => {
    expect(formatMoney('not-a-number', 'USD')).toBe('not-a-number')
  })
})

describe('formatMoneyCompact', () => {
  it('uses lakh/crore for INR', () => {
    expect(formatMoneyCompact('1240000', 'INR')).toBe('₹12.4L')
    expect(formatMoneyCompact('12400000', 'INR')).toBe('₹1.2Cr')
  })

  it('uses K/M/B elsewhere', () => {
    expect(formatMoneyCompact('1200', 'USD')).toBe('$1.2K')
    expect(formatMoneyCompact('1200000', 'USD')).toBe('$1.2M')
  })

  it('leaves small values alone', () => {
    expect(formatMoneyCompact('42', 'USD')).toBe('$42')
  })
})

describe('ratio / pctUsed', () => {
  it('never divides by zero', () => {
    expect(ratio('100', '0')).toBe(0)
    expect(pctUsed('100', '0')).toBe(0)
  })

  it('reports over-budget honestly rather than clamping', () => {
    expect(pctUsed('130', '100')).toBe(130)
  })
})

describe('barHeat', () => {
  it('matches the §3 thresholds', () => {
    expect(barHeat(87)).toBe('')
    expect(barHeat(88)).toBe('hot')
    expect(barHeat(100)).toBe('hot')
    expect(barHeat(101)).toBe('full')
  })
})
