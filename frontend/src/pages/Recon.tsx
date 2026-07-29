/**
 * Reconciliation — an inbox, ordered by money at stake.
 *
 * `POST /recon/scan` is limited to **5 per minute per principal** and the 429 is
 * surfaced honestly rather than swallowed into a spinner that never resolves
 * (§11). Scanning walks the whole ledger; that limit is the reason the endpoint
 * is safe to expose at all.
 *
 * Totals are per currency because they arrive per currency: ranking ₹5,000
 * against $100 without an FX conversion produces an ordering that is wrong.
 */

import { useState, type ReactNode } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { formatDistanceToNowStrict } from 'date-fns'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, Loading, PageHeader, Tag } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { formatMoney } from '@/lib/money'
import { reconTag } from '@/lib/verdict'
import { useUiStore } from '@/store/uiStore'
import type { ReconListResponse } from '@/types/domain'

const TYPE_LABELS: Record<string, string> = {
  duplicate_charge: 'Duplicate charge',
  failed_delivery: 'Paid but not delivered',
  price_drift: 'Price drift',
  missing_invoice: 'Missing invoice',
  orphan_charge: 'Charge with no agent',
  amount_mismatch: 'Amount mismatch',
}

export function Recon(): ReactNode {
  const { userId, entity } = useScope()
  const [openId, setOpenId] = useState<string | null>(null)
  const pushToast = useUiStore((s) => s.pushToast)
  const queryClient = useQueryClient()
  const key = scope('recon', userId, entity)

  const query = useLiveQuery(key, async () => {
    const { data } = await api.get<ReconListResponse>('/recon', {
      params: { ...entityParam(entity), status: 'open', limit: 100 },
    })
    return data
  })

  const scan = useMutation({
    mutationFn: async () => {
      const { data } = await api.post<{ created: number }>('/recon/scan')
      return data
    },
    onSuccess: (data) => {
      pushToast(
        data.created > 0 ? `Found ${data.created} new exception(s)` : 'Scan found nothing new',
      )
      void queryClient.invalidateQueries({ queryKey: key })
    },
    onError: (error: unknown) => {
      const failure = describeFailure(error)
      pushToast(
        failure.status === 429
          ? `Scan is limited to 5/min${failure.retryAfter ? ` — retry in ${failure.retryAfter}s` : ''}`
          : failure.message,
        'bad',
      )
    },
  })

  const resolve = useMutation({
    mutationFn: async (id: string) => {
      await api.post(`/recon/${id}/resolve`, { resolution: 'recovered' })
    },
    onSuccess: () => {
      pushToast('Marked recovered')
      void queryClient.invalidateQueries({ queryKey: key })
    },
    onError: (error: unknown) => pushToast(describeFailure(error).message, 'bad'),
  })

  const stakes = Object.entries(query.data?.total_at_stake ?? {})

  return (
    <div className="page">
      <PageHeader
        title="Reconciliation"
        sub="Money the ledger says you are owed, ranked by how much."
        actions={
          <button
            type="button"
            className="btn"
            disabled={scan.isPending}
            onClick={() => scan.mutate()}
          >
            {scan.isPending ? 'Scanning…' : 'Scan ledger'}
          </button>
        }
      />

      {stakes.length > 0 ? (
        <div className="inbox-top">
          {stakes.map(([currency, amount]) => (
            <span key={currency} style={{ marginRight: 18 }}>
              <b className="mono">{formatMoney(amount, currency)}</b> at stake
            </span>
          ))}
        </div>
      ) : null}

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && query.data.items.length === 0 ? (
        <Empty
          title="Inbox zero"
          hint="Nothing is unreconciled. Run a scan if you have posted spend outside the SDK."
        />
      ) : null}

      {query.data?.items.map((exception) => {
        const open = openId === exception.id
        const evidence = Object.entries(exception.evidence ?? {})
        return (
          <div className={`ex ${open ? 'open' : ''}`} key={exception.id}>
            <button
              type="button"
              className="ex-r"
              aria-expanded={open}
              onClick={() => setOpenId(open ? null : exception.id)}
            >
              <span className="xi" aria-hidden>
                ⟳
              </span>
              <span className="xm">
                <span className="xt">
                  {TYPE_LABELS[exception.exception_type] ?? exception.exception_type}
                </span>
                <span className="xs">{exception.fix_notes ?? 'Detected by the recovery agent'}</span>
              </span>
              <span className="xage">
                {formatDistanceToNowStrict(new Date(exception.created_at))} old
              </span>
              <span className="xamt">
                {formatMoney(exception.amount_at_stake, exception.currency ?? 'USD')}
              </span>
              <span className="cv">›</span>
            </button>
            {open ? (
              <div className="ex-body">
                <div className="ex-bi">
                  <div className="ex-evi">
                    {evidence.length === 0 ? (
                      <div className="e">
                        <span className="ek">No structured evidence recorded</span>
                      </div>
                    ) : (
                      evidence.map(([k, v]) => (
                        <div className="e" key={k}>
                          <span className="ek">{k.replace(/_/g, ' ')}</span>
                          <span className="ev">{String(v)}</span>
                        </div>
                      ))
                    )}
                  </div>
                  <div className="ex-fix">
                    <b>Drafted fix:</b> {exception.fix_notes ?? 'Review the transaction and confirm.'}
                  </div>
                  <div className="ex-acts">
                    <Tag tag={reconTag(exception.status)} />
                    <button
                      type="button"
                      className="btn sm lime"
                      disabled={resolve.isPending}
                      onClick={() => resolve.mutate(exception.id)}
                    >
                      Mark recovered
                    </button>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
