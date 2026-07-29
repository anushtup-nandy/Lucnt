/** Insights — "your CFO, always on". One card per finding, in serif. */

import type { ReactNode } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { useUiStore } from '@/store/uiStore'
import type { Insight, Paginated } from '@/types/domain'

const QUESTIONS: Record<string, string> = {
  cost_anomaly: 'Is anything unusual?',
  vendor_concentration: 'Who are we dependent on?',
  budget_forecast: 'Will we run out?',
  savings_opportunity: 'Where is money leaking?',
  tax_optimisation: 'Are we reclaiming everything?',
}

export function Insights(): ReactNode {
  const { userId, entity } = useScope()
  const pushToast = useUiStore((s) => s.pushToast)
  const queryClient = useQueryClient()
  const key = scope('insights', userId, entity)

  const query = useLiveQuery(key, async () => {
    const { data } = await api.get<Paginated<Insight> & { unapplied_count: number }>('/insights', {
      params: { ...entityParam(entity), limit: 50 },
    })
    return data
  })

  const act = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: 'apply' | 'dismiss' }) => {
      await api.post(`/insights/${id}/${action}`)
    },
    onSuccess: (_data, variables) => {
      pushToast(variables.action === 'apply' ? 'Applied' : 'Dismissed')
      void queryClient.invalidateQueries({ queryKey: key })
    },
    onError: (error: unknown) => pushToast(describeFailure(error).message, 'bad'),
  })

  return (
    <div className="page">
      <PageHeader
        title="Insights"
        sub="Patterns found in your own ledger — no model, no guesswork."
      />

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && query.data.items.length === 0 ? (
        <Empty
          title="Nothing to report"
          hint="Insights are recomputed as spend accumulates."
        />
      ) : null}

      {query.data?.items.map((insight) => (
        <article className="ins" key={insight.id}>
          <div className="ins-q">
            {QUESTIONS[insight.insight_type] ?? 'What changed?'}
            <span className="et">{insight.insight_type.replace(/_/g, ' ')}</span>
          </div>
          <h2 className="ins-h">{insight.title}</h2>
          <p className="ins-w">{insight.body}</p>
          <div className="ins-f">
            {insight.value_text ? (
              <div className="ins-i">
                <div className="k">Impact</div>
                <div className="v lime">{insight.value_text}</div>
              </div>
            ) : null}
            <div className="ins-cta">
              {insight.action_label ? (
                <button
                  type="button"
                  className="btn lime"
                  disabled={act.isPending}
                  onClick={() => act.mutate({ id: insight.id, action: 'apply' })}
                >
                  {insight.action_label}
                </button>
              ) : null}
              <button
                type="button"
                className="btn"
                disabled={act.isPending}
                onClick={() => act.mutate({ id: insight.id, action: 'dismiss' })}
              >
                Dismiss
              </button>
            </div>
          </div>
        </article>
      ))}
    </div>
  )
}
