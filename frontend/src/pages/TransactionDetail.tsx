/**
 * One transaction, in full: who spent, why, what tax applied, how it booked.
 *
 * The balanced badge is displayed as **fact, not as a client-side calculation**.
 * Σdebit == Σcredit is asserted at write time in the journal generator and
 * again by a database constraint; recomputing it here in floating point would
 * be strictly less trustworthy than the thing it claims to verify, and would
 * eventually disagree with it.
 */

import type { ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router'

import { api, describeFailure } from '@/api/client'
import { scope, useScope } from '@/api/queries/keys'
import { Failure, Loading, Tag } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { formatMoney } from '@/lib/money'
import { transactionTag } from '@/lib/verdict'
import { useEntityStore } from '@/store/entityStore'
import type { TransactionFull } from '@/types/domain'

function Field({ k, v }: { k: string; v: ReactNode }): ReactNode {
  return (
    <div className="qi">
      <span className="qk">{k}</span>
      <span className="qv">{v}</span>
    </div>
  )
}

export function TransactionDetail(): ReactNode {
  const { transactionId } = useParams<{ transactionId: string }>()
  const { userId, entity } = useScope()
  const entities = useEntityStore((s) => s.entities)
  const navigate = useNavigate()

  const query = useLiveQuery(
    scope('txn', userId, entity, transactionId),
    async () => {
      const { data } = await api.get<TransactionFull>(`/transactions/${transactionId}`)
      return data
    },
    { enabled: Boolean(transactionId) },
  )

  if (query.isPending) {
    return (
      <div className="page">
        <Loading rows={8} />
      </div>
    )
  }
  if (query.isError || !query.data) {
    return (
      <div className="page">
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      </div>
    )
  }

  const txn = query.data
  const entityRow = entities.find((e) => e.id === txn.entity_id)
  const currency = txn.paid_currency

  return (
    <div className="page">
      <div className="txd-h">
        <div style={{ minWidth: 0 }}>
          <h1 className="page-title">{txn.item_description ?? txn.intent ?? 'Spend'}</h1>
          <div className="page-sub">
            {txn.vendor} · {new Date(txn.transaction_date).toLocaleString()} ·{' '}
            <span className="mono">{txn.id.slice(0, 8)}</span>
          </div>
        </div>
        <div className="txd-amt">
          <div className="a">{formatMoney(txn.paid_amount, currency)}</div>
          <div className="s">
            {txn.local_currency !== currency
              ? `${formatMoney(txn.local_amount, txn.local_currency)} functional`
              : 'functional currency'}
          </div>
          <div style={{ marginTop: 6 }}>
            <Tag tag={transactionTag(txn)} />
          </div>
        </div>
      </div>

      <div className="txd-q">
        <Field
          k="Who spent it"
          v={
            <button
              type="button"
              className="mono"
              style={{ textDecoration: 'underline' }}
              onClick={() => navigate(`/agents/${txn.agent_id}`)}
            >
              {txn.agent_id.slice(0, 8)}
            </button>
          }
        />
        <Field k="What it bought" v={txn.item_description ?? txn.intent ?? '—'} />
        <Field
          k="Policy"
          v={
            txn.authorization_id ? (
              <>
                Authorized before payment
                <span className="sub mono">{txn.authorization_id.slice(0, 8)}</span>
              </>
            ) : (
              <>
                No pre-authorization
                <span className="sub">source: {txn.source}</span>
              </>
            )
          }
        />
        <Field
          k="Entity"
          v={
            entityRow ? (
              <>
                {entityRow.name}
                <span className="sub">{entityRow.full_name}</span>
              </>
            ) : (
              '—'
            )
          }
        />
        <Field k="Jurisdiction" v={txn.tax?.jurisdiction ?? entityRow?.tax_regime ?? '—'} />
        <Field
          k="Tax"
          v={
            txn.tax ? (
              <>
                {formatMoney(txn.tax.tax_amount, txn.tax.currency)} {txn.tax.tax_type}
                <span className="sub">
                  {txn.tax.reverse_charge ? 'reverse charge · ' : ''}
                  {txn.tax.is_reclaimable
                    ? `reclaimable ${formatMoney(txn.tax.reclaim_amount, txn.tax.currency)}`
                    : 'not reclaimable'}
                  {txn.tax.self_invoice_generated ? ' · self-invoiced' : ''}
                </span>
              </>
            ) : (
              'No tax record'
            )
          }
        />
        <Field
          k="Evidence"
          v={
            txn.invoice_url ? (
              <a href={txn.invoice_url} target="_blank" rel="noreferrer" className="mono">
                invoice ↗
              </a>
            ) : (
              'Not attached'
            )
          }
        />
        <Field
          k="Booked in"
          v={
            txn.journal.length > 0 ? (
              <>
                {txn.journal.length} journal lines
                <span className="sub">{txn.journal[0]?.ledger ?? ''}</span>
              </>
            ) : (
              'Not yet journalled'
            )
          }
        />
      </div>

      {txn.reason_text ? (
        <div className="txd-reason">
          <div className="rl">Why the agent spent</div>
          {txn.reason_text}
        </div>
      ) : null}

      {txn.failure_reason ? (
        <div className="txd-reason plain">
          <div className="rl">Failure</div>
          {txn.failure_reason}
        </div>
      ) : null}

      {txn.journal.length > 0 ? (
        <div className="jr">
          <h6>
            <span>Journal entry</span>
            {/* Asserted at write time, so displayed as fact — see the module note. */}
            <span className="bal">balanced</span>
          </h6>
          {txn.journal.map((line, index) => (
            <div className="je" key={`${line.account_code}-${index}`}>
              <span className="ja">
                {line.account_name}
                <small>
                  {line.account_code} · {line.ledger}
                </small>
              </span>
              <span className={`jd ${line.debit === '0.0000' ? 'z' : ''}`}>
                {formatMoney(line.debit, line.currency, { bare: true })}
              </span>
              <span className={`jc ${line.credit === '0.0000' ? 'z' : ''}`}>
                {formatMoney(line.credit, line.currency, { bare: true })}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="txd-links">
        <button type="button" className="btn" onClick={() => navigate(`/agents/${txn.agent_id}`)}>
          Open agent
        </button>
        <button type="button" className="btn" onClick={() => navigate('/books')}>
          See in books
        </button>
        <button type="button" className="btn" onClick={() => navigate('/transactions')}>
          Back to transactions
        </button>
      </div>
    </div>
  )
}
