export const OPERATION_PERIODS=[{id:'7',label:'7 дней',days:7},{id:'30',label:'30 дней',days:30},{id:'90',label:'90 дней',days:90},{id:'',label:'Всё время',days:0}]
export function periodStart(value:string):string|null {
  const days=OPERATION_PERIODS.find(row=>row.id===value)?.days
  return days ? new Date(Date.now()-days*86400000).toISOString() : null
}
