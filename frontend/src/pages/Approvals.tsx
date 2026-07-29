/**
 * Approvals — the `human_hold` queue, and the `approver` persona's landing page.
 *
 * Every row here is money that has **not** moved. The hold was taken, policy
 * routed it to a human, and the agent is blocked until someone answers. That is
 * why the age column exists: an unanswered queue is an agent that has stopped
 * working.
 */

import type { ReactNode } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { formatDistanceToNowStrict } from 'date-fns'

import { api, describeFailure } from '@/api/client'
import { scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, Loading, Money, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { useAuthStore } from '@/store/authStore'
import { useUiStore } from '@/store/uiStore'
import type { Approval, Paginated } from '@/types/domain'

export function Approvals(): ReactNode {
  const { userId, entity } = useScope()
  const canApprove = useAuthStore((s) => s.session?.perms.can_approve ?? false)
  const pushToast = useUiStore((s) => s.pushToast)
  const queryClient = useQueryClient()
  const key = scope('approvals', userId, entity)

  const query = useLiveQuery(key, async () => {
    const { data } = await api.get<Paginated<Approval>>('/approvals', { params: { limit: 100 } })
    return data
  })

  const act = useMutation({
    mutationFn: async ({ id, decision }: { id: string; decision: 'approve' | 'reject' }) => {
      await api.post(`/approvals/${id}/${decision}`)
    },
    onSuccess: (_data, variables) => {
      pushToast(variables.decision === 'approve' ? 'Approved — the agent may spend' : 'Rejected')
      void queryClient.invalidateQueries({ queryKey: key })
    },
    // Approving re-checks the budget server-side, so an approval can legitimately
    // fail. Surfacing that is the point: a silently-failed approval leaves the
    // agent blocked while the queue looks clear.
    onError: (error: unknown) => pushToast(describeFailure(error).message, 'bad'),
  })

  return (
    <div className="page wide">
      <PageHeader
        title="Approvals"
        sub={
          query.data
            ? `${query.data.total} spend${query.data.total === 1 ? '' : 's'} held before money moved.`
            : 'Spends policy routed to a human.'
        }
      />

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && query.data.items.length === 0 ? (
        <Empty title="Queue is clear" hint="No agent is waiting on a human right now." />
      ) : null}

      {query.data?.items.map((approval) => (
        <div className="ex" key={approval.id}>
          <div className="ex-r" style={{ cursor: 'default' }}>
            <span className="xi" aria-hidden>
              ✓
            </span>
            <span className="xm">
              <span className="xt">{approval.vendor}</span>
              <span className="xs">
                {approval.reason_text ?? approval.reason ?? 'Held for human approval'}
              </span>
            </span>
            <span className="xage">
              {formatDistanceToNowStrict(new Date(approval.created_at))} old
            </span>
            <span className="xamt">
              <Money amount={approval.amount} currency={approval.currency ?? 'USD'} />
            </span>
            <span className="ex-acts">
              <button
                type="button"
                className="btn sm lime"
                disabled={!canApprove || act.isPending}
                onClick={() => act.mutate({ id: approval.id, decision: 'approve' })}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn sm"
                disabled={!canApprove || act.isPending}
                onClick={() => act.mutate({ id: approval.id, decision: 'reject' })}
              >
                Reject
              </button>
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}
