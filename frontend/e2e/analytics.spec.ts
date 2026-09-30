import { test, expect } from '@playwright/test'
const document = { version: '1.0', title: 'Недельные поступления', summary: '', blocks: [{ id: 'weekly', kind: 'line', dataset_id: 'ds', encoding: { category: 'payment_week', series: 'credited_manager', value: 'received_amount' }, size: 'wide' }], datasets: { ds: { dataset_id: 'ds', columns: [{ name: 'payment_week', type: 'date', unit: null }, { name: 'credited_manager', type: 'text', unit: null }, { name: 'received_amount', type: 'money', unit: 'KZT' }], rows: [{ payment_week: '2026-09-21', credited_manager: '<img src=x onerror=alert(1)>', received_amount: '80.00' }, { payment_week: '2026-09-28', credited_manager: '<img src=x onerror=alert(1)>', received_amount: '123.45' }], normalized_query: { currency: 'KZT', filters: [{ field: 'project_type', op: 'eq', value: 'state' }], date_range: { start: '2026-09-01', end_exclusive: '2026-10-01' } }, timezone: 'Asia/Almaty', coverage: { status: 'partial', message: 'Полнота истории не подтверждена' }, returned_count: 2, total_groups: 2, truncated: false, definition: 'Подтверждённые поступления', evidence: [] } } }
test('renders real ECharts, safe labels and mobile layout without stale KPI or microphone', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.route('**/api/**', async route => {
    const path=new URL(route.request().url()).pathname
    const json=path.endsWith('/auth/refresh/') ? (route.request().method()==='GET' ? { csrf_token:'fixture',has_session:true } : { access:'fixture-token',user:{id:1,name:'Reviewer',phone:'70000000000',username:'review',roles:['team_lead']},csrf_token:'fixture',session_id:1 }) : path.endsWith('/directory/') ? { teams:[],profiles:[],projects:[] } : path.endsWith('/chat/query/') ? {operation_id:1,status:'queued'} : path.endsWith('/operations/1/') ? {id:1,status:'succeeded',result:{prompt:'query',text:'Результат представлен ниже.',widget:null,presentation:document,quotes:[],insights:[]}} : {}
    await route.fulfill({ json })
  })
  await page.goto('/')
  const input=page.getByPlaceholder('Спросите Mazory...');await input.waitFor()
  await input.fill('Покажи недельные поступления только по государственным проектам')
  await input.press('Enter')
  await expect(page.getByTestId('presentation')).toBeVisible()
  await expect(page.getByRole('heading',{name:'Недельные поступления',exact:true})).toBeVisible()
  await expect(page.locator('[data-testid="presentation"] canvas')).toHaveCount(1)
  await expect(page.getByTitle('Голосовой ввод')).toHaveCount(0)
  await expect(page.getByText('Прогноз поступлений')).toHaveCount(0)
  await page.getByText('Данные графика',{exact:true}).click()
  await expect(page.getByRole('cell',{name:'123.45',exact:true})).toBeVisible()
  await expect(page.locator('[data-testid="presentation"] img')).toHaveCount(0)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({ path:'/tmp/mazory-analytics-mobile.png', fullPage:true })
})
