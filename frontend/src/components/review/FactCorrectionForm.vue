<template>
  <form
    class="rounded-xl border border-indigo-500 p-4 space-y-4"
    @submit.prevent="submit"
  >
    <h3 class="font-semibold">
      Исправить {{ factLabels[item.fact_type].toLowerCase() }}
    </h3>
    <p class="text-sm text-slate-400">
      Укажите проверенные значения. Исходная цитата сохранится вместе с причиной
      исправления.
    </p>
    <p class="text-xs text-slate-400">
      Сроки вводятся в UTC+6. Время «утром» по принятому правилу — 09:00.
    </p>
    <div class="grid sm:grid-cols-2 gap-4">
      <label
        v-for="field in visibleFields"
        :key="field.key"
        class="block text-sm"
        >{{ field.label }}
        <select
          v-if="field.type === 'select'"
          class="field w-full mt-1"
          v-model="draft[field.key]"
        >
          <option value="">Не указано</option>
          <option
            v-for="option in field.options"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </option>
        </select>
        <template v-else-if="field.type === 'reference'">
          <select class="field w-full mt-1" v-model="reversesId">
            <option :value="undefined">Выберите полученный платёж</option>
            <option
              v-for="payment in payments"
              :key="payment.id"
              :value="payment.id"
            >
              {{ payment.payment_date }} · {{ payment.amount }}
              {{ payment.currency }} · №{{ payment.id }}
            </option>
          </select>
          <button
            v-if="morePayments"
            type="button"
            class="btn mt-2"
            :disabled="loadingPayments"
            @click="loadPayments"
          >
            Показать ещё платежи
          </button>
          <p v-if="!item.project_id" class="text-amber-300 mt-1">
            Сначала сопоставьте предложение с проектом.
          </p>
        </template>
        <textarea
          v-else-if="field.type === 'textarea'"
          class="field w-full mt-1 min-h-24"
          v-model="draft[field.key]"
          maxlength="1000"
        />
        <input
          v-else
          class="field w-full mt-1"
          :type="
            field.type === 'datetime-local' &&
            draft.deadline_precision === 'date'
              ? 'date'
              : ['money', 'text'].includes(field.type)
                ? 'text'
                : field.type
          "
          :inputmode="field.type === 'money' ? 'decimal' : undefined"
          v-model="draft[field.key]"
          :maxlength="field.type === 'text' ? 255 : undefined"
        />
      </label>
    </div>
    <label class="block text-sm"
      >Причина исправления<textarea
        class="field w-full mt-1"
        v-model="reason"
        required
        maxlength="2000"
        placeholder="Что было извлечено неверно и чем подтверждаются изменения"
      />
    </label>
    <p v-if="error" role="alert" class="text-rose-300">{{ error }}</p>
    <div class="flex gap-2 flex-wrap">
      <button
        class="btn-primary"
        :disabled="busy || !reason.trim() || !canApprove"
        type="submit"
      >
        Сохранить исправления и подтвердить</button
      ><button
        class="btn"
        type="button"
        :disabled="busy"
        @click="$emit('cancel')"
      >
        Отменить исправления
      </button>
    </div>
    <p v-if="!canApprove" class="text-amber-300 text-sm">
      Для подтверждения требуется доступ к источнику и нужная роль.
    </p>
  </form>
</template>
<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../../composables/api";
import type { Candidate, Page, Payment } from "../../types/platform";
import { changedValues, draftFor, fields, factLabels } from "./presentation";
const props = defineProps<{
  item: Candidate;
  busy: boolean;
  canApprove: boolean;
  initialReason?: string;
}>();
const emit = defineEmits<{
  cancel: [];
  save: [changes: Record<string, unknown>, reason: string];
}>();
const draft = ref(draftFor(props.item)),
  reason = ref(props.initialReason || ""),
  error = ref("");
const initialReversal = props.item.proposed_changes.reverses_id;
const reversesId = ref<number | undefined>(
  typeof initialReversal === "number" ? initialReversal : undefined,
);
const payments = ref<Payment[]>([]),
  morePayments = ref(true),
  loadingPayments = ref(false),
  paymentPage = ref(1);
const visibleFields = computed(() =>
  fields[props.item.fact_type].filter(
    (f) => f.key !== "reverses_id" || draft.value.payment_kind === "reversal",
  ),
);
async function loadPayments() {
  if (!props.item.project_id || loadingPayments.value) return;
  loadingPayments.value = true;
  try {
    const page = await api<Page<Payment>>(
      `/candidates/${props.item.id}/payments/?page=${paymentPage.value}`,
    );
    payments.value.push(...page.results);
    morePayments.value = Boolean(page.next);
    paymentPage.value++;
  } catch (e) {
    error.value =
      e instanceof Error ? e.message : "Не удалось загрузить платежи.";
  } finally {
    loadingPayments.value = false;
  }
}
watch(
  () => draft.value.payment_kind,
  (kind) => {
    if (kind === "reversal" && !payments.value.length) void loadPayments();
  },
  { immediate: true },
);
watch(
  () => draft.value.deadline_precision,
  (precision, previous) => {
    if (precision === "date")
      draft.value.deadline_at = (draft.value.deadline_at || "").slice(0, 10);
    else if (precision === "unknown" || previous === "date")
      draft.value.deadline_at = "";
  },
);
function submit() {
  error.value = "";
  try {
    if (!props.canApprove || props.busy)
      throw new Error(
        "Подтверждение сейчас недоступно. Проверьте права и источник.",
      );
    const changes = changedValues(props.item, draft.value);
    if (draft.value.payment_kind === "reversal") {
      if (
        !reversesId.value ||
        !payments.value.some((payment) => payment.id === reversesId.value)
      )
        throw new Error("Выберите исходный платёж для корректировки.");
      if (reversesId.value !== props.item.proposed_changes.reverses_id)
        changes.reverses_id = reversesId.value;
    }
    if (
      props.item.fact_type === "payment" &&
      !["increment", "reversal"].includes(
        draft.value.payment_kind || "increment",
      )
    )
      throw new Error(
        "Обещание или накопленный итог нельзя подтвердить как полученный платёж.",
      );
    if (props.item.fact_type === "payment" && !draft.value.payment_date)
      throw new Error("Укажите подтверждённую дату платежа.");
    if (
      props.item.fact_type === "commitment" &&
      draft.value.deadline_precision !== "unknown" &&
      !draft.value.deadline_at
    )
      throw new Error("Укажите срок с выбранной точностью.");
    if (!reason.value.trim()) throw new Error("Укажите причину исправления.");
    emit("save", changes, reason.value.trim());
  } catch (e) {
    error.value =
      e instanceof Error ? e.message : "Проверьте введённые значения.";
  }
}
</script>
