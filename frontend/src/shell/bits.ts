import { defineComponent, h, type PropType } from 'vue'

// Компактные shell-компоненты (h-реализация, без лишних SFC).
// Статусы: текст + иконка-символ вместе с цветом (WCAG AA, §3.2).

const TONE_BY_STATE: Record<string, string> = {
  received: 'info', queued: 'info', enqueued: 'info', pending: 'info', preparing: 'info',
  analyzing: 'info', sending: 'info', processing: 'info', reading: 'info', accepted: 'info',
  need_context: 'warn', needs_review: 'warn', awaiting: 'warn', preparing_send: 'warn', stale: 'warn',
  unknown: 'warn', unavailable: 'warn', expired: 'warn', unconfirmed: 'warn', partial: 'warn',
  error: 'err', failed: 'err',
  analyzed: 'ok', no_crm_actions: 'ok', delivered: 'ok', delivered_provider: 'ok',
  sent: 'ok', confirmed: 'ok', resolved: 'ok', answered: 'ok', done: 'ok', completed: 'ok',
  success: 'ok', not_required: 'muted', not_applicable: 'muted', disabled: 'muted',
}
const MARK: Record<string, string> = { ok: '●', warn: '▲', err: '✕', info: '◌', muted: '·' }

const LABELS: Record<string, string> = {
  WORKING:'Подключено',SCAN_QR_CODE:'Нужно подключение по QR',STOPPED:'Остановлено',STARTING:'Подключение',FAILED:'Ошибка подключения',
  open:'Открыт',ready:'Готов',cancelled:'Отменено',paused:'Приостановлено',partial:'Частично выполнено',blocked:'Заблокировано',
  analysis_error:'Ошибка анализа',delivery_problem:'Проблема доставки',crm_problem:'Проблема CRM',clarification_awaiting:'Ожидает ответа',clarification_expired:'Уточнение истекло',clarification_unconfirmed:'Ответ не подтверждён',
  received: 'Получено', queued: 'В очереди', analyzing: 'Анализируется', need_context: 'Нужен контекст',
  analyzed: 'Проанализировано', no_crm_actions: 'Без CRM-действий', error: 'Ошибка',
  needs_review: 'Требует проверки', analysis_needs_review: 'Нужна проверка данных',
  not_applicable: 'Не требуется', not_required: 'Не требуется', preparing: 'Готовится',
  sending: 'Отправляется', awaiting: 'Ожидает ответа', answered: 'Ответ получен',
  processing: 'Обработка', unconfirmed: 'Не подтверждено', resolved: 'Разрешено',
  expired: 'Истекло', superseded: 'Заменено', sent: 'Отправлено провайдеру',
  delivered: 'Доставлено', failed: 'Ошибка', unknown: 'Результат неизвестен',
  unavailable: 'Результат неизвестен', accepted: 'Запрос принят', reading: 'Проверяется чтением',
  confirmed: 'Подтверждено', done: 'Выполнено', completed: 'Завершено', success: 'Успешно',
  pending: 'Ожидает', stale: 'Зависло', disabled: 'Отключено', enqueued: 'В очереди',
}

export function statusLabel(state: string): string {
  return LABELS[state] || state
}

export const StatusBadge = defineComponent({
  name: 'StatusBadge',
  props: { state: { type: String, required: true }, extra: String },
  setup(props) {
    return () => {
      const tone = TONE_BY_STATE[props.state] || 'muted'
      return h('span', { class: `mx-chip mx-chip-${tone}`, role: 'status' }, [
        h('span', { 'aria-hidden': 'true' }, MARK[tone]),
        h('span', {}, statusLabel(props.state) + (props.extra ? ` · ${props.extra}` : '')),
      ])
    }
  },
})

export const EmptyState = defineComponent({
  name: 'EmptyState',
  props: { title: String, text: String },
  setup(props) {
    return () => h('div', { class: 'flex flex-col items-center justify-center gap-2 py-16 text-center' }, [
      h('div', { class: 'text-[15px]' }, props.title || 'Пока пусто'),
      props.text ? h('div', { class: 'mx-muted max-w-[320px]' }, props.text) : null,
    ])
  },
})

export const ErrorState = defineComponent({
  name: 'ErrorState',
  props: { text: String, onRetry: { type: Function as PropType<() => void>, required: true } },
  setup(props) {
    return () => h('div', { class: 'flex flex-col items-center justify-center gap-3 py-14 text-center', role: 'alert' }, [
      h('div', { class: 'mx-chip mx-chip-err' }, ['✕', h('span', {}, 'Не удалось загрузить данные')]),
      props.text ? h('div', { class: 'mx-muted max-w-[380px]' }, props.text) : null,
      h('button', { class: 'mx-btn', onClick: props.onRetry }, 'Повторить'),
    ])
  },
})

export const Skeleton = defineComponent({
  name: 'Skeleton',
  props: { rows: { type: Number, default: 3 }, height: { type: String, default: '16px' } },
  setup(props) {
    return () => h('div', { class: 'flex flex-col gap-3 p-4', 'aria-busy': 'true', 'aria-label': 'Загрузка' },
      Array.from({ length: props.rows }, (_, i) =>
        h('div', { class: 'mx-skeleton', style: { height: props.height, width: `${72 + ((i * 13) % 26)}%` } })))
  },
})

export const formatTime = (iso: string | null | undefined): string => {
  if (!iso) return 'Время неизвестно'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return 'Время неизвестно'
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  return sameDay
    ? date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' }) + ' ' +
      date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
}
