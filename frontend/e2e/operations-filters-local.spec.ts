import { test, expect } from '@playwright/test'
import { readFileSync } from './helpers/files'
import { authenticateShell } from './helpers/session'
import { join } from 'node:path'
import type { OperationPage, QueueJob, CrmChangeRow, RecordDetail } from '../src/types/messenger'

test.skip(process.env.MAZORY_OPERATIONS_FILTERS !== '1','Runs against the completed real business/provider fixture')
const directory=process.env.MAZORY_E2E_DIR!
const fixture=JSON.parse(readFileSync(join(directory,'messenger-session.json'),'utf8')) as {
  admin:{access:string};team_lead:{access:string};records:{allowed:number}
}
const headers={Authorization:`Bearer ${fixture.admin.access}`}

test('queue filters narrow the actual work/outbox rows; pagination survives a real polling cycle and reload',async({page,context,request})=>{
  const record=await (await request.get(`/api/messenger/records/${fixture.records.allowed}/`,{headers})).json() as RecordDetail
  const team=record.dialog.team_id!
  const expected=await (await request.get(`/api/processing/jobs/?source=source_work&team=${team}`,{headers})).json() as OperationPage<QueueJob>
  expect(expected.items.some(row=>row.raw_message_id===record.origin.raw_message_id)).toBe(true)
  expect(expected.items.every(row=>row.source==='source_work')).toBe(true)
  for(const query of ['source=invalid','team=invalid','from=invalid'])expect((await request.get('/api/processing/jobs/?'+query,{headers})).status()).toBe(400)
  const future=await (await request.get('/api/processing/jobs/?from=2099-01-01',{headers})).json() as OperationPage<QueueJob>
  expect(future.items).toEqual([])
  await authenticateShell(context);await page.goto('/queue?source=outbox')
  const rows=page.locator('tbody tr[data-job-source]')
  await expect(rows).toHaveCount(50)
  await page.getByRole('button',{name:'Загрузить ещё',exact:true}).click()
  await expect.poll(()=>rows.count()).toBeGreaterThan(50)
  const refreshed=Promise.all([
    page.waitForResponse(r=>r.url().includes('/api/processing/jobs/')&&!new URL(r.url()).searchParams.has('cursor')&&r.ok()),
    page.waitForResponse(r=>r.url().includes('/api/processing/jobs/')&&new URL(r.url()).searchParams.has('cursor')&&r.ok()),
  ])
  await refreshed
  await expect.poll(()=>rows.count()).toBeGreaterThan(50)
  await page.getByLabel('Команда очереди',{exact:true}).selectOption(String(team))
  await page.getByLabel('Источник задания',{exact:true}).selectOption('source_work')
  await expect(rows).toHaveCount(expected.items.length)
  await expect(page.locator('tbody tr[data-job-source="outbox"]')).toHaveCount(0)
  await expect(page.locator(`tbody tr[data-job-id="${expected.items[0]!.id}"]`)).toBeVisible()
  await page.getByLabel('Период очереди',{exact:true}).selectOption('7')
  await expect.poll(()=>new URL(page.url()).searchParams.get('period')).toBe('7')
  await page.reload()
  await expect(page.getByLabel('Команда очереди',{exact:true})).toHaveValue(String(team))
  await expect(page.getByLabel('Источник задания',{exact:true})).toHaveValue('source_work')
  await expect(page.getByLabel('Период очереди',{exact:true})).toHaveValue('7')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.setViewportSize({width:320,height:844})
  await expect(page.getByLabel('Команда очереди',{exact:true})).toBeInViewport({ratio:1})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({path:join(directory,'playwright/queue-filters-320.png')})
})

test('CRM filters use persisted readback, exact external IDs, actual method history and team access',async({page,context,request})=>{
  const provider=JSON.parse(readFileSync(join(directory,'provider-state.json'),'utf8')) as {tasks:Record<string,{id:string;responsibleId:string}>}
  const task=Object.values(provider.tasks).find(row=>row.responsibleId==='7')!
  expect(task).toBeDefined()
  const get=async(query:string):Promise<OperationPage<CrmChangeRow>>=>{
    const response=await request.get('/api/crm/changes/?'+query,{headers});expect(response.ok()).toBe(true);return response.json()
  }
  const data=await get('object_type=task&external_id='+task.id)
  expect(data.items.length).toBeGreaterThan(0)
  expect(data.items.every(row=>row.external_id===task.id&&row.object_type==='task'&&row.actor_label.length>0)).toBe(true)
  const team=data.items[0]!.team_id!
  expect(typeof team).toBe('number')
  const record=await (await request.get(`/api/messenger/records/${fixture.records.allowed}/`,{headers})).json() as RecordDetail
  expect(record.dialog.team_id).not.toBe(team)
  expect((await get(`object_type=task&external_id=${task.id}&team=${record.dialog.team_id}`)).items).toEqual([])
  const denied=await request.get(`/api/crm/changes/?object_type=task&external_id=${task.id}&team=${team}`,{headers:{Authorization:`Bearer ${fixture.team_lead.access}`}})
  expect(denied.ok()).toBe(true);expect((await denied.json() as OperationPage<CrmChangeRow>).items).toEqual([])
  const methods=(row:CrmChangeRow)=>[...(row.methods??[]),...(row.method?[row.method]:[])]
  const recent=await get('source=journal&action=delete&limit=100')
  const operation=recent.items.find(row=>row.object_type==='deal'&&row.external_id&&methods(row).some(method=>/\.delete(\.json)?$/.test(method)))!
  expect(operation).toBeDefined()
  const action=methods(operation).map(method=>method.match(/\.(add|update|delete|complete|defer)(\.json)?$/)?.[1]).find(Boolean)!
  const filtered=await get(`source=${operation.source}&action=${action}&object_type=${operation.object_type}&external_id=${operation.external_id}`)
  expect(filtered.items.some(row=>row.id===operation.id&&row.source===operation.source)).toBe(true)
  expect(filtered.items.every(row=>row.source===operation.source&&methods(row).some(method=>new RegExp('\\.'+action+'(\\.json)?$').test(method)))).toBe(true)
  for(const query of ['source=invalid','action=invalid','team=invalid'])expect((await request.get('/api/crm/changes/?'+query,{headers})).status()).toBe(400)
  await authenticateShell(context)
  await page.goto(`/crm-changes?object_type=task&external_id=${task.id}&source=delivery`)
  const link=page.locator(`a[href="https://localhost/company/personal/user/7/tasks/task/view/${task.id}/"]`).first()
  await expect(link).toBeVisible()
  await page.getByLabel('Команда CRM',{exact:true}).selectOption(String(team))
  await expect.poll(()=>new URL(page.url()).searchParams.get('team')).toBe(String(team))
  await page.reload()
  await expect(page.getByLabel('Внешний ID CRM',{exact:true})).toHaveValue(task.id)
  await expect(page.getByLabel('Источник CRM',{exact:true})).toHaveValue('delivery')
  await expect(page.getByLabel('Команда CRM',{exact:true})).toHaveValue(String(team))
  await expect(link).toBeVisible()
  await page.getByLabel('Внешний ID CRM',{exact:true}).fill('999999999')
  await page.getByRole('button',{name:'Найти объект',exact:true}).click()
  await expect(page.getByText('Изменений нет',{exact:true})).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.goto(`/crm-changes?source=${operation.source}&action=${action}&object_type=${operation.object_type}&external_id=${operation.external_id}`)
  await expect(page.getByLabel('Действие CRM',{exact:true})).toHaveValue(action)
  await expect(page.locator(`[data-crm-id="${operation.source}:${operation.id}"]`)).toBeVisible()
  await page.setViewportSize({width:320,height:844})
  await expect(page.getByRole('button',{name:'Найти объект',exact:true})).toBeInViewport({ratio:1})
  await expect(page.getByLabel('Команда CRM',{exact:true})).toBeInViewport({ratio:1})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({path:join(directory,'playwright/crm-filters-320.png')})
})
