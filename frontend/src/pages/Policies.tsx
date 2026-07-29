/**
 * Policies — one card per rule, with the count of what it actually stopped.
 *
 * "Blocked N times" is the number that makes a policy real. A toggle with no
 * evidence behind it is a settings screen; a toggle that says it refused four
 * spends is a control.
 */

import type { ReactNode } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, describeFailure } from '@/api/client'
import { scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { useAuthStore } from '@/store/authStore'
import { useUiStore } from '@/store/uiStore'
import type { Paginated, Policy } from '@/types/domain'

const RULE_ICONS: Record<string, string> = {
  budget_cap: '◧',
  merchant_list: '⊘',
  velocity: '⏱',
  amount_threshold: '⚖',
  time_of_day: '☾',
}

/** Render a rule's config as the sentence it actually enforces. */
function describeRule(policy: Policy): string {
  const config = policy.config
  switch (policy.rule_type) {
    case 'merchant_list': {
      const mode = String(config['mode'] ?? 'block')
      const vendors = Array.isArray(config['vendors']) ? (config['vendors'] as string[]) : []
      return `${mode === 'block' ? 'Blocks' : 'Allows only'} ${vendors.join(', ') || '—'}`
    }
    case 'amount_threshold':
      return `Single spends over ${String(config['max_amount'] ?? '?')} are ${String(config['action'] ?? 'reviewed')}`
    case 'velocity':
      return `At most ${String(config['max_count'] ?? '?')} spends per ${String(config['window'] ?? 'window')}`
    case 'budget_cap':
      return `Extra ${String(config['period'] ?? 'daily')} cap of ${String(config['limit'] ?? '?')}`
    case 'time_of_day':
      return `Only between ${String(config['start'] ?? '?')} and ${String(config['end'] ?? '?')}`
    default:
      return policy.description ?? policy.rule_type
  }
}

export function Policies(): ReactNode {
  const { userId, entity } = useScope()
  const canWrite = useAuthStore((s) => s.session?.perms.can_write_policies ?? false)
  const pushToast = useUiStore((s) => s.pushToast)
  const queryClient = useQueryClient()
  const key = scope('policies', userId, entity)

  const query = useLiveQuery(key, async () => {
    const { data } = await api.get<Paginated<Policy>>('/policies', { params: { limit: 200 } })
    return data
  })

  const toggle = useMutation({
    mutationFn: async ({ id, active }: { id: string; active: boolean }) => {
      await api.patch(`/policies/${id}`, { active })
    },
    onSuccess: (_data, variables) => {
      pushToast(variables.active ? 'Policy enabled' : 'Policy paused')
      void queryClient.invalidateQueries({ queryKey: key })
    },
    onError: (error: unknown) => pushToast(describeFailure(error).message, 'bad'),
  })

  return (
    <div className="page">
      <PageHeader
        title="Policies"
        sub={
          canWrite
            ? 'Rules evaluated before every spend, most-restrictive-wins.'
            : 'Read-only for your role.'
        }
      />

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && query.data.items.length === 0 ? (
        <Empty
          title="No policies yet"
          hint="Without a policy, only the always-on monthly budget invariant applies."
        />
      ) : null}

      {query.data?.items.map((policy) => (
        <div className="pol" key={policy.id}>
          <span className="pg" aria-hidden>
            {RULE_ICONS[policy.rule_type] ?? '⚖'}
          </span>
          <div className="pm">
            <div className="pt">
              <b>{policy.name}</b> — {describeRule(policy)}
            </div>
            <div className="pmeta">
              <span className="pscope">
                {policy.scope_type === 'company' ? 'Company-wide' : 'One agent'}
              </span>
              <span className={`tag ${policy.block_count > 0 ? 'bad' : ''}`}>
                <i className="d" />
                blocked {policy.block_count} times
              </span>
              <span className="tag">
                <i className="d" />
                evaluated {policy.hit_count}
              </span>
              {!policy.active ? (
                <span className="tag warn">
                  <i className="d" />
                  paused
                </span>
              ) : null}
            </div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={policy.active}
            aria-label={`${policy.active ? 'Pause' : 'Enable'} ${policy.name}`}
            className={`toggle ${policy.active ? 'on' : ''}`}
            disabled={!canWrite || toggle.isPending}
            onClick={() => toggle.mutate({ id: policy.id, active: !policy.active })}
          />
        </div>
      ))}
    </div>
  )
}
