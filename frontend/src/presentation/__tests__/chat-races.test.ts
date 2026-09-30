import { describe, it, expect, vi } from 'vitest'
import { effectScope, nextTick } from 'vue'
const mocks = vi.hoisted(() => ({ api: vi.fn().mockResolvedValue({}), post: vi.fn(), pollOperation: vi.fn() }))
vi.mock('../../composables/api', () => mocks)
vi.mock('../../composables/session', async () => { const { ref } = await import('vue'); return { currentUser: ref({id:1,roles:['team_lead']}), sessionVersion: ref(0) } })
vi.mock('../../composables/useAuth', async () => { const { ref } = await import('vue'); return { useAuth: () => ({ isAuthModalOpen: ref(false) }) } })
import { useChat } from '../../composables/useChat'
import type { ChatResponse } from '../../types/chat'
// No provider/UI implementation is mirrored: these exercise observable race behavior.
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
const response = (text: string) => ({ text, widget:null, presentation:null, quotes:[], insights:[], prompt:'question' })
describe('operation generation boundary', () => {
  it('late cancelled response cannot replace a newer answer or clear its loading state', async () => {
    const first = deferred<ChatResponse>()
    mocks.post.mockResolvedValueOnce({operation_id:1}).mockResolvedValueOnce({operation_id:2})
    mocks.pollOperation.mockReturnValueOnce(first.promise).mockResolvedValueOnce(response('Second answer'))
    const scope=effectScope(); const chat=scope.run(()=>useChat())!
    const old=chat.handlePromptSubmit('First');await nextTick();await Promise.resolve()
    await chat.cancel();await chat.handlePromptSubmit('Second')
    first.resolve(response('Old answer'));await old
    expect(chat.chatResponseText.value).toBe('Second answer');expect(chat.isGenerating.value).toBe(false)
    expect(mocks.api).toHaveBeenCalledWith('/operations/1/', {method:'DELETE'})
    scope.stop()
  })
  it('cancels a receipt that arrives after cancellation', async () => {
    const receipt=deferred<{operation_id:number,status:string}>();mocks.post.mockReturnValueOnce(receipt.promise)
    const scope=effectScope();const chat=scope.run(()=>useChat())!
    const pending=chat.handlePromptSubmit('First');await chat.cancel();receipt.resolve({operation_id:3,status:'queued'});await pending
    expect(mocks.api).toHaveBeenCalledWith('/operations/3/', {method:'DELETE'});expect(chat.chatResponseText.value).toBe('');scope.stop()
  })
})
