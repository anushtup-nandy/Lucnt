/**
 * Money formatting. **Amounts never become a JS `number`.**
 *
 * `frontend/PLAN.md` §11.2, and not a style preference: the backend stores money
 * as `Numeric(18,4)` and serialises it as a *string* for exactly this reason. A
 * `parseFloat` on the way in is how a ledger renders `₹6,00,000.00000001`, and
 * how a trial balance stops balancing on screen while balancing in the database.
 *
 * So every function here takes a decimal string and returns a display string.
 * Arithmetic that has to happen client-side (a percentage for a progress bar,
 * say) is confined to `ratio()`, which is explicitly lossy and explicitly only
 * for pixels.
 */

const CURRENCY_SYMBOLS: Record<string, string> = {
  INR: '₹',
  USD: '$',
  SGD: 'S$',
  EUR: '€',
  GBP: '£',
}

/** Split a decimal string into sign, integer digits and fraction digits. */
function parts(amount: string): { sign: string; whole: string; frac: string } {
  const trimmed = amount.trim()
  const sign = trimmed.startsWith('-') ? '-' : ''
  const unsigned = sign ? trimmed.slice(1) : trimmed
  const [whole = '0', frac = ''] = unsigned.split('.')
  return { sign, whole: whole || '0', frac }
}

/** Group integer digits in threes, e.g. `1234567` → `1,234,567`. */
function groupThousands(whole: string): string {
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/**
 * Group Indian-style: last three, then twos — `1234567` → `12,34,567`.
 *
 * Not a flourish. Two of the three seeded entities are INR, and a CFO reading a
 * lakh figure in western grouping has to stop and count digits.
 */
function groupLakh(whole: string): string {
  if (whole.length <= 3) return whole
  const last3 = whole.slice(-3)
  const rest = whole.slice(0, -3)
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${last3}`
}

export interface MoneyOptions {
  /** Fraction digits to show. Default 2 — the ledger keeps 4, screens show 2. */
  decimals?: number
  /** Omit the currency symbol (for a column that has its own header). */
  bare?: boolean
}

/**
 * Format a decimal-string amount for display.
 *
 * @param amount Decimal string from the API, e.g. `"1234.5600"`. `null` and
 *   `undefined` render as an em dash rather than as `0`, because "no value" and
 *   "zero" are different facts about a ledger.
 * @param currency ISO-4217 code, e.g. `"INR"`.
 */
export function formatMoney(
  amount: string | null | undefined,
  currency = 'USD',
  options: MoneyOptions = {},
): string {
  if (amount === null || amount === undefined || amount === '') return '—'
  const { decimals = 2, bare = false } = options
  const { sign, whole, frac } = parts(amount)
  if (!/^\d+$/.test(whole)) return String(amount)

  const grouped = currency === 'INR' ? groupLakh(whole) : groupThousands(whole)
  const padded = decimals > 0 ? `.${frac.padEnd(decimals, '0').slice(0, decimals)}` : ''
  const symbol = bare ? '' : (CURRENCY_SYMBOLS[currency] ?? `${currency} `)
  return `${sign}${symbol}${grouped}${padded}`
}

/** A compact form for headline stats: `₹12.4L`, `$1.2M`. Display only. */
export function formatMoneyCompact(
  amount: string | null | undefined,
  currency = 'USD',
): string {
  if (amount === null || amount === undefined || amount === '') return '—'
  const { sign, whole } = parts(amount)
  if (!/^\d+$/.test(whole)) return String(amount)
  const symbol = CURRENCY_SYMBOLS[currency] ?? `${currency} `
  const digits = whole.length
  const head = (divisorDigits: number, suffix: string): string => {
    const scaled = whole.slice(0, digits - divisorDigits)
    const remainder = whole.slice(digits - divisorDigits, digits - divisorDigits + 1)
    return `${sign}${symbol}${scaled}.${remainder}${suffix}`
  }
  if (currency === 'INR') {
    if (digits > 7) return head(7, 'Cr')
    if (digits > 5) return head(5, 'L')
  } else {
    if (digits > 9) return head(9, 'B')
    if (digits > 6) return head(6, 'M')
    if (digits > 3) return head(3, 'K')
  }
  return formatMoney(amount, currency, { decimals: 0 })
}

/**
 * A 0–1 ratio for a progress bar. **Lossy on purpose, and only for pixels.**
 *
 * This is the one place a money string becomes a float. A bar is 6 device
 * pixels tall; float error cannot be seen there. Never feed this to anything
 * that is displayed as a number.
 */
export function ratio(numerator: string, denominator: string): number {
  const bottom = Number(denominator)
  if (!Number.isFinite(bottom) || bottom === 0) return 0
  const top = Number(numerator)
  if (!Number.isFinite(top)) return 0
  return top / bottom
}

/** Percentage used, clamped to a sane bar width but reported honestly in text. */
export function pctUsed(spent: string, budget: string): number {
  return Math.round(ratio(spent, budget) * 100)
}

/** Bar heat per §3: `<88%` lime, `88–100%` amber, `>100%` red. */
export function barHeat(pct: number): '' | 'hot' | 'full' {
  if (pct > 100) return 'full'
  if (pct >= 88) return 'hot'
  return ''
}
