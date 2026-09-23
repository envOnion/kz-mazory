export interface MetricSummary {
  id: string
  title: string
  value: string
  trend: string
  trendPositive: boolean
  icon: 'bar-chart' | 'target' | 'users'
}

export interface ManagerKpi {
  id: string
  name: string
  role: string
  avatar: string
  isTopPerformer?: boolean
  statusColor: 'green' | 'yellow' | 'red'
  kpiPercent: number
  kpiBarColor: 'green' | 'yellow' | 'red'
  salesAmount: string
  dealsCount: number
  trend: string
  trendPositive: boolean
}

export interface QuickAction {
  id: string
  label: string
  icon: 'search' | 'file-text' | 'bar-chart-2' | 'send'
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
  summaryMetrics: MetricSummary[]
  managers: ManagerKpi[]
  insight: AiInsight
}

export type ViewMode = 'welcome' | 'dashboard'
