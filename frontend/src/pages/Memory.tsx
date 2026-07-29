/** Memory — what the company learned, per agent. Serif, because it reads as prose. */

import type { ReactNode } from 'react'

import { api, describeFailure } from '@/api/client'
import { entityParam, scope, useScope } from '@/api/queries/keys'
import { Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery } from '@/hooks/useLiveQuery'
import type { Memory as MemoryRow, Paginated } from '@/types/domain'

const MEMORY_ICONS: Record<string, string> = {
  vendor_pattern: '⊞',
  spend_rhythm: '◷',
  price_baseline: '⌁',
  tax_pattern: '§',
}

/** Render `metrics_json` as the chips under a memory, values in mono. */
function metricChips(metrics: Record<string, unknown>): ReactNode {
  const entries = Object.entries(metrics ?? {})
  if (entries.length === 0) return null
  return (
    <div className="mm">
      {entries.slice(0, 4).map(([k, v]) => (
        <span key={k}>
          {k.replace(/_/g, ' ')} <b>{String(v)}</b>
        </span>
      ))}
    </div>
  )
}

export function Memory(): ReactNode {
  const { userId, entity } = useScope()
  const query = useLiveQuery(scope('memory', userId, entity), async () => {
    const { data } = await api.get<Paginated<MemoryRow>>('/memory', {
      params: { ...entityParam(entity), limit: 100 },
    })
    return data
  })

  return (
    <div className="page">
      <PageHeader
        title="Memory"
        sub="Patterns the system learned by watching your agents spend."
      />

      {query.isPending ? <Loading /> : null}
      {query.isError ? (
        <Failure failure={describeFailure(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.data && query.data.items.length === 0 ? (
        <Empty
          title="Nothing learned yet"
          hint="Memories are recomputed from the ledger; they need a few weeks of spend."
        />
      ) : null}

      {query.data?.items.map((memory) => (
        <div className="mem" key={memory.id}>
          <span className="mi" aria-hidden>
            {MEMORY_ICONS[memory.memory_type] ?? '❧'}
          </span>
          <div className="mc">
            <div className="mt serif">{memory.summary}</div>
            <div className="md">{memory.detail_text}</div>
            {metricChips(memory.metrics_json)}
          </div>
        </div>
      ))}
    </div>
  )
}
