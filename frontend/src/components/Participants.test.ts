import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import WorkspaceView from './WorkspaceView.vue'
import { currentUser } from '../composables/session'
import type { Directory, Role } from '../types/platform'

const mocks = vi.hoisted(() => ({ api: vi.fn(), post: vi.fn() }))
vi.mock('../composables/api', () => ({ api: mocks.api, post: mocks.post, pollOperation: vi.fn(), download: vi.fn() }))
const directory: Directory = {
  teams: [], profiles: [], projects: [], chats: [{ id: 7, name: 'Продажи', team_id: 1 }],
  participants: [
    { id: 1, team_id: 1, team_name: 'Команда', display_name: 'Вячеслав', profile_id: 4, user_id: 5, phone: '79990000005', resolution_state: 'resolved', access_status: 'not_granted', aliases: ['Вячеслав Медведев'] },
    { id: 2, team_id: 1, team_name: 'Команда', display_name: 'Автор из TXT', profile_id: null, user_id: null, phone: '', resolution_state: 'phone_unknown', access_status: 'not_granted', aliases: ['Автор из TXT'] },
  ],
}
afterEach(() => { currentUser.value = null; vi.clearAllMocks() })
async function screen(roles: Role[] = ['team_lead']) {
  currentUser.value = { id: 1, name: 'Руководитель', phone: 'test', username: 'test', roles }
  mocks.api.mockImplementation(async (url: string) => url === '/directory/' ? directory : { results: [], count: 0, next: null })
  mocks.post.mockResolvedValue({ event_id: 12, state: 'pending' })
  const wrapper = mount(WorkspaceView)
  await flushPromises()
  return wrapper
}
describe('People directory', () => {
  it('shows confirmed phones and unknown authors without promising access', async () => {
    const wrapper = await screen()
    await wrapper.findAll('button').find(button => button.text() === 'Сотрудники')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="participant-1"]').text()).toContain('+79990000005')
    expect(wrapper.get('[data-testid="participant-1"]').text()).toContain('Доступ не предоставлен')
    expect(wrapper.get('[data-testid="participant-2"]').text()).toContain('Телефон не установлен')
    await wrapper.findAll('button').find(button => button.text().startsWith('Восстановить участников'))!.trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenCalledWith('/directory/participants-sync/', { config_id: 7 })
    wrapper.unmount()
  })
  it('does not expose the employee directory to a manager', async () => {
    const wrapper = await screen(['manager'])
    expect(wrapper.findAll('button').some(button => button.text() === 'Сотрудники')).toBe(false)
    wrapper.unmount()
  })
})
