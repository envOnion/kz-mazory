import { ref } from 'vue'
import type { ViewMode, KpiDashboardData } from '../types/chat'

export function useChat() {
  const currentView = ref<ViewMode>('welcome')
  const isGenerating = ref(false)

  // Welcome state suggestions (Screen 2 from user attachments)
  const welcomeSuggestions = ref([
    'Что требует моего внимания? ↗',
    'Покажи KPI команды ↗',
    'Какие сделки зависли? ↗'
  ])

  // Active dashboard suggestions (Screen 1 from user attachments)
  const dashboardSuggestions = ref([
    'Сравни с прошлым месяцем ↗',
    'Покажи воронку продаж ↗',
    'Кто может помочь Алине? ↗'
  ])

  // High-fidelity KPI Data matching the user's design screenshot
  const kpiData = ref<KpiDashboardData>({
    categoryBadge: 'AI АНАЛИЗ',
    queryTitle: 'Покажи KPI менеджеров',
    querySubtitle: 'Актуальные показатели по команде продаж',
    updatedAtText: 'Обновлено сегодня в 10:24',
    summaryMetrics: [
      {
        id: 'total-sales',
        title: 'Общие продажи',
        value: '24 500 000 ₽',
        trend: '+12% к прошлому месяцу',
        trendPositive: true,
        icon: 'bar-chart'
      },
      {
        id: 'plan-completion',
        title: 'Выполнение плана',
        value: '87%',
        trend: '+6 п.п. к прошлому месяцу',
        trendPositive: true,
        icon: 'target'
      },
      {
        id: 'deals-count',
        title: 'Количество сделок',
        value: '142',
        trend: '+18% к прошлому месяцу',
        trendPositive: true,
        icon: 'users'
      }
    ],
    managers: [
      {
        id: 'm1',
        name: 'Максим Кузнецов',
        role: 'Старший менеджер',
        avatar: 'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=200&q=80',
        isTopPerformer: true,
        statusColor: 'green',
        kpiPercent: 104,
        kpiBarColor: 'green',
        salesAmount: '7 800 000 ₽',
        dealsCount: 28,
        trend: '+26% к прошлому месяцу',
        trendPositive: true
      },
      {
        id: 'm2',
        name: 'Ирина Волкова',
        role: 'Менеджер по продажам',
        avatar: 'https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?auto=format&fit=crop&w=200&q=80',
        statusColor: 'green',
        kpiPercent: 92,
        kpiBarColor: 'green',
        salesAmount: '5 400 000 ₽',
        dealsCount: 24,
        trend: '+14% к прошлому месяцу',
        trendPositive: true
      },
      {
        id: 'm3',
        name: 'Даниил Соколов',
        role: 'Менеджер по продажам',
        avatar: 'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?auto=format&fit=crop&w=200&q=80',
        statusColor: 'yellow',
        kpiPercent: 78,
        kpiBarColor: 'yellow',
        salesAmount: '4 900 000 ₽',
        dealsCount: 22,
        trend: '+6% к прошлому месяцу',
        trendPositive: true
      },
      {
        id: 'm4',
        name: 'Алина Смирнова',
        role: 'Менеджер по продажам',
        avatar: 'https://images.unsplash.com/photo-1544005313-94ddf0286df2?auto=format&fit=crop&w=200&q=80',
        statusColor: 'red',
        kpiPercent: 61,
        kpiBarColor: 'red',
        salesAmount: '3 200 000 ₽',
        dealsCount: 14,
        trend: '-18% к прошлому месяцу',
        trendPositive: false
      },
      {
        id: 'm5',
        name: 'Егор Новиков',
        role: 'Менеджер по продажам',
        avatar: 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?auto=format&fit=crop&w=200&q=80',
        statusColor: 'green',
        kpiPercent: 88,
        kpiBarColor: 'green',
        salesAmount: '3 200 000 ₽',
        dealsCount: 18,
        trend: '+11% к прошлому месяцу',
        trendPositive: true
      }
    ],
    insight: {
      badge: 'AI-инсайт',
      source: 'На основе анализа сделок, активности и конверсий',
      headline: 'Лучший результат у Максима — 104% плана. У Алины — 61%.',
      details: 'Основная причина отставания — снижение конверсии (меньше сделок при том же объеме активности).',
      actions: [
        { id: 'why', label: 'Почему?', icon: 'search' },
        { id: 'deals', label: 'Показать сделки', icon: 'file-text' },
        { id: 'compare', label: 'Сравнить с прошлым месяцем', icon: 'bar-chart-2' },
        { id: 'contact', label: 'Написать сотруднику', icon: 'send' }
      ]
    }
  })

  function handlePromptSubmit(prompt: string) {
    // Transition to dashboard view
    currentView.value = 'dashboard'
    kpiData.value.queryTitle = prompt.includes('KPI') ? prompt : `Покажи KPI: ${prompt}`
  }

  function goHome() {
    currentView.value = 'welcome'
  }

  return {
    currentView,
    isGenerating,
    welcomeSuggestions,
    dashboardSuggestions,
    kpiData,
    handlePromptSubmit,
    goHome
  }
}
