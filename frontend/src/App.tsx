/**
 * Routes — 15 of them (12 nav + 2 detail + login), per `frontend/PLAN.md` §6.
 *
 * Each nav route is wrapped in its `RoleGate`, which reads the *same* `nav` key
 * the server's `require_nav` gates on. Detail routes inherit their parent's
 * key: a transaction detail is part of the Transactions domain.
 */

import { useEffect, type ReactNode } from 'react'
import { Route, Routes } from 'react-router'

import { bootstrapSession } from '@/api/queries/auth'
import { AppShell } from '@/components/layout/AppShell'
import { LandingRedirect, ProtectedRoute, RoleGate } from '@/components/layout/guards'
import { AgentProfile } from '@/pages/AgentProfile'
import { Agents } from '@/pages/Agents'
import { Approvals } from '@/pages/Approvals'
import { Books } from '@/pages/Books'
import { Budgets } from '@/pages/Budgets'
import { Developers } from '@/pages/Developers'
import { Insights } from '@/pages/Insights'
import { Login } from '@/pages/Login'
import { Memory } from '@/pages/Memory'
import { Overview } from '@/pages/Overview'
import { Policies } from '@/pages/Policies'
import { Recon } from '@/pages/Recon'
import { Settings } from '@/pages/Settings'
import { TransactionDetail } from '@/pages/TransactionDetail'
import { Transactions } from '@/pages/Transactions'

function gated(navKey: string, element: ReactNode): ReactNode {
  return <RoleGate navKey={navKey}>{element}</RoleGate>
}

function NotFound(): ReactNode {
  return (
    <div className="page">
      <div className="state">
        <div className="st-t serif">No such page</div>
        <div>That route does not exist in this build.</div>
      </div>
    </div>
  )
}

export default function App(): ReactNode {
  // One silent refresh on cold load: the access token is memory-only, so a
  // browser refresh starts with nothing but the httpOnly cookie.
  useEffect(() => {
    void bootstrapSession()
  }, [])

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <ProtectedRoute>
            <AppShell />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<LandingRedirect />} />
        <Route path="/overview" element={gated('overview', <Overview />)} />
        <Route path="/agents" element={gated('agents', <Agents />)} />
        <Route path="/agents/:agentId" element={gated('agents', <AgentProfile />)} />
        <Route path="/transactions" element={gated('transactions', <Transactions />)} />
        <Route
          path="/transactions/:transactionId"
          element={gated('transactions', <TransactionDetail />)}
        />
        <Route path="/books" element={gated('books', <Books />)} />
        <Route path="/budgets" element={gated('budgets', <Budgets />)} />
        <Route path="/policies" element={gated('policies', <Policies />)} />
        <Route path="/approvals" element={gated('approvals', <Approvals />)} />
        <Route path="/recon" element={gated('recon', <Recon />)} />
        <Route path="/insights" element={gated('insights', <Insights />)} />
        <Route path="/memory" element={gated('memory', <Memory />)} />
        <Route path="/developers" element={gated('developers', <Developers />)} />
        <Route path="/settings" element={gated('settings', <Settings />)} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
