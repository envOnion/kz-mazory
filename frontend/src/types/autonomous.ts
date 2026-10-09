export interface AutomaticDecision {
  outcome: 'accepted' | 'deferred' | 'rejected' | 'superseded'
  reason_code: string
  explanation: string
  policy_version: string
}
export interface FinancialTotal {
  currency: string
  direction: 'income' | 'expense'
  amount_precision: 'exact' | 'approximate' | 'range' | 'unknown'
  amount: string
  count: number
}
export interface ReportPayload {
  totals: FinancialTotal[]
  projects: number
  contract_known: number
  open_commitments: number
  missing_deadline: number
  cutoff: string
}
export type CrmDisabledReason = 'integration_disabled' | 'webhook_missing' | 'autonomous_crm_disabled'
export interface CrmStatus {
  integration_enabled: boolean
  webhook_configured: boolean
  autonomous_write_allowed: boolean
  effective_autonomous_write_enabled: boolean
  disabled_reason: CrmDisabledReason | null
}
export interface AutonomousOverview {
  enabled: boolean
  crm_enabled: boolean
  crm_status: CrmStatus
  policy_version: string
  source: string
  as_of: string
  totals: FinancialTotal[]
  series: (Omit<FinancialTotal, 'count'> & { payment_date: string })[]
  decisions: { outcome: string; count: number }[]
  waiting: (Omit<AutomaticDecision, 'policy_version'> & { candidate_id: number; project_id: number | null })[]
  coverage: { source_scope: string; complete_through: string | null; gaps: { raw_message_id: number; state: string }[]; counts: Record<string, number>; updated_at: string }[]
  reports: { id: number; created_at: string; payload: ReportPayload }[]
  observations: { id: number; project_id: number | null; project__name: string | null; event_type: string; occurred_at: string | null; payload: { amount?: string; currency?: string; evidence?: string; payment_date?: string | null; amount_precision?: string } }[]
  crm_deliveries: { id: number; event_type: string; state: string; error_code: string; next_attempt_at: string }[]
  schedule: { id: number; project_id: number; project__name: string; due_date: string; amount: string; currency: string; direction: string; amount_precision: string }[]
}
