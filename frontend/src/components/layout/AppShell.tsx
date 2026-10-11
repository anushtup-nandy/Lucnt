/** The authenticated shell: sidebar + topbar + routed content. Fetches entities. */

import { useEffect, type ReactNode } from 'react'
import { Outlet, useLocation } from 'react-router'

import { api } from '@/api/client'
import { useStaticQuery } from '@/hooks/useLiveQuery'
import { Sidebar } from '@/components/organisms/Sidebar'
import { Topbar } from '@/components/organisms/Topbar'
import { useAuthStore } from '@/store/authStore'
import { useEntityStore, type EntitySummary } from '@/store/entityStore'
import { useUiStore } from '@/store/uiStore'

function Toasts(): ReactNode {
  const toasts = useUiStore((s) => s.toasts)
  if (toasts.length === 0) return null
  // Only the newest toast is shown.
  const toast = toasts[toasts.length - 1]
  if (!toast) return null
  return (
    <div className={`toast ${toast.tone === 'bad' ? 'bad' : ''}`} role="status">
      <span className="ti">{toast.tone === 'bad' ? '!' : '✓'}</span>
      {toast.message}
    </div>
  )
}

export function AppShell(): ReactNode {
  const userId = useAuthStore((s) => s.session?.user.id ?? 'anonymous')
  const setEntities = useEntityStore((s) => s.setEntities)
  const setSidebarOpen = useUiStore((s) => s.setSidebarOpen)
  const location = useLocation()

  const entities = useStaticQuery(['entities', userId], async () => {
    const { data } = await api.get<{ items: EntitySummary[] }>('/entities')
    return data.items
  })

  useEffect(() => {
    if (entities.data) setEntities(entities.data)
  }, [entities.data, setEntities])

  // Move focus to main on route change for keyboard users.
  useEffect(() => {
    setSidebarOpen(false)
    document.getElementById('main-content')?.focus()
  }, [location.pathname, setSidebarOpen])

  return (
    <div className="app">
      <Sidebar />
      <div className="main">
        <Topbar />
        <div className="content" id="main-content" tabIndex={-1}>
          <Outlet />
        </div>
      </div>
      <Toasts />
    </div>
  )
}
