import { ref, onMounted } from 'vue'
import type { ViewMode, KpiDashboardData, ChatWidget } from '../types/chat'
import { useAuth } from './useAuth'

const API_BASE = import.meta.env?.VITE_API_URL || '/api'

export function useChat() {
  const currentView = ref<ViewMode>('welcome')
  const isGenerating = ref(false)
  const activeWidget = ref<ChatWidget | null>(null)
  const chatResponseText = ref<string>('')

  const { isAuthModalOpen, logout } = useAuth()

  // Welcome state suggestions
  const welcomeSuggestions = ref([
    'Покажи график продаж и выполнения плана ↗',
    'Покажи KPI команды ↗',
    'Какие обещания и дедлайны горят? ↗',
    'Покажи воронку проектов и контроль маржи ↗'
  ])

  // Active dashboard suggestions
  const dashboardSuggestions = ref([
    'Выведи график продаж ↗',
    'Какие обещания просрочены? ↗',
    'Покажи воронку проектов ↗',
    'Что с объектом Top Build 343? ↗'
  ])

  // KPI Data from Data Mart
  const kpiData = ref<KpiDashboardData>({
    categoryBadge: 'AQUA KIP DATA MART',
    queryTitle: 'KPI отдела продаж',
    querySubtitle: 'Актуальные показатели коммерческой команды (тенге ₸)',
    updatedAtText: 'Загрузка...',
    summaryMetrics: [],
    managers: [],
    insight: {
      badge: 'AI-инсайт',
      source: 'Витрина данных Aqua Kip (сбор оплат, маржа, SLA дедлайнов)',
      headline: 'Загрузка актуальных данных Data Mart...',
      details: '',
      actions: [
        { id: 'why', label: 'Почему?', icon: 'search' },
        { id: 'deals', label: 'Показать сделки', icon: 'file-text' },
        { id: 'chart', label: 'Вывести график продаж', icon: 'bar-chart-2' },
        { id: 'commitments', label: 'Обещания и дедлайны', icon: 'clock' }
      ]
    }
  })

  const selectedPeriod = ref<'this_month' | 'last_month' | 'quarter' | 'year'>('this_month')

  async function fetchKpiData(period?: string) {
    if (period) {
      selectedPeriod.value = period as any
    }
    try {
      const token = localStorage.getItem('mazory_access_token')
      const headers: Record<string, string> = {}
      if (token) {
        headers['Authorization'] = `Bearer ${token}`
      }
      const periodParam = period || selectedPeriod.value
      const res = await fetch(`${API_BASE}/kpi/summary/?period=${periodParam}`, { headers })
      if (res.ok) {
        const data = await res.json()
        kpiData.value = {
          categoryBadge: data.categoryBadge || data.category_badge || 'AQUA KIP DATA MART',
          queryTitle: data.queryTitle || data.query_title || 'KPI отдела продаж',
          querySubtitle: data.querySubtitle || data.query_subtitle || '',
          updatedAtText: data.updatedAtText || data.updated_at_text || '',
          period: data.period || '',
          periodCode: data.periodCode || data.period_code || periodParam,
          periodLabel: data.periodLabel || data.period_label || '',
          summaryMetrics: (data.summaryMetrics || data.summary_metrics || []).map((m: any) => ({
            id: m.id,
            title: m.title,
            value: m.value,
            trend: m.trend,
            trendPositive: m.trendPositive !== undefined ? m.trendPositive : m.trend_positive,
            icon: m.icon
          })),
          managers: (data.managers || []).map((mgr: any) => ({
            id: mgr.id,
            dbId: mgr.dbId || mgr.db_id,
            name: mgr.name,
            role: mgr.role,
            avatar: mgr.avatar,
            rank: mgr.rank,
            isTopPerformer: mgr.isTopPerformer !== undefined ? mgr.isTopPerformer : mgr.is_top_performer,
            statusColor: mgr.statusColor || mgr.status_color || 'green',
            kpiPercent: mgr.kpiPercent !== undefined ? mgr.kpiPercent : (mgr.kpi_percent || 0),
            kpiBarColor: mgr.kpiBarColor || mgr.statusColor || mgr.status_color || 'green',
            salesAmount: mgr.salesAmount || mgr.salesFormatted || mgr.sales_formatted || (mgr.sales_amount ? `${mgr.sales_amount} ₸` : '0 ₸'),
            targetAmount: mgr.targetAmount !== undefined ? mgr.targetAmount : mgr.target_amount,
            targetFormatted: mgr.targetFormatted || mgr.target_formatted,
            dealsCount: mgr.dealsCount !== undefined ? mgr.dealsCount : (mgr.deals_count || 0),
            averageMargin: mgr.averageMargin !== undefined ? mgr.averageMargin : mgr.average_margin,
            overdueCommitments: mgr.overdueCommitments !== undefined ? mgr.overdueCommitments : mgr.overdue_commitments,
            trend: mgr.trend || '',
            trendPositive: mgr.trendPositive !== undefined ? mgr.trendPositive : (mgr.trend_positive || false),
            projects: mgr.projects || []
          })),
          chartData: data.chartData || data.chart_data,
          insight: data.insight || kpiData.value.insight
        }
      }
    } catch (e) {
      console.error('Failed to fetch KPI summary:', e)
    }
  }

  onMounted(() => {
    fetchKpiData()
  })

  async function handlePromptSubmit(prompt: string) {
    if (isGenerating.value) return  // R5: prevent duplicate submissions
    const prevView = currentView.value
    isGenerating.value = true

    try {
      const token = localStorage.getItem('mazory_access_token')
      const headers: Record<string, string> = {
        'Content-Type': 'application/json'
      }
      if (token) {
        headers['Authorization'] = `Bearer ${token}`
      }

      const res = await fetch(`${API_BASE}/chat/query/`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ prompt })
      })

      if (res.status === 401) {
        logout()
        currentView.value = prevView
        isAuthModalOpen.value = true
        return
      }

      if (res.ok) {
        currentView.value = 'dashboard'
        kpiData.value.queryTitle = prompt
        const responseData = await res.json()
        chatResponseText.value = responseData.text || ''
        
        if (responseData.widget) {
          activeWidget.value = responseData.widget
        } else {
          activeWidget.value = null
        }

        if (responseData.insights && responseData.insights.length > 0) {
          kpiData.value.insight.headline = responseData.insights[0]
          kpiData.value.insight.details = responseData.insights.slice(1).join(' ') || responseData.text
        }
      }
    } catch (e) {
      console.error('Failed to query chat orchestrator:', e)
    } finally {
      isGenerating.value = false
    }
  }

  function goHome() {
    currentView.value = 'welcome'
    activeWidget.value = null
  }

  return {
    currentView,
    isGenerating,
    activeWidget,
    chatResponseText,
    welcomeSuggestions,
    dashboardSuggestions,
    selectedPeriod,
    kpiData,
    fetchKpiData,
    handlePromptSubmit,
    goHome
  }
}
