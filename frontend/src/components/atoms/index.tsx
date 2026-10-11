/**
 * The shared atoms, in one file on purpose.
 *
 * Each is three to ten lines over a class from the ported design system. Eight
 * files of five lines each would be eight imports and eight places to look; the
 * grouping is the DRY rule applied to the file count rather
 * than only to the code.
 */

import clsx from 'clsx'
import type { ReactNode } from 'react'

import type { ApiFailure } from '@/api/client'
import { barHeat, formatMoney } from '@/lib/money'
import type { Tag as TagSpec } from '@/lib/verdict'

/* --------------------------------------------------------------------- Tag */

export function Tag({ tag }: { tag: TagSpec }): ReactNode {
  const tone = tag.tone === 'neutral' ? '' : tag.tone
  return (
    <span className={clsx('tag', tone)}>
      <i className="d" />
      {tag.label}
    </span>
  )
}

/* --------------------------------------------------------------------- Bar */

/**
 * A budget heat bar. Width is clamped to 100%; the *label* still tells the
 * truth about an over-budget agent, because a bar that stops at full and a
 * number that says 130% are both needed — one to scan, one to believe.
 */
export function Bar({ pct }: { pct: number }): ReactNode {
  return (
    <div className="bar" role="presentation">
      <i className={barHeat(pct)} style={{ width: `${Math.min(Math.max(pct, 0), 100)}%` }} />
    </div>
  )
}

/* ------------------------------------------------------------------- Money */

export function Money({
  amount,
  currency,
  decimals,
  bare,
}: {
  amount: string | null | undefined
  currency?: string
  decimals?: number
  bare?: boolean
}): ReactNode {
  return (
    <span className="mono">
      {formatMoney(amount, currency, {
        ...(decimals === undefined ? {} : { decimals }),
        ...(bare === undefined ? {} : { bare }),
      })}
    </span>
  )
}

/* ------------------------------------------------------------------- Glyph */

export function Glyph({
  glyph,
  className = 'gy',
}: {
  glyph: string
  className?: string
}): ReactNode {
  return (
    <span className={className} aria-hidden>
      {glyph}
    </span>
  )
}

/* ------------------------------------------------------------------ States */

export function Loading({ rows = 5 }: { rows?: number }): ReactNode {
  return (
    <div aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div className="skel" key={i} />
      ))}
    </div>
  )
}

export function Empty({ title, hint }: { title: string; hint?: string }): ReactNode {
  return (
    <div className="state">
      <div className="st-t serif">{title}</div>
      {hint ? <div>{hint}</div> : null}
    </div>
  )
}

/**
 * Every status renders as itself.
 *
 * No error is ever collapsed into an empty table: an empty table means "no
 * rows", always. A 403 says which role was refused, a 429 says how long to
 * wait, a 5xx shows the request id so the user's screenshot and the server's
 * log line can be joined.
 */
export function Failure({
  failure,
  onRetry,
}: {
  failure: ApiFailure
  onRetry?: () => void
}): ReactNode {
  const title =
    failure.status === 403
      ? 'Your role cannot see this'
      : failure.status === 429
        ? 'Too many requests'
        : failure.status === 404
          ? 'Not found'
          : 'Something failed'
  return (
    <div className="state" role="alert">
      <div className="st-t serif">{title}</div>
      <div>{failure.message}</div>
      {failure.retryAfter ? <div>Try again in {failure.retryAfter}s.</div> : null}
      {failure.requestId ? <div className="st-d">request {failure.requestId}</div> : null}
      {onRetry ? (
        <button type="button" className="btn sm" style={{ marginTop: 14 }} onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  )
}

/* -------------------------------------------------------------------- Page */

export function PageHeader({
  title,
  sub,
  actions,
}: {
  title: string
  sub?: ReactNode
  actions?: ReactNode
}): ReactNode {
  return (
    <div className="page-h" style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <h1 className="page-title">{title}</h1>
        {sub ? <div className="page-sub">{sub}</div> : null}
      </div>
      {actions ? <div style={{ flexShrink: 0, paddingTop: 6 }}>{actions}</div> : null}
    </div>
  )
}

export function Card({
  title,
  action,
  className,
  children,
}: {
  title?: string
  action?: ReactNode
  className?: string
  children: ReactNode
}): ReactNode {
  return (
    <section className={clsx('card', className)}>
      {title ? (
        <div className="card-h">
          <span>{title}</span>
          {action}
        </div>
      ) : null}
      {children}
    </section>
  )
}

export function KV({
  k,
  v,
  mono,
  lime,
}: {
  k: string
  v: ReactNode
  mono?: boolean
  lime?: boolean
}): ReactNode {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className={clsx('v', mono && 'mono', lime && 'lime')}>{v}</span>
    </div>
  )
}

/* ------------------------------------------------------------- FilterChips */

export interface ChipSpec<T extends string> {
  value: T
  label: string
  count?: number
}

export function FilterChips<T extends string>({
  chips,
  active,
  onChange,
}: {
  chips: ChipSpec<T>[]
  active: T
  onChange: (value: T) => void
}): ReactNode {
  return (
    <div className="toolbar" role="tablist">
      {chips.map((chip) => (
        <button
          key={chip.value}
          type="button"
          role="tab"
          aria-selected={chip.value === active}
          className={clsx('fchip', chip.value === active && 'on')}
          onClick={() => onChange(chip.value)}
        >
          {chip.label}
          {chip.count === undefined ? null : ` · ${chip.count}`}
        </button>
      ))}
    </div>
  )
}
