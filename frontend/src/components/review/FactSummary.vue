<template>
  <section class="space-y-3">
    <div class="flex justify-between gap-3 flex-wrap">
      <h3 class="font-semibold text-lg">{{ factLabels[item.fact_type] }}</h3>
      <span class="text-sm text-slate-400">{{
        statusLabels[item.status]
      }}</span>
    </div>
    <p v-if="item.source_metadata" class="text-xs text-slate-400">
      Исходная отправка: {{ dateLabel(item.source_metadata.sent_at)
      }}<template v-if="item.source_metadata.time_basis === 'export_header'">
        · дата из экспорта; получено системой
        {{ dateLabel(item.source_metadata.received_at) }}</template
      >
    </p>
    <p class="text-sm text-indigo-200">{{ effectLabel(item) }}</p>
    <p v-if="compare" class="text-xs text-slate-400">
      Название объекта и компания используются для сопоставления. Подтверждение
      не переименовывает существующий проект и не меняет его компанию.
    </p>
    <div class="overflow-auto">
      <table class="w-full text-sm text-left">
        <thead>
          <tr class="text-slate-400 border-b border-slate-700">
            <th class="py-2 pr-3">Что проверяем</th>
            <th v-if="compare" class="py-2 pr-3">Сейчас в проекте</th>
            <th class="py-2">
              {{ compare ? "Предлагается" : "Найдено в сообщении" }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="field in shownFields"
            :key="field.key"
            class="border-b border-slate-800"
          >
            <th class="py-2 pr-3 font-normal text-slate-300">
              {{ field.label }}
            </th>
            <td
              v-if="compare"
              class="py-2 pr-3 text-slate-400 whitespace-pre-wrap"
            >
              {{
                valueLabel(
                  field,
                  item.current_values?.[field.key],
                  currentCurrency,
                )
              }}
            </td>
            <td
              class="py-2 whitespace-pre-wrap break-words"
              :class="
                compare && different(field.key)
                  ? 'text-amber-200'
                  : 'text-slate-100'
              "
            >
              {{
                valueLabel(
                  field,
                  item.proposed_changes[field.key],
                  proposedCurrency,
                  item.proposed_changes.deadline_precision,
                )
              }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p
      v-if="
        item.fact_type === 'commitment' &&
        item.proposed_changes.commitment_status !== 'fulfilled' &&
        typeof item.proposed_changes.deadline_at === 'string' &&
        Date.parse(item.proposed_changes.deadline_at) < Date.now()
      "
      class="text-sm text-amber-300"
    >
      Срок уже прошёл; выполнение по переписке пока не подтверждено.
    </p>
    <p class="text-xs text-slate-400">
      Оценка AI: {{ Math.round(item.confidence * 100) }}%. Проверяйте значения
      по переписке, даже при высокой оценке.
    </p>
    <p
      v-for="uncertainty in readableUncertainties"
      :key="uncertainty"
      class="text-sm text-amber-300"
    >
      {{ uncertainty }}
    </p>
    <details class="text-xs text-slate-500">
      <summary class="cursor-pointer">Сведения о проверке</summary>
      <p class="mt-2">
        Предложение №{{ item.id }} · {{ dateLabel(item.created_at) }}. Версия
        проекта при анализе: {{ item.base_version }}. Текущая:
        {{ item.current_version }}.
      </p>
      <p v-for="text in technicalUncertainties" :key="text">
        Пояснение AI: {{ uncertaintyLabel(text) }}
      </p>
    </details>
  </section>
</template>
<script setup lang="ts">
import { computed } from "vue";
import type { Candidate } from "../../types/platform";
import {
  fields,
  factLabels,
  statusLabels,
  effectLabel,
  valueLabel,
  dateLabel,
  uncertaintyLabel,
} from "./presentation";
const props = defineProps<{ item: Candidate }>();
const compare = computed(
  () =>
    props.item.fact_type === "project" &&
    Object.keys(props.item.current_values || {}).length > 0,
);
const proposedCurrency = computed(() =>
  typeof props.item.proposed_changes.currency === "string"
    ? props.item.proposed_changes.currency
    : typeof props.item.current_values?.currency === "string"
      ? props.item.current_values.currency
      : "валюта не указана",
);
const currentCurrency = computed(() =>
  typeof props.item.current_values?.currency === "string"
    ? props.item.current_values.currency
    : "",
);
const shownFields = computed(() =>
  fields[props.item.fact_type].filter(
    (f) =>
      f.key in props.item.proposed_changes &&
      (f.key !== "reverses_id" ||
        props.item.proposed_changes.payment_kind === "reversal"),
  ),
);
const readableUncertainties = computed(() => [
  ...new Set(props.item.uncertainties.map(uncertaintyLabel)),
]);
const technicalUncertainties = computed(() =>
  props.item.uncertainties.filter((t) => !/[а-яё]/i.test(t)),
);
function different(key: string) {
  return (
    String(props.item.current_values?.[key] ?? "") !==
    String(props.item.proposed_changes[key] ?? "")
  );
}
</script>
