import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ProfileMcpConnection from './ProfileMcpConnection.vue'
import type { McpConnectionResponse } from '../../types/mcp'

const mocks = vi.hoisted(() => ({ api: vi.fn(), post: vi.fn() }))
vi.mock('../../composables/api', () => mocks)
vi.mock('../../composables/session', () => ({ API_BASE: '/api' }))
const response: McpConnectionResponse = { connection: { id: 1, token: 'mcp_test_personal', created_at: '2026-10-07T10:00:00Z', last_used_at: null, expires_at: null } }
afterEach(() => vi.resetAllMocks())

describe('Personal MCP connection', () => {
  it('loads a recoverable masked token and shows the endpoint and header', async () => {
    mocks.api.mockResolvedValue(response)
    const wrapper = mount(ProfileMcpConnection)
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledWith('/mcp/connection/', { cache: 'no-store' })
    expect(wrapper.get<HTMLInputElement>('input[type=password]').element.value).toBe('mcp_test_personal')
    expect(wrapper.text()).toContain('Authorization: Bearer')
    const show = wrapper.findAll('button').find(button => button.text() === 'Показать токен')
    await show?.trigger('click')
    expect(wrapper.findAll('input').at(1)?.attributes('type')).toBe('text')
    wrapper.unmount()
  })
  it('does not create a credential when a revoked connection is opened', async () => {
    mocks.api.mockResolvedValue({ connection: null })
    const wrapper = mount(ProfileMcpConnection)
    await flushPromises()
    expect(mocks.post).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Получить токен MCP')
    wrapper.unmount()
  })
  it('explicitly revokes the credential and clears the token from the view', async () => {
    mocks.api.mockResolvedValue(response)
    mocks.post.mockResolvedValue({ connection: null })
    const wrapper = mount(ProfileMcpConnection)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'Отозвать токен')?.trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenCalledWith('/mcp/connection/', { action: 'revoke' })
    expect(wrapper.find('input[type=password]').exists()).toBe(false)
    wrapper.unmount()
  })
})
