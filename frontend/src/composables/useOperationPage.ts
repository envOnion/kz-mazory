import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { api } from './api'
import type { OperationPage, TeamOption } from '../types/messenger'

/** Refresh exactly the visible pages, cancelling obsolete filters and preserving pagination. */
export function useOperationPage<T extends {id:number;source:string}>(url:(cursor:string|null)=>string, enabled=()=>true) {
  const rows=shallowRef<T[]>([]), teams=ref<TeamOption[]>([]), stateLabels=ref<Record<string,string>>({})
  const error=ref(''), loading=ref(true), cursor=ref<string|null>(null), asOf=ref('')
  let pending:AbortController|null=null, timer:number|null=null, pages=1, delay=5000, stopped=false
  async function load(reset=true,poll=false) {
    if(stopped) return
    pending?.abort();const request=new AbortController();pending=request
    if(reset&&!poll) pages=1
    loading.value=true
    try {
      let next=reset?null:cursor.value
      const values=new Map((reset?[]:rows.value).map(row=>[`${row.source}:${row.id}`,row]))
      for(let index=0;index<(reset?pages:1);index++) {
        const result=await api<OperationPage<T>>(url(next),{signal:request.signal})
        if(request.signal.aborted||stopped) return
        for(const row of result.items) values.set(`${row.source}:${row.id}`,row)
        teams.value=result.teams;stateLabels.value=result.state_labels ?? {};asOf.value=result.as_of
        next=result.next_cursor
        if(!next) break
      }
      rows.value=[...values.values()];cursor.value=next
      if(!reset) pages++
      error.value='';delay=5000
    } catch(e) {
      if(!request.signal.aborted) {error.value=e instanceof Error?e.message:'Не удалось обновить данные';delay=Math.min(delay*2,30000)}
    } finally {if(!request.signal.aborted) loading.value=false}
  }
  function schedule(){if(timer)clearTimeout(timer);if(!stopped)timer=window.setTimeout(refresh,delay)}
  function refresh(){if(document.hidden||!enabled()){schedule();return}void load(true,true).finally(schedule)}
  function visibility(){if(!document.hidden)refresh()}
  onMounted(()=>{void load().finally(schedule);document.addEventListener('visibilitychange',visibility)})
  onBeforeUnmount(()=>{stopped=true;pending?.abort();if(timer)clearTimeout(timer);document.removeEventListener('visibilitychange',visibility)})
  return {rows,teams,stateLabels,error,loading,cursor,asOf,load}
}
