/** The nav must render from `perms.nav`, never a hardcoded list. */

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'

import { Sidebar } from './Sidebar'
import { useAuthStore, type NavItem, type Session } from '@/store/authStore'
import { useEntityStore } from '@/store/entityStore'

function session(nav: NavItem[], role = 'cfo'): Session {
  return {
    user: { id: 'u1', email: 'a@b.c', name: 'Arvind Sharma', role, is_active: true },
    companyId: 'c1',
    companyName: 'Acme',
    perms: {
      role,
      nav,
      landing: nav[0]?.path ?? '/overview',
      agent_scope: 'all',
      can_write_budgets: true,
      can_write_policies: true,
      can_approve: true,
      can_read_books: true,
    },
  }
}

function mount(nav: NavItem[], role?: string): void {
  useAuthStore.getState().setSession(session(nav, role))
  useEntityStore.getState().setEntities([])
  render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>,
  )
}

afterEach(() => {
  useAuthStore.getState().clear()
})

describe('Sidebar', () => {
  it('renders exactly the items the server sent, and nothing else', () => {
    mount([
      { key: 'agents', label: 'Agents', path: '/agents' },
      { key: 'budgets', label: 'Budgets', path: '/budgets' },
    ])

    expect(screen.getByRole('link', { name: /Agents/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Budgets/ })).toBeInTheDocument()
    // Known domain, but not granted by the server.
    expect(screen.queryByRole('link', { name: /Books/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Developers/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(2)
  })

  it('renders a nav item this build has never heard of', () => {
    // No hardcoded array could contain this key.
    mount([{ key: 'treasury', label: 'Treasury', path: '/treasury' }])
    const link = screen.getByRole('link', { name: /Treasury/ })
    expect(link).toHaveAttribute('href', '/treasury')
  })

  it('uses the server-supplied path, not one derived from the key', () => {
    mount([{ key: 'agents', label: 'Agents', path: '/some/other/place' }])
    expect(screen.getByRole('link', { name: /Agents/ })).toHaveAttribute(
      'href',
      '/some/other/place',
    )
  })

  it('shows the signed-in user and their role', () => {
    mount([{ key: 'agents', label: 'Agents', path: '/agents' }], 'agent_owner')
    expect(screen.getByText('Arvind Sharma')).toBeInTheDocument()
    expect(screen.getByText('agent owner')).toBeInTheDocument()
    // Initials, not an avatar service (no third-party requests).
    expect(screen.getByText('AS')).toBeInTheDocument()
  })

  it('renders nothing at all without a session', () => {
    useAuthStore.getState().clear()
    const { container } = render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
