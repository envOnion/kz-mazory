import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AuthModal from './AuthModal.vue'

afterEach(() => { vi.unstubAllGlobals(); vi.clearAllTimers(); vi.useRealTimers() })

describe('OTP request feedback', () => {
  it('keeps the phone step and shows the unknown user error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ error: 'Пользователя нет в системе' }) }))
    const wrapper = mount(AuthModal, { props: { isOpen: true } })
    await wrapper.get('input[type="tel"]').setValue('79991234567')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Пользователя нет в системе')
    expect(wrapper.find('input[type="tel"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Код подтверждения')
    wrapper.unmount()
  })
  it('describes a successful request as queued for delivery', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ cooldown: 45 }) }))
    const wrapper = mount(AuthModal, { props: { isOpen: true } })
    await wrapper.get('input[type="tel"]').setValue('79991234567')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Код поставлен на отправку')
    expect(wrapper.text()).not.toContain('Код отправлен')
    wrapper.unmount()
  })
})
