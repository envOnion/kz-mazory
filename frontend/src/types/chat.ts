export interface MetricSummary {
  id: string
  title: string
  value: string
  trend: string
  trendPositive: boolean
  icon: 'bar-chart' | 'target' | 'users'
}

export interface ManagerProjectSummary {
  id: number
  name: string
  company: string
  contract_amount: number
  contract_formatted: string
  paid_amount: number
  paid_formatted: string
  due_amount: number
  due_formatted: string
  status: string
  status_code: string
  margin: number
  equipment: string
  is_verified?: boolean
}

export interface ManagerKpi {
  id: string
  dbId?: number
  name: string
  role: string
  avatar: string
  isTopPerformer?: boolean
  statusColor: 'green' | 'yellow' | 'red'
  kpiPercent: number
  kpiBarColor: 'green' | 'yellow' | 'red'
  salesAmount: string
  targetAmount?: number
  targetFormatted?: string
  dealsCount: number
  averageMargin?: number
  overdueCommitments?: number
  trend: string
  trendPositive: boolean
  projects?: ManagerProjectSummary[]
}

export interface QuickAction {
  id: string
  label: string
  icon: 'search' | 'file-text' | 'bar-chart-2' | 'send' | 'clock'
}

export interface AiInsight {
  badge: string
  source: string
  headline: string
  details: string
  actions: QuickAction[]
}

export interface KpiDashboardData {
  categoryBadge: string
  queryTitle: string
  querySubtitle: string
  updatedAtText: string
  period?: string
  periodCode?: string
  periodLabel?: string
  summaryMetrics: MetricSummary[]
  managers: ManagerKpi[]
  chartData?: any
  insight: AiInsight
}

export type WidgetType = 'chart' | 'commitments_list' | 'project_table' | 'kpi_grid'

export interface ChatWidget {
  type: WidgetType
  preset?: string
  title?: string
  data: any
}

export interface ChatResponse {
  prompt: string
  text: string
  widget?: ChatWidget
  insights?: string[]
  quotes?: string[]
}

export type ViewMode = 'welcome' | 'dashboard' | 'profile'
