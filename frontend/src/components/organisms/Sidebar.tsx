/** The sidebar: brand, entity switcher, role-driven nav, signed-in user. */

import clsx from 'clsx'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router'

import { logout } from '@/api/queries/auth'
import { groupNav, initials, navIcon } from '@/lib/nav'
import { useAuthStore } from '@/store/authStore'
import { entityFlag, useEntityStore } from '@/store/entityStore'
import { useUiStore } from '@/store/uiStore'

function EntitySwitcher(): ReactNode {
  const { current, entities, setCurrent } = useEntityStore()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent): void => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const selected = entities.find((e) => e.code === current)
  return (
    <div className="ws" ref={ref}>
      <button
        type="button"
        className="ws-btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="fl">{selected ? entityFlag(selected.code) : '◫'}</span>
        <span className="nm">{selected ? selected.name : 'All entities'}</span>
        <span className="cv">▾</span>
      </button>
      {open ? (
        <div className="ws-menu" role="listbox">
          <button
            type="button"
            role="option"
            aria-selected={current === null}
            className={clsx('ws-opt', current === null && 'on')}
            onClick={() => {
              setCurrent(null)
              setOpen(false)
            }}
          >
            <span className="fl">◫</span>
            <span>
              <span className="nm">All entities</span>
              <span className="mt">Consolidated</span>
            </span>
            <span className="ck">✓</span>
          </button>
          {entities.map((entity) => (
            <button
              key={entity.id}
              type="button"
              role="option"
              aria-selected={entity.code === current}
              className={clsx('ws-opt', entity.code === current && 'on')}
              onClick={() => {
                setCurrent(entity.code)
                setOpen(false)
              }}
            >
              <span className="fl">{entityFlag(entity.code)}</span>
              <span>
                <span className="nm">{entity.name}</span>
                <span className="mt">
                  {entity.currency} · {entity.tax_regime}
                </span>
              </span>
              <span className="ck">✓</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export function Sidebar(): ReactNode {
  const session = useAuthStore((s) => s.session)
  const { sidebarOpen, setSidebarOpen } = useUiStore()
  const navigate = useNavigate()
  if (!session) return null

  const sections = groupNav(session.perms.nav)

  return (
    <aside className={clsx('side', sidebarOpen && 'open')}>
      <div className="brand">
        <span className="mark" aria-hidden />
        <span className="wm">LUCNT</span>
      </div>

      <EntitySwitcher />

      <nav className="nav" aria-label="Main">
        {sections.map((section) => (
          <div key={section.label}>
            <div className="nav-sec">{section.label}</div>
            {section.items.map((item) => (
              <NavLink
                key={item.key}
                to={item.path}
                className={({ isActive }) => clsx('nav-i', isActive && 'on')}
                onClick={() => setSidebarOpen(false)}
              >
                <span className="ic" aria-hidden>
                  {navIcon(item.key)}
                </span>
                {item.label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="side-foot">
        <span className="av" aria-hidden>
          {initials(session.user.name)}
        </span>
        <span className="who" style={{ flex: 1, overflow: 'hidden' }}>
          {session.user.name}
          <small>{session.perms.role.replace('_', ' ')}</small>
        </span>
        <button
          type="button"
          className="btn sm"
          onClick={() => {
            void logout().then(() => navigate('/login', { replace: true }))
          }}
        >
          Exit
        </button>
      </div>
    </aside>
  )
}
