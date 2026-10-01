<template>
  <section class="border border-slate-700 rounded-xl p-4 space-y-3">
    <button
      class="text-indigo-300 font-medium"
      :aria-expanded="opened"
      @click="toggle"
    >
      {{ opened ? "Скрыть" : "Показать" }} переписку и контекст анализа
    </button>
    <template v-if="opened">
      <p v-if="error" role="alert" class="text-rose-300">
        {{ error }} <button class="underline" @click="load()">Повторить</button>
      </p>
      <p v-if="loading" role="status">Загрузка переписки…</p>
      <template v-if="context">
        <h3 class="font-semibold">
          {{ context.chat_name || "Первоисточник" }}
        </h3>
        <p class="text-sm text-amber-200">{{ context.coverage }}</p>
        <div class="flex gap-2">
          <button
            class="btn"
            :class="mode === 'chat' ? 'border-indigo-400' : ''"
            @click="mode = 'chat'"
          >
            Переписка вокруг факта</button
          ><button
            class="btn"
            :class="mode === 'ai' ? 'border-indigo-400' : ''"
            @click="mode = 'ai'"
          >
            Что видел AI
          </button>
        </div>
        <p class="text-xs text-slate-400">
          {{
            mode === "chat"
              ? "Это окружающие сообщения. Отметка «Использовано AI» показывает участие в анализе."
              : "Здесь показан сохранённый текст, переданный AI. Частичные сообщения отмечены отдельно."
          }}
        </p>
        <p class="text-xs text-slate-400">Автор и дата показаны по данным сообщения в системе. Пересланный или импортированный текст может содержать собственную дату и подпись.</p>
        <button
          v-if="mode === 'chat' && context.before"
          class="btn"
          :disabled="loading"
          @click="load('before', context.before)"
        >
          Загрузить более ранние сообщения
        </button>
        <article
          v-for="message in displayedMessages"
          :key="message.id"
          class="rounded-lg p-3 whitespace-pre-wrap text-sm border"
          :class="
            message.is_source
              ? 'bg-indigo-950/60 border-indigo-400'
              : 'border-slate-700 bg-slate-900/60'
          "
        >
          <div class="flex flex-wrap gap-2 text-xs text-slate-400 mb-2">
            <strong class="text-slate-200">{{ message.sender_name }}</strong
            ><span>{{
              message.sent_at
                ? dateLabel(message.sent_at)
                : `Время отправки неизвестно · получено ${dateLabel(message.received_at)}`
            }}</span
            ><span v-if="message.is_source" class="text-indigo-200"
              >Исходное сообщение</span
            ><span v-else-if="message.is_source_revision" class="text-amber-300"
              >Другая версия исходного сообщения</span
            ><span v-else-if="message.used_by_ai" class="text-sky-300"
              >Использовано AI</span
            ><span v-if="message.partial" class="text-amber-300"
              >AI получил только часть текста</span
            >
          </div>
          <p>{{ message.content }}</p>
        </article>
        <p v-if="!displayedMessages.length" class="text-slate-400 text-sm">
          {{
            mode === "ai"
              ? "Сохранённых доступных сообщений истории нет."
              : "Доступной переписки нет."
          }}
        </p>
        <button
          v-if="mode === 'chat' && context.after"
          class="btn"
          :disabled="loading"
          @click="load('after', context.after)"
        >
          Загрузить более поздние сообщения
        </button>
        <button
          v-if="mode === 'ai' && context.ai_next_page"
          class="btn"
          :disabled="loading"
          @click="loadAi"
        >
          Загрузить ещё контекст AI
        </button>
        <p
          v-if="!context.history_available && context.source"
          class="text-slate-400 text-sm"
        >
          Для этого источника нет связанной истории чата.
        </p>
      </template>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../../composables/api";
import type {
  CandidateContext,
  ConversationMessage,
} from "../../types/factReview";
import { dateLabel } from "./presentation";
const props = defineProps<{ candidateId: number }>();
const opened = ref(false),
  loading = ref(false),
  error = ref(""),
  mode = ref<"chat" | "ai">("chat"),
  context = ref<CandidateContext | null>(null);
const displayedMessages = computed(() =>
  mode.value === "chat"
    ? context.value?.messages || []
    : [
        ...(context.value?.ai_messages || []),
        ...(context.value?.source ? [context.value.source] : []),
      ],
);
function mergeMessages(a: ConversationMessage[], b: ConversationMessage[]) {
  const seen = new Set<number>();
  return [...a, ...b].filter((m) => {
    if (seen.has(m.id)) return false;
    seen.add(m.id);
    return true;
  });
}
async function toggle() {
  opened.value = !opened.value;
  if (opened.value && !context.value) await load();
}
async function load(
  direction: "around" | "before" | "after" = "around",
  anchor?: number,
) {
  if (loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const next = await api<CandidateContext>(
      `/candidates/${props.candidateId}/context/?direction=${direction}${anchor ? `&anchor=${anchor}` : ""}`,
    );
    if (context.value && direction !== "around") {
      context.value.messages =
        direction === "before"
          ? mergeMessages(next.messages, context.value.messages)
          : mergeMessages(context.value.messages, next.messages);
      if (direction === "before") context.value.before = next.before;
      else context.value.after = next.after;
    } else context.value = next;
  } catch (e) {
    error.value =
      e instanceof Error ? e.message : "Не удалось загрузить переписку.";
  } finally {
    loading.value = false;
  }
}
async function loadAi() {
  if (!context.value?.ai_next_page || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const next = await api<CandidateContext>(
      `/candidates/${props.candidateId}/context/?ai_page=${context.value.ai_next_page}`,
    );
    context.value.ai_messages = mergeMessages(
      context.value.ai_messages,
      next.ai_messages,
    );
    context.value.ai_next_page = next.ai_next_page;
  } catch (e) {
    error.value =
      e instanceof Error ? e.message : "Не удалось загрузить контекст AI.";
  } finally {
    loading.value = false;
  }
}
</script>
