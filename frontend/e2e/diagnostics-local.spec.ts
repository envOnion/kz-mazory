import { test, expect } from '@playwright/test'
import { readFileSync } from './helpers/files'
import { authenticateShell } from './helpers/session'
import { join } from 'node:path'
interface DiagnosticPage<T>{resource:string;items:T[];next_cursor:string|null;columns:{key:string;label:string}[]}
const directory=process.env.MAZORY_E2E_DIR!
const fixture=JSON.parse(readFileSync(join(directory,'messenger-session.json'),'utf8')) as {admin:{access:string};team_lead:{access:string};client:{access:string}}
test('system diagnostics uses real provider/Q2/version ledgers, preserves identifiers and restricts access',async({page,context,request})=>{
  const headers={Authorization:`Bearer ${fixture.admin.access}`}
  expect((await request.get('/api/settings/diagnostics/')).status()).toBe(401)
  for(const role of [fixture.team_lead,fixture.client])expect((await request.get('/api/settings/diagnostics/',{headers:{Authorization:`Bearer ${role.access}`}})).status()).toBe(403)
  for(const resource of ['audit','reservations','usage','versions','operations']){
    const response=await request.get(`/api/settings/diagnostics/?resource=${resource}`,{headers})
    expect(response.status()).toBe(200)
    const data=await response.json() as DiagnosticPage<{id:number}>
    expect(data.items.every(row=>typeof row.id==='number')).toBe(true)
    expect(data.columns.length).toBeGreaterThan(1)
  }
  const tasks=await (await request.get('/api/settings/diagnostics/?resource=q2',{headers})).json() as DiagnosticPage<{id:string}>
  expect(tasks.items.length).toBeGreaterThan(0)
  expect(tasks.items.every(row=>typeof row.id==='string')).toBe(true)
  if(tasks.next_cursor){
    const next=await (await request.get('/api/settings/diagnostics/?resource=q2&cursor='+encodeURIComponent(tasks.next_cursor),{headers})).json() as typeof tasks
    expect(next.items.every(row=>!tasks.items.some(old=>old.id===row.id))).toBe(true)
  }
  const coverage=await (await request.get('/api/settings/diagnostics/?resource=coverage',{headers})).json() as DiagnosticPage<{raw_total:number;auth_missing:number}>
  expect(coverage.items[0]!.raw_total).toBeGreaterThan(0)
  expect(typeof coverage.items[0]!.auth_missing).toBe('number')
  expect((await request.get('/api/settings/diagnostics/?resource=usage&object=not-an-integer',{headers})).status()).toBe(400)
  expect((await request.get('/api/settings/diagnostics/?resource=unknown',{headers})).status()).toBe(400)
  await authenticateShell(context)
  await page.goto('/settings?section=system')
  await page.getByRole('button',{name:'Расход провайдера',exact:true}).click()
  await expect(page.getByRole('columnheader',{name:'Входные токены',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'Результаты Q2',exact:true}).click()
  await page.getByLabel('Точный ID записи',{exact:true}).fill(tasks.items[0]!.id)
  await page.getByRole('button',{name:'Найти запись',exact:true}).click()
  await expect(page.getByTestId('diagnostic-table').locator('tbody tr')).toHaveCount(1)
  await page.reload()
  await expect(page.getByTestId('diagnostic-table')).toContainText(tasks.items[0]!.id)
  await expect(page.getByRole('alert')).toHaveCount(0)
})
