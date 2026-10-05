import type { PresentationDocument } from './presentation'
import type { Money, Coverage, Payment, Receivables } from './platform'
export interface MetricSummary { id: string; title: string; value: string; trend: string; trendPositive: boolean; icon: 'bar-chart' | 'target' | 'users' }
export interface ManagerProjectSummary {
  id: number; name: string; company: string; manager: string; contract_amount: Money; contract_formatted: string;
  paid_amount: Money; paid_formatted: string; due_amount: Money; due_formatted: string; overpayment: Money;
  status: string; status_code: string; margin_percent: number | null; equipment: string; is_verified: boolean;
  margin_alert: boolean; priority: string; version: number; currency: string; current_action: string; next_action: string
}
export interface ProjectWorkspaceSummary extends ManagerProjectSummary {
  contract_known: boolean; payments_known: boolean; balance_known: boolean;
  approximate_paid_formatted?: string | null;
  data_completeness: 'complete' | 'partial' | 'missing';
  missing_data_reasons: { code: string; message: string }[]; review_available: boolean
}
export interface ManagerKpi { id: number; name: string; role: string; avatar: string; isTopPerformer?: boolean;
  statusColor: 'green' | 'yellow' | 'red'; kpiPercent: number | null; kpiBarColor: 'green' | 'yellow' | 'red';
  salesAmount: string; fact: Money; targetAmount: Money | null; targetFormatted: string; dealsCount: number;
  trend: string; trendPositive: boolean; projects: ManagerProjectSummary[] }
export interface QuickAction { id: string; label: string; icon: 'search' | 'file-text' | 'bar-chart-2' | 'send' | 'clock' }
export interface AiInsight { badge: string; source: string; headline: string; details: string; actions: QuickAction[] }
export interface ChartDataset { label: string; data: (Money | null)[]; backgroundColor?: string | string[] }
export interface ChartPayload { chart_type?: string; title: string; unit: string; labels: string[]; datasets: ChartDataset[] }
export interface KpiDashboardData { categoryBadge: string; queryTitle: string; querySubtitle: string; updatedAtText: string;
  period: string; periodCode: string; periodLabel: string; summaryMetrics: MetricSummary[]; managers: ManagerKpi[];
  chartData: ChartPayload; timeline: ChartPayload; insight: AiInsight; coverage: Coverage; currency: string;
  source_rows: Payment[]; source_count: number; source_path: string; receivables: Receivables; definition: string; forecast: { available: boolean; reason: string; amount?: Money; currency?: string; as_of?: string; method?: string } }
export interface CommitmentItem { id: number; text: string; project_id: number | null; project_name: string; manager_name: string;
  deadline: string | null; deadline_formatted: string; status: string; status_code: string; status_color: 'red' | 'green' | 'yellow'; severity: string; version: number; postponed_reason: string }
export interface CommitmentData { total_count: number; fulfilled_count: number; pending_count: number; overdue_count: number;
  slippage_rate_percent: number | null; original_sla_percent: number | null; commitments: CommitmentItem[] }
export interface PipelineData { conversion: { value: number | null; reason: string; cohort_size: number; completed: number }; stage_duration_days: { stage: string; mean_days: number; observations: number }[]; stages: { code: string; label: string; count: number; volume: Money; volume_formatted: string }[];
  margin_distribution: { low_under_15: number; norm_15_to_20: number; high_over_20: number; unknown: number }; projects: ManagerProjectSummary[]; weighted_margin: number | null }
export type ChatWidget = { type: 'chart'; data: ChartPayload } | { type: 'commitments_list'; data: CommitmentData } | { type: 'project_table'; data: PipelineData } | { type: 'kpi_grid'; data: KpiDashboardData }
export type WidgetType = ChatWidget['type']
export interface ChatQuote { id: number; content: string; sender_name: string; sent_at: string; source_url: string }
export interface ChatResponse { presentation?: PresentationDocument | null; prompt: string; text: string; widget: ChatWidget | null; insights: string[]; quotes: ChatQuote[] }
export type ViewMode = 'welcome' | 'dashboard' | 'profile' | 'workspace'
