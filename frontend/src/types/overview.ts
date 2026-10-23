export interface OverviewMetric { key: string; label: string; value: number | null; description: string; tone?: 'normal' | 'warning' | 'danger' }
export interface OverviewSource { id: number; name: string; status: 'fresh' | 'stale' | 'unknown' | 'disconnected'; checked_at: string | null; covered_through: string | null; last_message_at: string | null; description: string }
export interface OverviewIssue { key: string; label: string; count: number; description: string; severity: 'warning' | 'danger' }
export interface OverviewSignal { key: string; title: string; description: string; metric: string; severity: 'info' | 'warning' | 'danger' }
export interface OverviewOutcome { key: string; label: string; description: string; metrics: OverviewMetric[] }
export interface OverviewItem { id: number; label: string; description: string; at: string | null; state: string; href: string | null; source_href: string | null; crm_href: string | null }
export interface AnalyticsOverview {
  as_of: string; timezone: string; from: string | null; to: string; coverage: string
  filters: { teams: { id: number; name: string }[]; chats: { id: number; name: string; team_id: number | null }[] }
  status: { code: 'empty' | 'unknown' | 'processing' | 'attention' | 'complete'; title: string; description: string; processed_through: string | null; technical_issues: number; human_questions: number }
  sources: OverviewSource[]; messages: OverviewMetric[]; dialogues: OverviewMetric[]; outcomes: OverviewOutcome[]
  attention: OverviewIssue[]; signals: OverviewSignal[]; comparison: string
  performance: { oldest_pending_at: string | null; median_delivery_seconds: number | null; automatic_percent: number | null; sample_size: number }
  activity: OverviewItem[]; limitations: string[]
}
export interface OverviewDetails { metric: string; title: string; count: number; items: OverviewItem[]; page: number; next_page: number | null; as_of: string }
