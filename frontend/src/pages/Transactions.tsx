/**
 * Transactions. Each chip reads exactly one endpoint (blocked/held spends are
 * authorizations, not transactions) so pagination stays correct.
 */

import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, FilterChips, Loading, Money, PageHeader, Tag } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { transactionTag, verdictTag } from '@/lib/verdict'
import { useEntityStore } from '@/store/entityStore'
import type { Authorization, Paginated, Transaction } from '@/types/domain'
import { verdictReason } from '@/types/domain'

type ChipKey = 'captured' | 'estimated' | 'reversed' | 'blocked' | 'review' | 'human_hold'

interface ChipSource {
  label: string
  path: '/transactions' | '/authorizations'
  params: Record<string, string>
}

/** One chip, one endpoint, one status value. */
const SOURCES: Record<ChipKey, ChipSource> = {
  captured: { label: 'Captured', path: '/transactions', params: {} },
  estimated: { label: 'Estimated', path: '/transactions', params: { estimated: 'true' } },
  reversed: { label: 'Reversed', path: '/transactions', params: { reversed: 'true' } },
  blocked: { label: 'Blocked', path: '/authorizations', params: { status: 'blocked' } },
  review: { label: 'Flagged', path: '/authorizations', params: { status: 'review' } },
  human_hold: { label: 'Held', path: '/authorizations', params: { status: 'human_hold' } },
}

const COLUMNS = '1.5fr 1.4fr 0.9fr 0.9fr 1fr'

function isAuthorizationChip(chip: ChipKey): boolean {
  return SOURCES[chip].path === '/authorizations'
}

export function Transactions(): ReactNode {
  const [chip, setChip] = useState<ChipKey>('captured')
  const { userId, entity } = useScope()
  const entities = useEntityStore((s) => s.entities)
  const navigate = useNavigate()
  const source = SOURCES[chip]

  const rows = useLiveQuery(scope('txn-list', userId, entity, chip), async () => {
    const { data } = await api.get<Paginated<Transaction | Authorization>>(source.path, {
      params: { ...source.params, ...entityParam(entity), limit: 100 },
    })
    return data
  })

  // `limit=1` reads: the response `total` gives the count.
  const counts = useLiveQuery(scope('txn-counts', userId, entity), async () => {
    const ask = async (path: string, params: Record<string, string>): Promise<number> => {
      const { data } = await api.get<{ total: number }>(path, {
        params: { ...params, ...entityParam(entity), limit: 1 },
      })
      return data.total
    }
    const [captured, blocked, held] = await Promise.all([
      ask('/transactions', {}),
      ask('/authorizations', { status: 'blocked' }),
      ask('/authorizations', { status: 'human_hold' }),
    ])
    return { captured, blocked, held }
  })

  const currencyFor = (entityId: string): string =>
    entities.find((e) => e.id === entityId)?.currency ?? 'USD'

  return (
    <div className="page wide">
      <PageHeader
        title="Transactions"
        sub={
          counts.data ? (
            <>
              <b>{counts.data.captured}</b> captured · <b>{counts.data.blocked}</b> blocked before
              money moved · <b>{counts.data.held}</b> awaiting approval
            </>
          ) : (
            'Every agent-initiated spend, governed at the moment it happens.'
          )
        }
      />

      <FilterChips
        chips={(Object.keys(SOURCES) as ChipKey[]).map((key) => ({
          value: key,
          label: SOURCES[key].label,
        }))}
        active={chip}
        onChange={setChip}
      />

      {rows.isPending ? <Loading /> : null}
      {rows.isError ? (
        <Failure failure={describeFailure(rows.error)} onRetry={() => void rows.refetch()} />
      ) : null}

      {rows.data && rows.data.items.length === 0 ? (
        <Empty
          title={`Nothing ${SOURCES[chip].label.toLowerCase()}`}
          hint={
            isAuthorizationChip(chip)
              ? 'No spend has hit this verdict yet.'
              : 'No transactions match this filter.'
          }
        />
      ) : null}

      {rows.data && rows.data.items.length > 0 ? (
        <div className="tbl-wrap">
          <div className="tbl">
            <div className="tbl-h" style={{ gridTemplateColumns: COLUMNS }}>
              <span>{isAuthorizationChip(chip) ? 'Agent' : 'Purchase'}</span>
              <span>Vendor</span>
              <span>Project</span>
              <span className="amt">Amount</span>
              <span>Verdict</span>
            </div>

            {isAuthorizationChip(chip)
              ? (rows.data.items as Authorization[]).map((row) => (
                  <div key={row.id} className="tbl-r static" style={{ gridTemplateColumns: COLUMNS }}>
                    <span className="glyphn">
                      <span className="gy" aria-hidden>
                        {row.agent_glyph}
                      </span>
                      <span className="c-main">
                        {row.agent_name}
                        <small>{row.reason}</small>
                      </span>
                    </span>
                    <span className="c-main" style={{ fontWeight: 400 }}>
                      {row.vendor}
                      <small>{verdictReason(row.verdict_code) ?? row.reason_text ?? '—'}</small>
                    </span>
                    <span className="mut">{row.project ?? '—'}</span>
                    <span className="amt">
                      <Money amount={row.amount} currency={row.currency ?? 'USD'} />
                    </span>
                    <span>
                      <Tag tag={verdictTag(row.status)} />
                    </span>
                  </div>
                ))
              : (rows.data.items as Transaction[]).map((row) => (
                  <button
                    key={row.id}
                    type="button"
                    className="tbl-r"
                    style={{ gridTemplateColumns: COLUMNS }}
                    onClick={() => navigate(`/transactions/${row.id}`)}
                  >
                    <span className="c-main">
                      {row.item_description ?? row.intent ?? 'Spend'}
                      <small>{new Date(row.transaction_date).toLocaleString()}</small>
                    </span>
                    <span className="c-main" style={{ fontWeight: 400 }}>
                      {row.vendor}
                      <small>{row.model_used ?? row.source}</small>
                    </span>
                    <span className="mut">{row.project ?? '—'}</span>
                    <span className="amt">
                      <Money amount={row.paid_amount} currency={row.paid_currency} />
                      <small>
                        {row.local_currency !== row.paid_currency
                          ? `${row.local_currency} ${row.local_amount}`
                          : currencyFor(row.entity_id)}
                      </small>
                    </span>
                    <span>
                      <Tag tag={transactionTag(row)} />
                    </span>
                  </button>
                ))}
          </div>
          <div className="tcount" style={{ paddingTop: 12 }}>
            {rows.data.items.length} of {rows.data.total}
          </div>
        </div>
      ) : null}
    </div>
  )
}
