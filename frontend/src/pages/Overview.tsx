/**
 * Overview — attention cards composed **client-side** from existing reads.
 *
 * There is deliberately no `GET /overview` aggregate:
 * four small reads the dashboard already makes are cheaper to build, cheaper to
 * keep honest, and cannot drift from the pages they link to. If this page ever
 * feels slow, that is the signal to add the aggregate — not before.
 *
 * A card only appears when it has something to say. An empty Overview is the
 * *good* outcome, and it says so rather than showing four zeroes.
 */

import type { ReactNode } from 'react'
import { useNavigate } from 'react-router'

import { api } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { formatMoney } from '@/lib/money'
import { useAuthStore } from '@/store/authStore'
import type { BudgetsResponse, Paginated, ReconListResponse } from '@/types/domain'
import type { Approval, Insight } from '@/types/domain'

interface AttentionCard {
  tone: 'bad' | 'warn' | 'money' | 'info'
  icon: string
  title: string
  sub: string
  action: string
  to: string
}

function Attention({ card }: { card: AttentionCard }): ReactNode {
  const navigate = useNavigate()
  return (
    <button type="button" className={`att ${card.tone}`} onClick={() => navigate(card.to)}>
      <span className="ai" aria-hidden>
        {card.icon}
      </span>
      <span className="am">
        <span className="at">{card.title}</span>
        <span className="as">{card.sub}</span>
      </span>
      <span className="ab btn sm">{card.action}</span>
    </button>
  )
}

export function Overview(): ReactNode {
  const { userId, entity } = useScope()
  const perms = useAuthStore((s) => s.session?.perms)
  const canSee = (key: string): boolean => perms?.nav.some((n) => n.key === key) ?? false

  const params = { ...entityParam(entity), limit: 1 }

  const txns = useLiveQuery(scope('ov-txn', userId, entity), async () => {
    const { data } = await api.get<{ total: number }>('/transactions', { params })
    return data.total
  })

  const blocked = useLiveQuery(scope('ov-blocked', userId, entity), async () => {
    const { data } = await api.get<{ total: number }>('/authorizations', {
      params: { ...params, status: 'blocked' },
    })
    return data.total
  })

  const recon = useLiveQuery(
    scope('ov-recon', userId, entity),
    async () => {
      const { data } = await api.get<ReconListResponse>('/recon', {
        params: { ...params, status: 'open' },
      })
      return data
    },
    { enabled: canSee('recon') },
  )

  const approvals = useLiveQuery(
    scope('ov-approvals', userId, entity),
    async () => {
      const { data } = await api.get<Paginated<Approval>>('/approvals', { params: { limit: 1 } })
      return data.total
    },
    { enabled: canSee('approvals') },
  )

  const budgets = useLiveQuery(
    scope('ov-budgets', userId, entity),
    async () => {
      const { data } = await api.get<BudgetsResponse>('/budgets', {
        params: { ...entityParam(entity), limit: 200 },
      })
      return data
    },
    { enabled: canSee('budgets') },
  )

  const insights = useLiveQuery(
    scope('ov-insights', userId, entity),
    async () => {
      const { data } = await api.get<Paginated<Insight> & { unapplied_count: number }>(
        '/insights',
        { params },
      )
      return data
    },
    { enabled: canSee('insights') },
  )

  const cards: AttentionCard[] = []

  const hotAgents = (budgets.data?.agents ?? []).filter((a) => Number(a.pct_used) >= 88)
  if (hotAgents.length > 0) {
    cards.push({
      tone: hotAgents.some((a) => Number(a.pct_used) > 100) ? 'bad' : 'warn',
      icon: '◧',
      title: `${hotAgents.length} agent${hotAgents.length === 1 ? '' : 's'} at or over budget`,
      sub: hotAgents
        .slice(0, 3)
        .map((a) => `${a.name} ${Math.round(Number(a.pct_used))}%`)
        .join(' · '),
      action: 'Adjust',
      to: '/budgets',
    })
  }

  if (approvals.data) {
    cards.push({
      tone: 'warn',
      icon: '✓',
      title: `${approvals.data} spend${approvals.data === 1 ? '' : 's'} waiting on a human`,
      sub: 'Held before money moved, pending approval.',
      action: 'Review',
      to: '/approvals',
    })
  }

  const atStake = Object.entries(recon.data?.total_at_stake ?? {})
  if (recon.data && recon.data.open_count > 0) {
    cards.push({
      tone: 'money',
      icon: '⟳',
      title: `${formatMoney(atStake[0]?.[1] ?? '0', atStake[0]?.[0] ?? 'USD')} recoverable`,
      sub: `${recon.data.open_count} open reconciliation exception${recon.data.open_count === 1 ? '' : 's'}.`,
      action: 'Recover',
      to: '/recon',
    })
  }

  if (insights.data && insights.data.unapplied_count > 0) {
    cards.push({
      tone: 'info',
      icon: '✦',
      title: `${insights.data.unapplied_count} new insight${insights.data.unapplied_count === 1 ? '' : 's'}`,
      sub: insights.data.items[0]?.title ?? 'Patterns found in your own ledger.',
      action: 'Read',
      to: '/insights',
    })
  }

  const loading = txns.isPending || blocked.isPending

  return (
    <div className="page">
      <PageHeader
        title="Overview"
        sub={
          txns.data === undefined ? (
            'Loading your ledger…'
          ) : (
            <>
              <b>{txns.data}</b> financial decision{txns.data === 1 ? ' was' : 's were'} made
              automatically and booked
              {blocked.data ? (
                <>
                  {' '}
                  · <b>{blocked.data}</b> blocked before money moved
                </>
              ) : null}
            </>
          )
        }
      />

      {loading ? <Loading rows={3} /> : null}

      {!loading && cards.length === 0 ? (
        <div className="att-done">
          Nothing needs you. Every spend was authorized, booked, and reconciled.
        </div>
      ) : (
        cards.map((card) => <Attention key={card.title} card={card} />)
      )}
    </div>
  )
}
