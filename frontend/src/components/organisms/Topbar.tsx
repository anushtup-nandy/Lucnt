/**
 * The topbar: breadcrumb, entity badge, and the `/` search palette.
 *
 * Search is client-side over the agent list, deliberately: there is no search
 * endpoint, and inventing one for the demo would be a façade.
 * What it does do — jump to an agent by name — is the thing
 * the mockup's palette is actually used for.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { api } from '@/api/client'
import { useStaticQuery } from '@/hooks/useLiveQuery'
import { useAuthStore } from '@/store/authStore'
import { useEntityStore } from '@/store/entityStore'
import { useUiStore } from '@/store/uiStore'

interface AgentRow {
  id: string
  name: string
  slug: string
  glyph: string
  role: string
}

/** `/transactions/abc` → `Transactions`, using the role's own nav labels. */
function useCrumb(): string {
  const nav = useAuthStore((s) => s.session?.perms.nav ?? [])
  const { pathname } = useLocation()
  const root = `/${pathname.split('/')[1] ?? ''}`
  return nav.find((item) => item.path === root)?.label ?? 'LUCNT'
}

export function Topbar(): ReactNode {
  const crumb = useCrumb()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const setSidebarOpen = useUiStore((s) => s.setSidebarOpen)
  const userId = useAuthStore((s) => s.session?.user.id ?? 'anonymous')
  const current = useEntityStore((s) => s.current)
  const entities = useEntityStore((s) => s.entities)
  const canSeeAgents = useAuthStore((s) =>
    (s.session?.perms.nav ?? []).some((i) => i.key === 'agents'),
  )

  const agents = useStaticQuery(
    ['agents-search', userId],
    async () => {
      const { data } = await api.get<{ items: AgentRow[] }>('/agents', {
        params: { limit: 200 },
      })
      return data.items
    },
    { enabled: canSeeAgents },
  )

  // `/` focuses search, `Esc` closes it (§8).
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null
      const typing = target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA'
      if (event.key === '/' && !typing) {
        event.preventDefault()
        inputRef.current?.focus()
      }
      if (event.key === 'Escape') {
        setOpen(false)
        inputRef.current?.blur()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const needle = query.trim().toLowerCase()
  const matches = needle
    ? (agents.data ?? [])
        .filter(
          (a) =>
            a.name.toLowerCase().includes(needle) ||
            a.slug.includes(needle) ||
            a.role.toLowerCase().includes(needle),
        )
        .slice(0, 6)
    : []

  const entityLabel = current
    ? (entities.find((e) => e.code === current)?.name ?? current.toUpperCase())
    : 'All entities'

  return (
    <header className="topbar">
      <button
        type="button"
        className="btn sm md:hidden"
        onClick={() => setSidebarOpen(true)}
        aria-label="Open navigation"
      >
        ☰
      </button>
      <div className="crumb">
        <b>{crumb}</b>
        <span>·</span>
        <span>{entityLabel}</span>
      </div>

      <div className="tb-search">
        <div className="in">
          <span className="mut" aria-hidden>
            ⌕
          </span>
          <input
            ref={inputRef}
            value={query}
            aria-label="Search agents"
            placeholder="Search agents…"
            onChange={(event) => {
              setQuery(event.target.value)
              setOpen(true)
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          />
          <span className="k">/</span>
        </div>
        {open && needle ? (
          <div className="tb-res">
            <div className="tb-grp">Agents</div>
            {matches.length === 0 ? (
              <div className="tb-item" style={{ cursor: 'default' }}>
                <span className="t mut">No match</span>
              </div>
            ) : (
              matches.map((agent) => (
                <button
                  key={agent.id}
                  type="button"
                  className="tb-item"
                  onMouseDown={() => {
                    setQuery('')
                    navigate(`/agents/${agent.id}`)
                  }}
                >
                  <span className="g" aria-hidden>
                    {agent.glyph}
                  </span>
                  <span className="t">
                    {agent.name}
                    <small>{agent.role}</small>
                  </span>
                </button>
              ))
            )}
          </div>
        ) : null}
      </div>
    </header>
  )
}
