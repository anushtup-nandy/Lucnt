/**
 * Books: one subtab per `/books/*` endpoint. Shows the server's `balanced` flag
 * rather than re-summing; statements are grouped by currency (no FX).
 */

import { useState, type ReactNode } from 'react'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { formatMoney } from '@/lib/money'
import type {
  BalanceSheetResponse,
  BookLine,
  CashFlowResponse,
  GeneralLedgerResponse,
  JournalsResponse,
  ProfitAndLossResponse,
  TrialBalanceResponse,
} from '@/types/domain'

type TabKey = 'journals' | 'gl' | 'tb' | 'pl' | 'bs' | 'cf'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'journals', label: 'Journal explorer' },
  { key: 'gl', label: 'General ledger' },
  { key: 'tb', label: 'Trial balance' },
  { key: 'pl', label: 'P&L' },
  { key: 'bs', label: 'Balance sheet' },
  { key: 'cf', label: 'Cash flow' },
]

function Row({
  name,
  code,
  value,
  currency,
  variant,
}: {
  name: string
  code?: string
  value: string
  currency: string
  variant?: 'head' | 'sub' | 'grand' | 'ind'
}): ReactNode {
  return (
    <div className={`sr ${variant ?? ''}`}>
      <span className="sn">
        {name}
        {code ? <small>{code}</small> : null}
      </span>
      <span className="sv">{formatMoney(value, currency, { bare: true })}</span>
    </div>
  )
}

function Lines({ title, lines }: { title: string; lines: BookLine[] }): ReactNode {
  if (lines.length === 0) return null
  return (
    <>
      <Row name={title} value="" currency="" variant="head" />
      {lines.map((line) => (
        <Row
          key={`${line.account_code}-${line.currency}`}
          name={line.account_name}
          code={line.account_code}
          value={line.amount}
          currency={line.currency}
        />
      ))}
    </>
  )
}

function BalancedBadge({ balanced }: { balanced: boolean }): ReactNode {
  return (
    <span className={`tag ${balanced ? 'ok' : 'bad'}`}>
      <i className="d" />
      {balanced ? 'Balanced' : 'Out of balance'}
    </span>
  )
}

function JournalExplorer(): ReactNode {
  const { userId, entity } = useScope()
  const [openId, setOpenId] = useState<string | null>(null)
  const query = useLiveQuery(scope('books-journals', userId, entity), async () => {
    const { data } = await api.get<JournalsResponse>('/books/journals', {
      params: { ...entityParam(entity), limit: 50 },
    })
    return data
  })

  if (query.isPending) return <Loading />
  if (query.isError) {
    return <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
  }
  if (!query.data || query.data.items.length === 0) {
    return <Empty title="No journal entries yet" hint="Entries appear as spends are captured." />
  }

  return (
    <>
      {query.data.items.map((group) => {
        const open = openId === group.transaction_id
        return (
          <div className={`jx ${open ? 'open' : ''}`} key={group.transaction_id}>
            <button
              type="button"
              className="jx-r"
              aria-expanded={open}
              onClick={() => setOpenId(open ? null : group.transaction_id)}
            >
              <span className="g" aria-hidden>
                ▤
              </span>
              <span className="m">
                <span className="t">{group.vendor}</span>
                <span className="s">
                  {new Date(group.date).toLocaleDateString()} · {group.lines.length} lines
                </span>
              </span>
              <span className="v">
                {formatMoney(group.total_debit, group.currency, { bare: true })}
              </span>
              <span className="cv">›</span>
            </button>
            {open ? (
              <div className="jx-body">
                <div className="jx-bi">
                  {group.lines.map((line, index) => (
                    <div className="je" key={`${line.account_code}-${index}`}>
                      <span className="ja">
                        {line.account_name}
                        <small>
                          {line.account_code} · {line.ledger}
                        </small>
                      </span>
                      <span className={`jd ${Number(line.debit) === 0 ? 'z' : ''}`}>
                        {formatMoney(line.debit, line.currency, { bare: true })}
                      </span>
                      <span className={`jc ${Number(line.credit) === 0 ? 'z' : ''}`}>
                        {formatMoney(line.credit, line.currency, { bare: true })}
                      </span>
                    </div>
                  ))}
                  <div className="je tot">
                    <span className="ja">Total</span>
                    <span className="jd">
                      {formatMoney(group.total_debit, group.currency, { bare: true })}
                    </span>
                    <span className="jc">
                      {formatMoney(group.total_credit, group.currency, { bare: true })}
                    </span>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        )
      })}
    </>
  )
}

function GeneralLedger(): ReactNode {
  const { userId, entity } = useScope()
  const query = useLiveQuery(scope('books-gl', userId, entity), async () => {
    const { data } = await api.get<GeneralLedgerResponse>('/books/gl', {
      params: entityParam(entity),
    })
    return data
  })
  if (query.isPending) return <Loading />
  if (query.isError) {
    return <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
  }
  if (!query.data || query.data.accounts.length === 0) return <Empty title="No accounts posted to yet" />
  return (
    <div className="st">
      {query.data.accounts.map((account) => (
        <Row
          key={`${account.account_code}-${account.currency}`}
          name={account.account_name}
          code={`${account.account_code} · ${account.category}`}
          value={account.balance}
          currency={account.currency}
        />
      ))}
    </div>
  )
}

function TrialBalance(): ReactNode {
  const { userId, entity } = useScope()
  const query = useLiveQuery(scope('books-tb', userId, entity), async () => {
    const { data } = await api.get<TrialBalanceResponse>('/books/tb', {
      params: entityParam(entity),
    })
    return data
  })
  if (query.isPending) return <Loading />
  if (query.isError) {
    return <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
  }
  if (!query.data || query.data.rows.length === 0) return <Empty title="Nothing posted yet" />

  return (
    <div className="st">
      <div className="st-meta">
        <b>Trial balance</b> — the backend asserts the balance; this page reports it.{' '}
        <BalancedBadge balanced={query.data.balanced} />
      </div>
      {query.data.rows.map((row) => (
        <div className="sr" key={`${row.account_code}-${row.currency}`}>
          <span className="sn">
            {row.account_name}
            <small>{row.account_code}</small>
          </span>
          <span className="sv">
            {formatMoney(row.debit, row.currency, { bare: true })} /{' '}
            {formatMoney(row.credit, row.currency, { bare: true })}
          </span>
        </div>
      ))}
      {query.data.currencies.map((totals) => (
        <div className="sr sub" key={totals.currency}>
          <span className="sn">Total {totals.currency}</span>
          <span className="sv">
            {formatMoney(totals.total_debit, totals.currency, { bare: true })} /{' '}
            {formatMoney(totals.total_credit, totals.currency, { bare: true })}
          </span>
        </div>
      ))}
    </div>
  )
}

function ProfitAndLoss(): ReactNode {
  const { userId, entity } = useScope()
  const query = useLiveQuery(scope('books-pl', userId, entity), async () => {
    const { data } = await api.get<ProfitAndLossResponse>('/books/pl', {
      params: entityParam(entity),
    })
    return data
  })
  if (query.isPending) return <Loading />
  if (query.isError) {
    return <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
  }
  if (!query.data) return null
  const empty = query.data.income.length === 0 && query.data.expenses.length === 0
  if (empty) return <Empty title="No income or expense posted yet" />
  return (
    <div className="st">
      <Lines title="Income" lines={query.data.income} />
      <Lines title="Expenses" lines={query.data.expenses} />
      {query.data.currencies.map((totals) => (
        <div className="sr grand" key={totals.currency}>
          <span className="sn">Net profit ({totals.currency})</span>
          <span className="sv">{formatMoney(totals.net_profit, totals.currency, { bare: true })}</span>
        </div>
      ))}
    </div>
  )
}

function BalanceSheet(): ReactNode {
  const { userId, entity } = useScope()
  const query = useLiveQuery(scope('books-bs', userId, entity), async () => {
    const { data } = await api.get<BalanceSheetResponse>('/books/bs', {
      params: entityParam(entity),
    })
    return data
  })
  if (query.isPending) return <Loading />
  if (query.isError) {
    return <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
  }
  if (!query.data) return null
  const empty =
    query.data.assets.length === 0 &&
    query.data.liabilities.length === 0 &&
    query.data.equity.length === 0
  if (empty) return <Empty title="Nothing on the balance sheet yet" />
  return (
    <div className="st">
      <div className="st-meta">
        <BalancedBadge balanced={query.data.balanced} />
      </div>
      <Lines title="Assets" lines={query.data.assets} />
      <Lines title="Liabilities" lines={query.data.liabilities} />
      <Lines title="Equity" lines={query.data.equity} />
      {query.data.currencies.map((totals) => (
        <div className="sr grand" key={totals.currency}>
          <span className="sn">Assets ({totals.currency})</span>
          <span className="sv">
            {formatMoney(totals.total_assets, totals.currency, { bare: true })}
          </span>
        </div>
      ))}
    </div>
  )
}

function CashFlow(): ReactNode {
  const { userId, entity } = useScope()
  const query = useLiveQuery(scope('books-cf', userId, entity), async () => {
    const { data } = await api.get<CashFlowResponse>('/books/cf', { params: entityParam(entity) })
    return data
  })
  if (query.isPending) return <Loading />
  if (query.isError) {
    return <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
  }
  if (!query.data || query.data.operating.length === 0) return <Empty title="No cash movement yet" />
  return (
    <div className="st">
      <Lines title="Operating" lines={query.data.operating} />
      {query.data.currencies.map((totals) => (
        <div className="sr grand" key={totals.currency}>
          <span className="sn">Net cash flow ({totals.currency})</span>
          <span className="sv">
            {formatMoney(totals.net_cash_flow, totals.currency, { bare: true })}
          </span>
        </div>
      ))}
    </div>
  )
}

const PANELS: Record<TabKey, () => ReactNode> = {
  journals: JournalExplorer,
  gl: GeneralLedger,
  tb: TrialBalance,
  pl: ProfitAndLoss,
  bs: BalanceSheet,
  cf: CashFlow,
}

export function Books(): ReactNode {
  const [tab, setTab] = useState<TabKey>('journals')
  const Panel = PANELS[tab]
  return (
    <div className="page wide">
      <PageHeader title="Books" sub="Double-entry, produced at the moment of spend." />
      <div className="subtabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={t.key === tab}
            className={`subtab ${t.key === tab ? 'on' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <Panel />
    </div>
  )
}
