import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PresetChart from './presets/PresetChart.vue'
import PresetProjectTable from './presets/PresetProjectTable.vue'
import type { ProjectWorkspaceSummary, PipelineData } from '../types/chat'
vi.mock('../composables/api', () => ({ api: vi.fn() }))

describe('reporting availability', () => {
  it('explains missing receipts without an empty canvas', () => {
    const wrapper = mount(PresetChart, { props: { data: { title: 'Поступления', unit: 'KZT', labels: [], datasets: [], empty_reason: 'Поступления ещё не зарегистрированы.' } } })
    expect(wrapper.get('[data-testid="chart-empty"]').text()).toContain('не зарегистрированы')
    expect(wrapper.find('canvas').exists()).toBe(false)
  })

  it('shows unknown finances, CRM origin and the coverage of all projects', () => {
    const row: ProjectWorkspaceSummary = {
      id: 1, name: 'Объект', company: 'Компания', manager: 'CRM owner', contract_amount: null, contract_formatted: 'Нет данных',
      paid_amount: null, paid_formatted: 'Нет данных', due_amount: null, due_formatted: 'Не определён', overpayment: null,
      contract_known: false, payments_known: false, balance_known: false, data_completeness: 'partial', review_available: false,
      missing_data_reasons: [{ code: 'payments_missing', message: 'Нет подтверждённых записей о поступлениях.' }],
      crm_formatted: '500.00 ₸', crm_amount: '500.00', stage_source: 'CRM', status: 'Выиграна', status_code: 'crm:WON',
      margin_percent: null, margin_alert: false, equipment: '', is_verified: false, priority: '', version: 0, currency: 'KZT', current_action: '', next_action: '',
    }
    const data: PipelineData = { total_count: 641, projects: [row], next_page: 2, stages: [{ code: 'crm:WON', label: 'Выиграна', count: 641, volume: null, volume_formatted: 'Договоры неизвестны', crm_volume_formatted: '500.00 ₸' }],
      conversion: { value: null, reason: 'Нет истории', cohort_size: 0, completed: 0 }, stage_duration_days: [],
      weighted_margin: null, margin_distribution: { low_under_15: 0, norm_15_to_20: 0, high_over_20: 0, unknown: 641 } }
    const wrapper = mount(PresetProjectTable, { props: { data } })
    expect(wrapper.text()).toContain('Нет данных')
    expect(wrapper.text()).toContain('CRM: 500.00 ₸')
    expect(wrapper.text()).toContain('Не определён')
    expect(wrapper.text()).toContain('Показано 1 из 641')
    expect(wrapper.findAll('tbody td')[3]?.text()).toBe('Нет данных')
    expect(wrapper.findAll('button').some(button => button.text() === 'Загрузить ещё')).toBe(true)
  })
})
