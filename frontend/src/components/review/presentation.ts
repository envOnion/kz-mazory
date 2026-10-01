import type { ApiError, Candidate } from "../../types/platform";
import type { ReviewField, FactDraft, FactType } from "../../types/factReview";
export const roles: Record<string, string> = {
  manager: "Менеджер",
  team_lead: "Руководитель",
  finance: "Финансист",
  admin: "Администратор",
  client: "Клиент",
};
export const factLabels = {
  project: "Данные проекта",
  payment: "Платёж",
  commitment: "Обязательство",
};
export const statusLabels: Record<string, string> = {
  pending: "На проверке",
  approved: "Подтверждено",
  rejected: "Отклонено",
  superseded: "Заменено новым предложением",
};
const options = (values: Record<string, string>) =>
  Object.entries(values).map(([value, label]) => ({ value, label }));
export const stages = {
  lead: "Первичный контакт",
  qualification: "Сбор требований",
  design: "Проектирование",
  proposal_sent: "Коммерческое предложение отправлено",
  contract_signing: "Согласование договора",
  in_execution: "В исполнении",
  completed: "Завершён",
  stalled: "Требует внимания",
  lost: "Закрыт без сделки",
};
export const paymentKinds = {
  increment: "Отдельный полученный платёж",
  cumulative: "Накопленная сумма поступлений",
  promise: "Обещание оплаты",
  reversal: "Корректировка платежа",
};
const common: ReviewField[] = [
  { key: "object_name", label: "Объект / проект", type: "text" },
];
const currency: ReviewField = {
  key: "currency",
  label: "Валюта",
  type: "select",
  options: options({
    KZT: "Тенге (KZT)",
    USD: "Доллар (USD)",
    EUR: "Евро (EUR)",
    RUB: "Рубль (RUB)",
  }),
};
export const fields: Record<FactType, ReviewField[]> = {
  project: [
    ...common,
    { key: "company_name", label: "Компания", type: "text" },
    { key: "contract_amount", label: "Сумма договора", type: "money" },
    { key: "cost_amount", label: "Себестоимость", type: "money" },
    currency,
    {
      key: "stage",
      label: "Стадия проекта",
      type: "select",
      options: options(stages),
    },
    { key: "current_action", label: "Последнее действие", type: "textarea" },
    { key: "next_action", label: "Следующий шаг", type: "textarea" },
  ],
  payment: [
    ...common,
    { key: "amount", label: "Сумма", type: "money" },
    currency,
    { key: "payment_date", label: "Дата платежа", type: "date" },
    {
      key: "payment_kind",
      label: "Вид платежа",
      type: "select",
      options: options(paymentKinds),
    },
    {
      key: "reverses_id",
      label: "Исходный платёж для корректировки",
      type: "reference",
    },
  ],
  commitment: [
    ...common,
    { key: "commitment_text", label: "Обязательство", type: "textarea" },
    { key: "deadline_at", label: "Срок выполнения", type: "datetime-local" },
    {
      key: "deadline_precision",
      label: "Точность срока",
      type: "select",
      options: options({
        unknown: "Срок не указан",
        date: "Известен только день",
        datetime: "Указаны дата и время",
      }),
    },
  ],
};
export const fieldLabel = (key: string) =>
  Object.values(fields)
    .flat()
    .find((f) => f.key === key)?.label ||
  (
    {
      reason: "Причина",
      base_version: "Версия данных",
      project_id: "Проект",
      non_field_errors: "Проверка предложения",
      evidence: "Источник",
      confidence: "Уверенность",
      sender_phone: "Автор",
      uncertainties: "Неопределённости",
    } as Record<string, string>
  )[key] ||
  "Данные предложения";
export function dateLabel(value: string | null) {
  return value && !Number.isNaN(Date.parse(value))
    ? new Date(value).toLocaleString("ru-RU")
    : "Время не указано";
}
export function valueLabel(
  field: ReviewField,
  value: unknown,
  currencyCode = "",
): string {
  if (value === null || value === undefined || value === "")
    return "Не указано";
  if (field.options)
    return (
      field.options.find((o) => o.value === value)?.label ||
      "Значение требует уточнения"
    );
  if (field.type === "money") {
    const amount =
      typeof value === "number"
        ? value
        : typeof value === "string"
          ? Number.parseFloat(value)
          : NaN;
    return Number.isFinite(amount)
      ? `${amount.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ${currencyCode}`.trim()
      : "Сумма требует уточнения";
  }
  if (field.type === "date")
    return new Date(
      `${String(value).slice(0, 10)}T12:00:00`,
    ).toLocaleDateString("ru-RU");
  if (field.type === "datetime-local") return dateLabel(String(value));
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : "Значение требует уточнения";
}
export function effectLabel(item: Candidate) {
  if (item.fact_type === "project")
    return item.project_id
      ? "После подтверждения будут обновлены указанные данные проекта."
      : "После подтверждения будет создан проект. Проверьте название и связь со сделкой.";
  if (item.fact_type === "commitment")
    return "После подтверждения будет записано обязательство с указанным сроком.";
  return (
    (
      {
        promise:
          "Это обещание оплаты. Его нельзя подтвердить как полученный платёж. Для фиксации обещания нужен факт обязательства.",
        cumulative:
          "Это накопленный итог. Его нельзя подтвердить как новый платёж на всю указанную сумму.",
        reversal:
          "После подтверждения будет записана корректировка исходного платежа.",
        increment:
          "После подтверждения будет записан отдельный полученный платёж.",
      } as Record<string, string>
    )[String(item.proposed_changes.payment_kind || "increment")] ||
    "Проверьте вид платежа перед подтверждением."
  );
}
export const crmReasons: Record<string, string> = {
  company_exact: "Совпадает компания",
  title_exact: "Совпадает название сделки",
  title_partial: "Название сделки похоже",
  object_field_exact: "Совпадает объект",
  object_field_partial: "Название объекта похоже",
  existing_project_bitrix_id: "У проекта уже есть связь с этой сделкой",
};
export const crmErrors: Record<string, string> = {
  crm_match_deadline:
    "Поиск сделки занял слишком много времени. Повторите проверку позже.",
  crm_pagination_limit:
    "Список сделок слишком большой для одной проверки. Уточните объект или компанию.",
  crm_project_scope_conflict:
    "Найденная сделка относится к другой команде. Обратитесь к руководителю.",
  crm_match_stale:
    "Данные предложения изменились. Требуется новая проверка сделки.",
  crm_disabled: "Сопоставление со сделками отключено администратором.",
  crm_rejected:
    "CRM отклонила запрос. Попросите администратора проверить подключение.",
  crm_invalid_response:
    "Не удалось прочитать ответ CRM. Повторите проверку позже.",
  provider_timeout: "CRM не ответила вовремя. Повторите проверку позже.",
  crm_object_field_invalid:
    "Не настроено поле объекта в CRM. Обратитесь к администратору.",
  crm_match_request_limit:
    "Сохранён старый результат проверки с ограничением запросов. Запустите новую обработку сообщения.",
};
export function uncertaintyLabel(text: string) {
  return /[а-яё]/i.test(text)
    ? text
    : "В извлечённых данных есть неопределённость. Сверьте значения с перепиской перед подтверждением.";
}
export function friendlyApiError(error: ApiError, status: number): string {
  const validation: string[] = [];
  function collect(value: unknown, key: string) {
    if (typeof value === "string")
      validation.push(
        `${fieldLabel(key)}: ${/[а-яё]/i.test(value) ? value : /date|datetime/i.test(value) ? "укажите корректную дату." : /decimal|number|digits/i.test(value) ? "проверьте число и количество знаков после запятой." : /required|blank|null/i.test(value) ? "заполните обязательное поле." : "проверьте выбранное значение."}`,
      );
    else if (Array.isArray(value)) value.forEach((v) => collect(v, key));
    else if (value && typeof value === "object")
      Object.entries(value).forEach(([k, v]) => collect(v, k));
  }
  if (error.fields) collect(error.fields, "non_field_errors");
  if (validation.length) return validation.join(" ");
  if (status === 409)
    return `${error.error && /[а-яё]/i.test(error.error) ? error.error : "Данные изменились после загрузки."} Обновите список и проверьте предложение заново.`;
  if (error.error && /[а-яё]/i.test(error.error)) return error.error;
  if (status === 403)
    return "Недостаточно прав для этого действия. Обратитесь к руководителю.";
  if (status === 404)
    return "Предложение или источник больше недоступны. Обновите список.";
  if (status === 429)
    return "Слишком много запросов. Подождите немного и повторите.";
  return "Не удалось выполнить действие. Повторите позже или обратитесь к администратору.";
}
export function draftFor(item: Candidate): FactDraft {
  const draft: FactDraft = {};
  for (const field of fields[item.fact_type]) {
    if (field.type === "reference") continue;
    const value = item.proposed_changes[field.key];
    if (
      field.type === "datetime-local" &&
      typeof value === "string" &&
      !Number.isNaN(Date.parse(value))
    ) {
      const date = new Date(value);
      draft[field.key] = new Date(
        date.getTime() - date.getTimezoneOffset() * 60000,
      )
        .toISOString()
        .slice(0, 16);
    } else
      draft[field.key] =
        value === null || value === undefined ? "" : String(value);
  }
  return draft;
}
export function changedValues(
  item: Candidate,
  draft: FactDraft,
): Record<string, unknown> {
  const before = draftFor(item),
    result: Record<string, unknown> = {};
  for (const field of fields[item.fact_type]) {
    const value = draft[field.key]?.trim() || "";
    if (value === before[field.key]) continue;
    if (field.type === "money") {
      if (!/^-?\d{1,12}([.,]\d{1,2})?$/.test(value))
        throw new Error(
          `${field.label}: укажите сумму с точностью до двух знаков.`,
        );
      result[field.key] = value.replace(",", ".");
    } else if (field.type === "reference") continue;
    else if (field.type === "datetime-local") {
      if (value && Number.isNaN(Date.parse(value)))
        throw new Error("Укажите корректный срок.");
      result[field.key] = value ? new Date(value).toISOString() : null;
    } else if (field.type === "date") {
      if (
        value &&
        (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(Date.parse(value)))
      )
        throw new Error("Укажите корректную дату платежа.");
      result[field.key] = value || null;
    } else if (field.type === "select") {
      if (!field.options?.some((o) => o.value === value))
        throw new Error(`${field.label}: выберите значение.`);
      result[field.key] = value;
    } else result[field.key] = value;
  }
  return result;
}
