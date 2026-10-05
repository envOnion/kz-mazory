import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AutonomousReports from './AutonomousReports.vue'
import type { AutonomousOverview } from '../types/autonomous'
const mocks = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../composables/api', () => ({ api: mocks.api }))
const data: AutonomousOverview = {
  enabled: true, crm_enabled: true, policy_version: 'v1', source: 'WhatsApp', as_of: '2026-10-05T12:00:00Z',
  totals: [{ currency: 'KZT', direction: 'income', amount_precision: 'approximate', amount: '42000000', count: 1 }],
  series: [], decisions: [], waiting: [{ candidate_id: 5, project_id: null, outcome: 'deferred', reason_code: 'ambiguous_project', explanation: 'Несколько объектов с одинаковым обозначением.' }],
  coverage: [{ source_scope: 'group', complete_through: null, gaps: [{ raw_message_id: 1, state: 'missing_media' }], counts: {}, updated_at: '2026-10-05T12:00:00Z' }],
  reports: [{ id: 7, created_at: '2026-10-05T12:00:00Z', payload: { totals: [], projects: 8, contract_known: 2, open_commitments: 3, missing_deadline: 1, cutoff: '2026-10-05' } }],
  observations: [{ id: 3, project_id: null, project__name: null, event_type: 'reported_debt', occurred_at: null, payload: { amount: '50874000', currency: 'KZT', evidence: 'Отчет о долге' } }],
  crm_deliveries: [{ id: 11, event_type: 'autonomous_crm', state: 'failed', error_code: 'blocked_missing_external_assignee', next_attempt_at: '2026-10-05' }], schedule: [],
}
afterEach(() => vi.resetAllMocks())
describe('Autonomous reports', () => {
  it('distinguishes approximate receipts, debt, missing evidence and CRM delay', async () => {
    mocks.api.mockResolvedValue(data)
    const screen = mount(AutonomousReports)
    await flushPromises()
    expect(screen.text()).toContain('Приблизительные суммы')
    expect(screen.text()).toContain('42 000 000')
    expect(screen.text()).toContain('Долг')
    expect(screen.text()).toContain('Эти показатели не увеличивают сумму поступлений')
    expect(screen.text()).toContain('Несколько объектов с одинаковым обозначением')
    expect(screen.text()).toContain('аккаунт CRM не установлен')
    expect(screen.text()).toContain('Открытых обязательств: 3')
    expect(screen.findAll('button').map(button => button.text())).toEqual(['Обновить'])
    screen.unmount()
  })
  it('shows a fetch failure rather than empty finance totals', async () => {
    mocks.api.mockRejectedValue(new Error('Нет связи с сервером'))
    const screen = mount(AutonomousReports)
    await flushPromises()
    expect(screen.get('[role="alert"]').text()).toBe('Нет связи с сервером')
    expect(screen.text()).not.toContain('Зарегистрированных финансовых движений нет')
    screen.unmount()
  })
})
