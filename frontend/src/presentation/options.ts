import type { EChartsOption } from 'echarts'
import type { AnalyticsCell, AnalyticsDataset } from '../types/analytics'
import type { PresentationBlock } from '../types/presentation'
const palette = ['#818cf8', '#34d399', '#fbbf24', '#f472b6', '#38bdf8', '#a78bfa', '#fb923c']
export const fieldLabels: Record<string, string> = { message_day: 'День сообщения', message_week: 'Неделя сообщения', message_month: 'Месяц сообщения', message_count: 'Сообщения', chat: 'Чат', crm_amount: 'Сумма сделки CRM', crm_manager: 'Ответственный CRM', commitment_id: 'Обязательство', text: 'Текст', project: 'Проект', responsible: 'Ответственный', deadline: 'Срок', project_id: 'Проект', project_manager: 'Менеджер проекта', credited_manager: 'Менеджер поступления', responsible_manager: 'Ответственный', manager: 'Менеджер', team: 'Команда', status: 'Статус', project_status: 'Статус проекта', project_type: 'Тип проекта', payment_day: 'День оплаты', payment_week: 'Неделя оплаты', payment_month: 'Месяц оплаты', month: 'Месяц', received_amount: 'Поступления', payment_count: 'Платежи', project_count: 'Проекты', contract_amount: 'Сумма договора', confirmed_cost: 'Подтверждённая стоимость', contract_margin_percent: 'Расчётная маржа', commitment_count: 'Обязательства', overdue_count: 'Просроченные обязательства', target_amount: 'План', effective_deadline_day: 'День срока', effective_deadline_week: 'Неделя срока', effective_deadline_month: 'Месяц срока', manager_id: 'Менеджер', team_id: 'Команда', amount: 'Сумма оплаты' }
export function fieldLabel(name: string): string { return fieldLabels[name] || 'Показатель' }
export function datasetLabel(ds: AnalyticsDataset, name: string): string { return ds.columns.find(c => c.name === name)?.label || (name === 'project_count' && ds.normalized_query.dataset === 'crm_projects' ? 'Количество сделок' : fieldLabel(name)) }
export function display(value: AnalyticsCell, unit?: string | null, kind?: string): string {
  if (value === null) return 'Нет данных'
  const text = String(value)
  if (kind === 'date' && /^\d{4}-\d{2}-\d{2}/.test(text)) {
    const [year, month, day] = text.slice(0, 10).split('-')
    return `${day}.${month}.${year}${text.length > 10 ? ` ${text.slice(11, 16)}` : ''}`
  }
  if ((unit || ['money', 'percent', 'count'].includes(kind || '')) && /^-?\d+(?:\.\d+)?$/.test(text)) {
    const [integer, fraction] = text.split('.')
    return `${integer!.replace(/\B(?=(\d{3})+(?!\d))/g, '\u202f')}${fraction ? `,${fraction}` : ''}${unit ? ` ${unit}` : ''}`
  }
  return `${text}${unit ? ` ${unit}` : ''}`
}
export function plot(value: AnalyticsCell): number | null {
  if (value === null) return null
  const number = Number(value)
  if (!Number.isFinite(number) || Math.abs(number) > Number.MAX_SAFE_INTEGER) throw new Error('Число вне диапазона')
  return number
}
export function chartOptions(b: PresentationBlock, ds: AnalyticsDataset, width = 800): EChartsOption {
  const e = b.encoding
  const label = (value: AnalyticsCell | undefined) => value === null || value === undefined ? 'Не назначен' : String(value)
  const numeric = (row: Record<string, AnalyticsCell>, field?: string) => plot(field ? row[field] ?? null : null)
  const unit = ds.columns.find(c => c.name === (e.value || e.y))?.unit
  const countInterval = ds.columns.find(c => c.name === (e.value || e.y))?.type === 'count' ? 1 : undefined
  const base: EChartsOption = { color: palette, backgroundColor: 'transparent', textStyle: { color: '#cbd5e1' }, animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    tooltip: { trigger: 'item', renderMode: 'richText' }, legend: { type: 'scroll', textStyle: { color: '#cbd5e1' }, bottom: 0 },
    grid: { left: 60, right: 20, top: 30, bottom: 65 },
  }
  const axes: EChartsOption = { xAxis: { type: 'category', axisLabel: { color: '#94a3b8', hideOverlap: true } }, yAxis: { type: 'value', minInterval: countInterval, name: unit || '', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#25304a' } } } }
  if (['bar', 'line', 'area'].includes(b.kind)) {
    const categories = [...new Set(ds.rows.map(row => label(row[e.category!])))]
    const horizontal = b.kind === 'bar' && (ds.normalized_query.dataset === 'crm_projects' || categories.some(c => c.length > 24))
    const labelWidth = Math.max(75, Math.min(170, width * .38))
    const series = e.series ? [...new Set(ds.rows.map(row => label(row[e.series!])))] : [e.value!]
    const dated = ds.columns.some(c => c.name === e.category && c.type === 'date')
    const monthly = e.category === 'month' || e.category?.endsWith('_month')
    const oneYear = new Set(categories.map(c => c.slice(0, 4))).size === 1
    const calendarLabel = (value: string) => dated && /^\d{4}-\d{2}-\d{2}$/.test(value)
      ? new Intl.DateTimeFormat('ru-RU', { timeZone:'UTC', month:'short', ...(monthly ? {} : { day:'2-digit' }), ...(oneYear ? {} : { year:'numeric' }) }).format(new Date(`${value}T00:00:00Z`))
      : value
    return { ...base, ...axes, legend: series.length > 1 ? base.legend : { show: false },
      grid: { ...base.grid, left: horizontal ? labelWidth + 20 : 55, bottom: b.kind === 'bar' ? (series.length > 1 ? 65 : 40) : (series.length > 1 ? 100 : 70) },
      xAxis: horizontal ? { type: 'value', minInterval: countInterval, name: unit || '', axisLabel: { color: '#a0acc1' }, splitLine: { lineStyle: { color: '#25304a' } } } : { type:'category', data: categories, axisLabel: { color:'#94a3b8', hideOverlap:true, formatter:calendarLabel } },
      yAxis: horizontal ? { type: 'category', inverse: true, data: categories, axisLabel: { color: '#a0acc1', width: labelWidth, overflow: 'break', interval: 0 } } : axes.yAxis,
      dataZoom: b.kind === 'bar' ? undefined : [{ type: 'inside' }, { type: 'slider', bottom: series.length > 1 ? 30 : 10, height: 15 }],
      series: series.map(name => ({ name: e.series ? name : datasetLabel(ds, name), type: b.kind === 'bar' ? 'bar' : 'line', areaStyle: b.kind === 'area' ? {} : undefined,
        data: categories.map(category => { const row = ds.rows.find(r => label(r[e.category!]) === category && (!e.series || label(r[e.series]) === name)); return { value: row ? numeric(row, e.value) : null, exact: row ? display(row[e.value!] ?? null, unit) : 'Нет данных' } }),
      })),
      tooltip: { trigger: 'item', renderMode: 'richText', formatter: params => { const p = Array.isArray(params) ? params[0] : params; const item = p.data as { exact?: string }; return `${p.name}\n${p.seriesName}: ${item.exact || 'Нет данных'}` } },
    }
  }
  if (b.kind === 'scatter') return { ...base, ...axes, xAxis: { type: 'value', name: ds.columns.find(c => c.name === e.x)?.unit || '' },
    series: [{ type: 'scatter', symbolSize: 12, data: ds.rows.filter(row => row[e.x!] !== null && row[e.y!] !== null).map(row => ({ name: label(row[e.label || 'project_id']), value: [numeric(row, e.x)!, numeric(row, e.y)!], exact: `${display(row[e.x!] ?? null, ds.columns.find(c => c.name === e.x)?.unit)}; ${display(row[e.y!] ?? null, unit)}` })) }],
    tooltip: { renderMode: 'richText', formatter: params => { const p = Array.isArray(params) ? params[0] : params; return `${p.name}\n${(p.data as { exact: string }).exact}` } },
  }
  if (b.kind === 'donut' || b.kind === 'funnel') {
    const order = ['lead', 'qualification', 'design', 'proposal_sent', 'contract_signing', 'in_execution', 'completed', 'stalled', 'lost']
    const rows = b.kind === 'funnel' ? [...ds.rows].sort((a, c) => order.indexOf(String(a[e.category!])) - order.indexOf(String(c[e.category!]))) : ds.rows
    const data = rows.filter(r => r[e.value!] !== null).map(row => ({ name: label(row[e.category!]), value: numeric(row, e.value)!, exact: display(row[e.value!] ?? null, unit) }))
    return { ...base, series: b.kind === 'donut' ? [{ type: 'pie', radius: ['40%', '65%'], data, label: { color: '#cbd5e1' } }] : [{ type: 'funnel', sort: 'none', left: '15%', width: '70%', bottom: 50, data, label: { color: '#cbd5e1' } }], tooltip: { renderMode: 'richText', formatter: params => { const p = Array.isArray(params) ? params[0] : params; return `${p.name}: ${(p.data as { exact: string }).exact}` } } }
  }
  if (b.kind === 'waterfall') {
    const steps = b.steps || []
    let cumulative = 0
    const bottoms: number[] = [], heights: number[] = []
    for (const step of steps) { const value = plot(step.value)!; if (step.kind !== 'delta') { bottoms.push(Math.min(0, value)); heights.push(Math.abs(value)); cumulative = value } else { const next = cumulative + value; bottoms.push(Math.min(cumulative, next)); heights.push(Math.abs(value)); cumulative = next } }
    return { ...base, ...axes, xAxis: { ...axes.xAxis, data: steps.map(s => s.label) }, series: [{ type: 'bar', stack: 'total', silent: true, itemStyle: { color: 'transparent' }, data: bottoms }, { type: 'bar', stack: 'total', data: heights }], tooltip: { renderMode: 'richText', formatter: params => { const p = Array.isArray(params) ? params[0] : params; return display(steps[p.dataIndex]?.value ?? null, ds.normalized_query.currency as string) } } }
  }
  throw new Error('Неподдерживаемый график')
}
