/** Settings — legal entities, the team, and what this workspace actually is. */

import type { ReactNode } from 'react'

import { api, describeFailure } from '@/api/client'
import { scope, useScope } from '@/api/queries/keys'
import { Card, Empty, Failure, Loading, PageHeader } from '@/components/atoms'
import { useStaticQuery } from '@/hooks/useLiveQuery'
import { useAuthStore } from '@/store/authStore'
import { entityFlag, useEntityStore } from '@/store/entityStore'
import type { Paginated, TeamMember } from '@/types/domain'

export function Settings(): ReactNode {
  const { userId, entity } = useScope()
  const session = useAuthStore((s) => s.session)
  const entities = useEntityStore((s) => s.entities)

  const team = useStaticQuery(scope('users', userId, entity), async () => {
    const { data } = await api.get<Paginated<TeamMember>>('/users', { params: { limit: 200 } })
    return data
  })

  return (
    <div className="page">
      <PageHeader
        title="Settings"
        sub={session ? `${session.companyName} · ${entities.length} legal entities` : undefined}
      />

      <Card title="Legal entities">
        {entities.length === 0 ? (
          <Empty title="No entities configured" />
        ) : (
          entities.map((row) => (
            <div className="set-ent" key={row.id}>
              <span className="fl" aria-hidden>
                {entityFlag(row.code)}
              </span>
              <span className="m">
                <span className="nm">{row.full_name}</span>
                <span className="mt">
                  {row.currency} · {row.timezone}
                </span>
              </span>
              <span className="badges">
                <span className="chipv mono">{row.code.toUpperCase()}</span>
                <span className="chipv">{row.accounting_std}</span>
                <span className="chipv">{row.tax_regime}</span>
                <span className="chipv">{row.erp ?? 'no ERP'}</span>
              </span>
            </div>
          ))
        )}
      </Card>

      <div style={{ height: 18 }} />

      <Card title="Team">
        {team.isPending ? <Loading rows={3} /> : null}
        {team.isError ? <Failure failure={describeFailure(team.error)} /> : null}
        {team.data?.items.map((member) => (
          <div className="key" key={member.id}>
            <span className="kn">{member.name}</span>
            <span className="kv">{member.email}</span>
            <span className="kt">
              {member.role.replace('_', ' ')}
              {member.is_active ? '' : ' · deactivated'}
            </span>
          </div>
        ))}
      </Card>

      <div style={{ height: 18 }} />

      <Card title="Workspace">
        <div className="kv">
          <span className="k">Your role</span>
          <span className="v">{session?.perms.role.replace('_', ' ')}</span>
        </div>
        <div className="kv">
          <span className="k">Agent scope</span>
          <span className="v">
            {session?.perms.agent_scope === 'owned' ? 'Only agents you own' : 'All agents'}
          </span>
        </div>
        <div className="kv">
          <span className="k">Landing page</span>
          <span className="v mono">{session?.perms.landing}</span>
        </div>
      </Card>
    </div>
  )
}
