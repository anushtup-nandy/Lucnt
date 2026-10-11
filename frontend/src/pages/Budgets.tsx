/**
 * Budgets — allocated / deployed / available, and a live stepper per agent.
 *
 * **`reserved` and `available` are shown, not just `spent`**. An
 * approved-but-uncaptured hold is real money: it has already been promised to a
 * vendor and the gate already counts it. A page that showed only `spent` would
 * tell a CFO they have headroom the policy engine will refuse to grant.
 *
 * The stepper is disabled without `perms.can_write_budgets` — and the endpoint
 * 403s regardless, which is the part that actually matters.
 */

import { useState, type ReactNode } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Bar, Card, Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import { formatMoney } from '@/lib/money'
import { useAuthStore } from '@/store/authStore'
import { useEntityStore } from '@/store/entityStore'
import { useUiStore } from '@/store/uiStore'
import type { AgentBudget, BudgetsResponse } from '@/types/domain'

/** One step of the stepper, in whole units of the entity's currency. */
const STEP = 1000

/** Sum decimal strings without floats: integer-cents arithmetic on strings. */
function sumMinor(values: string[]): string {
  let total = 0n
  for (const value of values) {
    const [whole = '0', frac = ''] = value.split('.')
    total += BigInt(whole) * 10000n + BigInt(frac.padEnd(4, '0').slice(0, 4))
  }
  const negative = total < 0n
  const abs = negative ? -total : total
  return `${negative ? '-' : ''}${abs / 10000n}.${String(abs % 10000n).padStart(4, '0')}`
}

function StepperRow({
  budget,
  currency,
  canWrite,
  onChange,
  pending,
}: {
  budget: AgentBudget
  currency: string
  canWrite: boolean
  onChange: (agentId: string, next: string) => void
  pending: boolean
}): ReactNode {
  const [draft, setDraft] = useState<string | null>(null)
  const shown = draft ?? budget.monthly_budget
  const pct = Math.round(Number(budget.pct_used))

  const bump = (delta: number): void => {
    const next = String(Math.max(0, Math.round(Number(shown) + delta)))
    setDraft(next)
    onChange(budget.id, next)
  }

  return (
    <div className="bgr">
      <span className="g" aria-hidden>
        ◧
      </span>
      <span className="m">
        <span className="nm">{budget.name}</span>
        <span className="rl">{budget.slug}</span>
      </span>
      <span className="barwrap">
        <span className="lab">
          <span>
            <span className="v">{formatMoney(budget.spent, currency)}</span>
            <span className="mut"> spent</span>
          </span>
          <span>
            <span className="v">{formatMoney(budget.available, currency)}</span>
            <span className="mut"> available</span>
            {Number(budget.reserved) > 0 ? (
              <span className="mut"> · {formatMoney(budget.reserved, currency)} on hold</span>
            ) : null}
          </span>
        </span>
        <Bar pct={pct} />
      </span>
      <span className="step">
        <button
          type="button"
          aria-label={`Decrease ${budget.name} budget`}
          disabled={!canWrite || pending}
          onClick={() => bump(-STEP)}
        >
          −
        </button>
        <span className="sv">{formatMoney(shown, currency, { decimals: 0 })}</span>
        <button
          type="button"
          aria-label={`Increase ${budget.name} budget`}
          disabled={!canWrite || pending}
          onClick={() => bump(STEP)}
        >
          +
        </button>
      </span>
    </div>
  )
}

export function Budgets(): ReactNode {
  const { userId, entity } = useScope()
  const entities = useEntityStore((s) => s.entities)
  const canWrite = useAuthStore((s) => s.session?.perms.can_write_budgets ?? false)
  const pushToast = useUiStore((s) => s.pushToast)
  const queryClient = useQueryClient()
  const key = scope('budgets', userId, entity)

  const query = useLiveQuery(key, async () => {
    const { data } = await api.get<BudgetsResponse>('/budgets', {
      params: { ...entityParam(entity), limit: 200 },
    })
    return data
  })

  const update = useMutation({
    mutationFn: async ({ agentId, next }: { agentId: string; next: string }) => {
      await api.patch(`/budgets/${agentId}`, { monthly_budget: next })
    },
    onSuccess: () => {
      pushToast('Budget updated')
      void queryClient.invalidateQueries({ queryKey: key })
    },
    onError: (error: unknown) => pushToast(describeFailure(error).message, 'bad'),
  })

  const currencyFor = (entityId: string): string =>
    entities.find((e) => e.id === entityId)?.currency ?? 'USD'

  const agents = query.data?.agents ?? []
  // Only meaningful within one currency; the consolidated view says so rather
  // than adding rupees to dollars.
  const currencies = new Set(agents.map((a) => currencyFor(a.entity_id)))
  const singleCurrency = currencies.size === 1 ? [...currencies][0] : null

  return (
    <div className="page wide">
      <PageHeader
        title="Budgets"
        sub={
          canWrite
            ? 'Adjust a limit and the next authorize() honours it.'
            : 'Read-only for your role.'
        }
      />

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data ? (
        <>
          {singleCurrency ? (
            <div className="bg-top">
              <div>
                <div className="n">
                  {formatMoney(sumMinor(agents.map((a) => a.monthly_budget)), singleCurrency, {
                    decimals: 0,
                  })}
                </div>
                <div className="l">Allocated</div>
              </div>
              <div>
                <div className="n">
                  {formatMoney(sumMinor(agents.map((a) => a.spent)), singleCurrency, {
                    decimals: 0,
                  })}
                </div>
                <div className="l">Deployed</div>
              </div>
              <div>
                <div className="n lime">
                  {formatMoney(sumMinor(agents.map((a) => a.available)), singleCurrency, {
                    decimals: 0,
                  })}
                </div>
                <div className="l">Available after holds</div>
              </div>
            </div>
          ) : (
            <div className="inbox-top">
              Agents span {currencies.size} currencies — totals are shown per agent, because a sum
              across currencies without an FX conversion is not a number.
            </div>
          )}

          {agents.length === 0 ? (
            <Empty title="No budgets to show" />
          ) : (
            agents.map((budget) => (
              <StepperRow
                key={budget.id}
                budget={budget}
                currency={currencyFor(budget.entity_id)}
                canWrite={canWrite}
                pending={update.isPending}
                onChange={(agentId, next) => update.mutate({ agentId, next })}
              />
            ))
          )}

          {query.data.workflows.length > 0 ? (
            <Card title="Workflow rollups" className="full2" >
              {query.data.workflows.map((workflow) => (
                <div className="mini-row" key={workflow.id}>
                  <span className="mg" aria-hidden>
                    ⇶
                  </span>
                  <span className="mm">
                    {workflow.name}
                    <small>{workflow.agent_ids.length} agents</small>
                  </span>
                  <span className="mv">
                    {Math.round(Number(workflow.pct_used))}% of{' '}
                    {formatMoney(workflow.budget, singleCurrency ?? 'USD', { decimals: 0 })}
                  </span>
                </div>
              ))}
            </Card>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
