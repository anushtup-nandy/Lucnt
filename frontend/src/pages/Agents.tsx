/** Agents. Scoping is server-side only; never filter client-side. */

import type { ReactNode } from 'react'
import { useNavigate } from 'react-router'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Bar, Empty, Failure, Loading, Money, PageHeader, Tag } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { pctUsed } from '@/lib/money'
import { agentTag } from '@/lib/verdict'
import { useEntityStore } from '@/store/entityStore'

export interface Agent {
  id: string
  entity_id: string
  name: string
  slug: string
  glyph: string
  role: string
  wallet_ref: string | null
  models_list: string[]
  vendors_list: string[]
  monthly_budget: string
  spent_ytd: string
  spent_today: string
  status: string
  is_live: boolean
  confidence_pct: number
}

const COLUMNS = '1.6fr 1fr 1.3fr 0.8fr 0.7fr'

export function Agents(): ReactNode {
  const { userId, entity } = useScope()
  const entities = useEntityStore((s) => s.entities)
  const navigate = useNavigate()

  const query = useLiveQuery(scope('agents', userId, entity), async () => {
    const { data } = await api.get<{ items: Agent[]; total: number }>('/agents', {
      params: { ...entityParam(entity), limit: 200 },
    })
    return data
  })

  const currencyFor = (entityId: string): string =>
    entities.find((e) => e.id === entityId)?.currency ?? 'USD'

  return (
    <div className="page">
      <PageHeader
        title="Agents"
        sub={
          query.data ? (
            <>
              <b>{query.data.total}</b> agent{query.data.total === 1 ? '' : 's'} under governance
            </>
          ) : undefined
        }
      />

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && query.data.items.length === 0 ? (
        <Empty
          title="No agents here"
          hint="Agents appear once the SDK authorizes its first spend for them."
        />
      ) : null}

      {query.data && query.data.items.length > 0 ? (
        <div className="tbl-wrap">
          <div className="tbl" role="table">
            <div className="tbl-h" style={{ gridTemplateColumns: COLUMNS }} role="row">
              <span>Agent</span>
              <span>Owner role</span>
              <span>Monthly budget</span>
              <span className="amt">Today</span>
              <span>Status</span>
            </div>
            {query.data.items.map((agent) => {
              const pct = pctUsed(agent.spent_ytd, agent.monthly_budget)
              const currency = currencyFor(agent.entity_id)
              return (
                <button
                  key={agent.id}
                  type="button"
                  className="tbl-r"
                  style={{ gridTemplateColumns: COLUMNS }}
                  onClick={() => navigate(`/agents/${agent.id}`)}
                >
                  <span className="glyphn">
                    <span className="gy" aria-hidden>
                      {agent.glyph}
                    </span>
                    <span className="c-main">
                      {agent.name}
                      <small>{agent.slug}</small>
                    </span>
                  </span>
                  <span className="c-main" style={{ fontWeight: 400 }}>
                    {agent.role}
                  </span>
                  <span>
                    <Bar pct={pct} />
                    <small className="mut mono" style={{ fontSize: 11 }}>
                      {pct}% of <Money amount={agent.monthly_budget} currency={currency} decimals={0} />
                    </small>
                  </span>
                  <span className="amt">
                    <Money amount={agent.spent_today} currency={currency} />
                  </span>
                  <span>
                    <Tag tag={agentTag(agent.status)} />
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      ) : null}
    </div>
  )
}
