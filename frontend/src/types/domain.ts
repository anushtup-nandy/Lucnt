/**
 * Hand-kept views over the generated `api.ts`. `verdict_code` is an open string
 * on the wire, so its union is maintained by hand against verdict-codes.json.
 */

/** Mirrors `contracts/verdict-codes.json`. Keep in step with it, by hand. */
export type VerdictCode =
  | 'ok'
  | 'budget_exhausted'
  | 'merchant_blocked'
  | 'velocity_exceeded'
  | 'amount_over_threshold'
  | 'outside_hours'

/** Human wording for a machine code. Unknown codes render as themselves. */
export const VERDICT_REASONS: Record<VerdictCode, string> = {
  ok: 'Within policy',
  budget_exhausted: 'Budget exhausted',
  merchant_blocked: 'Merchant blocked',
  velocity_exceeded: 'Spending too fast',
  amount_over_threshold: 'Over the amount threshold',
  outside_hours: 'Outside allowed hours',
}

export function verdictReason(code: string | null): string | null {
  if (!code || code === 'ok') return null
  return VERDICT_REASONS[code as VerdictCode] ?? code.replace(/_/g, ' ')
}

export interface Paginated<T> {
  items: T[]
  total: number
}

export interface Transaction {
  id: string
  agent_id: string
  entity_id: string
  authorization_id: string | null
  source: string
  vendor: string
  item_description: string | null
  paid_amount: string
  paid_currency: string
  local_amount: string
  local_currency: string
  project: string | null
  intent: string | null
  model_used: string | null
  tokens_consumed: number | null
  estimated: boolean
  invoice_url: string | null
  status: string
  is_reversed: boolean
  reason_text: string | null
  failure_reason: string | null
  transaction_date: string
}

export interface JournalLine {
  account_name: string
  account_code: string
  debit: string
  credit: string
  ledger: string
  currency: string
}

export interface TaxInfo {
  tax_type: string
  jurisdiction: string
  rate: string
  tax_amount: string
  is_reclaimable: boolean
  reclaim_amount: string
  currency: string
  legal_reference: string
  reverse_charge: boolean
  self_invoice_generated: boolean
}

export interface TransactionFull extends Transaction {
  tax: TaxInfo | null
  journal: JournalLine[]
}

export interface Authorization {
  id: string
  agent_id: string
  agent_name: string
  agent_glyph: string
  entity_id: string
  vendor: string
  amount: string
  currency: string | null
  reason: string
  intent: string | null
  project: string | null
  status: string
  verdict_code: string | null
  reason_text: string | null
  created_at: string
}

export interface AgentBudget {
  id: string
  entity_id: string
  name: string
  slug: string
  monthly_budget: string
  spent: string
  spent_today: string
  remaining: string
  reserved: string
  available: string
  pct_used: string
  status: string
}

export interface WorkflowBudget {
  id: string
  name: string
  budget: string
  spent: string
  remaining: string
  pct_used: string
  status: string
  agent_ids: string[]
}

export interface BudgetsResponse {
  agents: AgentBudget[]
  workflows: WorkflowBudget[]
  total?: number
}

export interface Policy {
  id: string
  name: string
  rule_type: string
  config: Record<string, unknown>
  scope_type: string
  scope_id: string | null
  description: string | null
  active: boolean
  hit_count: number
  block_count: number
  created_at: string
  updated_at: string
}

export interface Approval {
  id: string
  agent_id: string
  entity_id: string
  vendor: string
  amount: string
  currency: string | null
  reason: string | null
  reason_text: string | null
  status: string
  created_at: string
}

export interface ReconException {
  id: string
  transaction_id: string
  exception_type: string
  status: string
  amount_at_stake: string
  currency: string | null
  evidence: Record<string, unknown>
  fix_notes: string | null
  resolved_at: string | null
  created_at: string
  updated_at: string
}

export interface ReconListResponse extends Paginated<ReconException> {
  total_at_stake: Record<string, string>
  open_count: number
}

export interface Insight {
  id: string
  agent_id: string | null
  insight_type: string
  title: string
  body: string
  value_text: string | null
  action_label: string | null
  action_payload: Record<string, unknown>
  is_applied: boolean
  applied_at: string | null
  is_dismissed: boolean
  dismissed_at: string | null
  created_at: string
}

export interface Memory {
  id: string
  agent_id: string
  memory_type: string
  summary: string
  detail_text: string
  metrics_json: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface ApiKeySummary {
  id: string
  name: string
  key_prefix: string
  mode: string
  scope: string
  is_active: boolean
  last_used_at: string | null
  created_at: string
}

export interface AuditLogEntry {
  id: string
  action: string
  entity_type: string
  entity_id: string | null
  actor_api_key_id: string | null
  actor_user_id: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  created_at: string
}

export interface TeamMember {
  id: string
  email: string
  name: string
  role: string
  is_active: boolean
}

/* --------------------------------------------------------------- Books */

export interface BookLine {
  account_code: string
  account_name: string
  amount: string
  currency: string
}

export interface TrialBalanceRow {
  account_code: string
  account_name: string
  category: string
  debit: string
  credit: string
  currency: string
}

export interface CurrencyTotals {
  currency: string
  total_debit: string
  total_credit: string
  balanced: boolean
}

export interface TrialBalanceResponse {
  rows: TrialBalanceRow[]
  currencies: CurrencyTotals[]
  balanced: boolean
}

export interface ProfitAndLossTotals {
  currency: string
  total_income: string
  total_expenses: string
  net_profit: string
}

export interface ProfitAndLossResponse {
  income: BookLine[]
  expenses: BookLine[]
  currencies: ProfitAndLossTotals[]
}

export interface BalanceSheetTotals {
  currency: string
  total_assets: string
  total_liabilities: string
  total_equity: string
  balanced: boolean
}

export interface BalanceSheetResponse {
  assets: BookLine[]
  liabilities: BookLine[]
  equity: BookLine[]
  currencies: BalanceSheetTotals[]
  balanced: boolean
}

export interface CashFlowTotals {
  currency: string
  net_cash_flow: string
}

export interface CashFlowResponse {
  operating: BookLine[]
  currencies: CashFlowTotals[]
}

export interface GLAccount {
  account_code: string
  account_name: string
  category: string
  currency: string
  total_debit: string
  total_credit: string
  balance: string
  entry_count?: number
  entries?: JournalLine[]
}

export interface GeneralLedgerResponse {
  accounts: GLAccount[]
}

export interface JournalGroup {
  transaction_id: string
  date: string
  vendor: string
  currency: string
  total_debit: string
  total_credit: string
  lines: JournalLine[]
}

export interface JournalsResponse extends Paginated<JournalGroup> {}
