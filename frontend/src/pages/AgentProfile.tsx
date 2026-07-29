/** One agent: budget, identity, vendors, governing policies, recent spend, memory. */

import type { ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router'

import { api, describeFailure } from '@/api/client'
import { scope, useScope } from '@/api/queries/keys'
import { Bar, Card, Empty, Failure, KV, Loading, Money, Tag } from '@/components/atoms'
import { useLiveQuery, useStaticQuery } from '@/hooks/useLiveQuery'
import { formatMoney, pctUsed } from '@/lib/money'
import { agentTag, transactionTag } from '@/lib/verdict'
import { useAuthStore } from '@/store/authStore'
import { useEntityStore } from '@/store/entityStore'
import type { Agent } from '@/pages/Agents'
import type { Memory, Paginated, Policy, Transaction } from '@/types/domain'

export function AgentProfile(): ReactNode {
  const { agentId } = useParams<{ agentId: string }>()
  const { userId, entity } = useScope()
  const entities = useEntityStore((s) => s.entities)
  const perms = useAuthStore((s) => s.session?.perms)
  const navigate = useNavigate()
  const canSee = (key: string): boolean => perms?.nav.some((n) => n.key === key) ?? false

  const agent = useLiveQuery(
    scope('agent', userId, entity, agentId),
    async () => {
      const { data } = await api.get<Agent>(`/agents/${agentId}`)
      return data
    },
    { enabled: Boolean(agentId) },
  )

  const slug = agent.data?.slug

  const txns = useLiveQuery(
    scope('agent-txns', userId, entity, slug),
    async () => {
      const { data } = await api.get<Paginated<Transaction>>('/transactions', {
        params: { agent: slug, limit: 6 },
      })
      return data.items
    },
    { enabled: Boolean(slug) && canSee('transactions') },
  )

  const memories = useLiveQuery(
    scope('agent-memory', userId, entity, slug),
    async () => {
      const { data } = await api.get<Paginated<Memory>>('/memory', {
        params: { agent: slug, limit: 5 },
      })
      return data.items
    },
    { enabled: Boolean(slug) && canSee('memory') },
  )

  const policies = useStaticQuery(
    scope('agent-policies', userId, entity, agentId),
    async () => {
      const { data } = await api.get<Paginated<Policy>>('/policies', {
        params: { active: true, limit: 100 },
      })
      // Company-wide policies govern every agent; agent-scoped ones only this
      // one. The backend applies exactly this rule at authorize time.
      return data.items.filter((p) => p.scope_type === 'company' || p.scope_id === agentId)
    },
    { enabled: Boolean(agentId) && canSee('policies') },
  )

  if (agent.isPending) {
    return (
      <div className="page">
        <Loading rows={7} />
      </div>
    )
  }
  if (agent.isError || !agent.data) {
    return (
      <div className="page">
        <Failure failure={describeFailure(agent.error)} onRetry={() => void agent.refetch()} />
      </div>
    )
  }

  const row = agent.data
  const entityRow = entities.find((e) => e.id === row.entity_id)
  const currency = entityRow?.currency ?? 'USD'
  const pct = pctUsed(row.spent_ytd, row.monthly_budget)

  return (
    <div className="page">
      <div className="prof-h">
        <span className="prof-g" aria-hidden>
          {row.glyph}
          <i className={`lv ${row.is_live ? '' : 'idle'}`} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="prof-name">{row.name}</div>
          <div className="prof-meta">
            {row.role} · {entityRow?.name ?? '—'} · confidence {row.confidence_pct}%
          </div>
        </div>
        <Tag tag={agentTag(row.status)} />
      </div>

      <div className="prof-grid">
        <Card title="Budget">
          <div className="bud-big">{formatMoney(row.monthly_budget, currency, { decimals: 0 })}</div>
          <div className="bud-sub">
            {formatMoney(row.spent_ytd, currency)} spent · {pct}% used
          </div>
          <Bar pct={pct} />
          <div style={{ marginTop: 14 }}>
            <KV k="Spent today" v={<Money amount={row.spent_today} currency={currency} />} mono />
            <KV
              k="Remaining"
              v={<Money amount={String(Number(row.monthly_budget) - Number(row.spent_ytd))} currency={currency} />}
              mono
              lime
            />
          </div>
        </Card>

        <Card title="Identity">
          <KV k="Slug" v={row.slug} mono />
          <KV k="Wallet" v={row.wallet_ref ?? 'None attached'} />
          <KV k="Entity" v={entityRow?.full_name ?? '—'} />
          <KV k="Mode" v={row.is_live ? 'Live' : 'Idle'} />
        </Card>

        <Card title="Models & vendors">
          <div className="chips" style={{ marginBottom: 12 }}>
            {row.models_list.length === 0 ? (
              <span className="mut">No models recorded</span>
            ) : (
              row.models_list.map((m) => (
                <span className="chipv" key={m}>
                  {m}
                </span>
              ))
            )}
          </div>
          <div className="chips">
            {row.vendors_list.length === 0 ? (
              <span className="mut">No vendors recorded</span>
            ) : (
              row.vendors_list.map((v) => (
                <span className="chipv" key={v}>
                  {v}
                </span>
              ))
            )}
          </div>
        </Card>

        {canSee('policies') ? (
          <Card title="Governing policies">
            {policies.data && policies.data.length > 0 ? (
              policies.data.map((policy) => (
                <div className="mini-row" key={policy.id}>
                  <span className="mg" aria-hidden>
                    ⚖
                  </span>
                  <span className="mm">
                    {policy.name}
                    <small>{policy.scope_type === 'company' ? 'company-wide' : 'this agent'}</small>
                  </span>
                  <span className="mv">{policy.block_count} blocked</span>
                </div>
              ))
            ) : (
              <span className="mut">No active policies govern this agent.</span>
            )}
          </Card>
        ) : null}

        {canSee('transactions') ? (
          <Card className="full2" title="Recent spend">
            {txns.data && txns.data.length > 0 ? (
              txns.data.map((txn) => (
                <button
                  key={txn.id}
                  type="button"
                  className="mini-row"
                  style={{ width: '100%', textAlign: 'left' }}
                  onClick={() => navigate(`/transactions/${txn.id}`)}
                >
                  <span className="mg" aria-hidden>
                    ⇄
                  </span>
                  <span className="mm">
                    {txn.item_description ?? txn.vendor}
                    <small>{new Date(txn.transaction_date).toLocaleDateString()}</small>
                  </span>
                  <Tag tag={transactionTag(txn)} />
                  <span className="mv">{formatMoney(txn.paid_amount, txn.paid_currency)}</span>
                </button>
              ))
            ) : (
              <Empty title="No spend yet" />
            )}
          </Card>
        ) : null}

        {canSee('memory') ? (
          <Card className="full2" title="What the company learned">
            {memories.data && memories.data.length > 0 ? (
              memories.data.map((memory) => (
                <div className="mini-row" key={memory.id}>
                  <span className="mg" aria-hidden>
                    ❧
                  </span>
                  <span className="mm">
                    {memory.summary}
                    <small>{memory.detail_text}</small>
                  </span>
                </div>
              ))
            ) : (
              <span className="mut">
                Nothing learned yet — patterns appear after a few weeks of spend.
              </span>
            )}
          </Card>
        ) : null}
      </div>
    </div>
  )
}
