/**
 * Developers — the SDK snippet, the keys, the event log, and one honest gap.
 *
 * **The snippet matches the real SDK surface.** The mockup shows
 * `from lucnt import Agent`; there is no `Agent` class and there deliberately
 * never will be (the SDK *attaches* to the customer's agents,
 * it does not own the noun). What is shown here is `lucnt.task()` +
 * `authorize()` / `capture()`, which is what a customer actually writes.
 *
 * **Webhooks are a labelled Phase 6 empty state**, not fake rows. Rendering
 * plausible-looking deliveries for a feature that does not exist is exactly the
 * kind of façade this dashboard does not build.
 */

import { useState, type ReactNode } from 'react'

import { api, describeFailure } from '@/api/client'
import { scope, useScope } from '@/api/queries/keys'
import { Card, Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useLiveQuery, useStaticQuery } from '@/hooks/useLiveQuery'
import { useUiStore } from '@/store/uiStore'
import type { ApiKeySummary, AuditLogEntry, Paginated } from '@/types/domain'

const SNIPPET = `import lucnt

lucnt.configure(api_key="lucnt_live_...")

# Identity and intent, set once — your agent, not ours.
with lucnt.task(agent="research", entity="in", intent="market scan"):

    # Blocks BEFORE money moves.
    verdict = lucnt.authorize(vendor="openai", amount="12.40", currency="USD")
    if verdict.status != "approved":
        raise SystemExit(verdict.reason)

    response = openai_client.responses.create(...)   # your real purchase

    # Commits the spend: ledger + tax + double-entry journal.
    tx = lucnt.capture(verdict, amount="12.40")
    print(tx.journal, tx.tax)`

function CodeBlock(): ReactNode {
  const pushToast = useUiStore((s) => s.pushToast)
  return (
    <pre className="code">
      <button
        type="button"
        className="cp"
        onClick={() => {
          void navigator.clipboard?.writeText(SNIPPET).then(() => pushToast('Copied'))
        }}
      >
        copy
      </button>
      <code>{SNIPPET}</code>
    </pre>
  )
}

export function Developers(): ReactNode {
  const { userId, entity } = useScope()
  const [logFilter, setLogFilter] = useState('')

  const keys = useStaticQuery(scope('api-keys', userId, entity), async () => {
    const { data } = await api.get<Paginated<ApiKeySummary>>('/api-keys', {
      params: { limit: 100 },
    })
    return data
  })

  const logs = useLiveQuery(scope('audit-logs', userId, entity, logFilter), async () => {
    const { data } = await api.get<Paginated<AuditLogEntry>>('/audit-logs', {
      params: { limit: 50, ...(logFilter ? { action: logFilter } : {}) },
    })
    return data
  })

  return (
    <div className="page wide">
      <PageHeader
        title="Developers"
        sub="Attach LUCNT to the agents you already have. No framework to adopt."
      />

      <CodeBlock />

      <div className="dev-grid" style={{ marginTop: 18 }}>
        <Card title="API keys">
          {keys.isPending ? <Loading rows={2} /> : null}
          {keys.isError ? <Failure failure={describeFailure(keys.error)} /> : null}
          {keys.data?.items.length === 0 ? <Empty title="No keys issued" /> : null}
          {keys.data?.items.map((key) => (
            <div className="key" key={key.id}>
              <span className="kn">{key.name}</span>
              {/* Only a prefix is stored — the secret is unrecoverable by design. */}
              <span className="kv">{key.key_prefix}…</span>
              <span className="kt">
                {key.mode} · {key.scope}
                {key.is_active ? '' : ' · revoked'}
              </span>
            </div>
          ))}
        </Card>

        <Card title="Webhooks">
          <div className="state" style={{ padding: '18px 0' }}>
            <div className="st-t serif">Phase 6</div>
            <div>
              Outbound webhooks are not built yet. Events are emitted internally and visible in
              the log below.
            </div>
          </div>
        </Card>
      </div>

      <Card title="Event log" className="full2" action={
        <input
          aria-label="Filter by action"
          placeholder="filter action…"
          value={logFilter}
          onChange={(event) => setLogFilter(event.target.value)}
          style={{
            border: '1px solid var(--faint)',
            borderRadius: 8,
            padding: '4px 9px',
            fontSize: 12,
            textTransform: 'none',
            letterSpacing: 0,
          }}
        />
      }>
        {logs.isPending ? <Loading rows={4} /> : null}
        {logs.isError ? <Failure failure={describeFailure(logs.error)} /> : null}
        {logs.data?.items.length === 0 ? (
          <Empty title="No governance events yet" hint="Budget, policy and approval changes land here." />
        ) : null}
        {logs.data?.items.map((entry) => (
          <div className="log" key={entry.id}>
            <span className="dot-ok" aria-hidden />
            <span className="lt">{new Date(entry.created_at).toLocaleTimeString()}</span>
            <span className="lm">{entry.entity_type}</span>
            <span className="lp">{entry.action}</span>
            <span className="ls">
              {entry.actor_user_id ? 'human' : entry.actor_api_key_id ? 'api key' : 'system'}
            </span>
          </div>
        ))}
      </Card>
    </div>
  )
}
