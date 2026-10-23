<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import FinancePanel from '../shell/FinancePanel.vue'
import AttachmentPanel from '../shell/AttachmentPanel.vue'
import WorkspaceView from '../components/WorkspaceView.vue'
import AnalyticsOverview from '../shell/AnalyticsOverview.vue'

const route = useRoute()
const views = [{id:'operations',label:'Обзор'}, {id:'projects',label:'Проекты'}, {id:'finance',label:'Финансы'}, {id:'documents',label:'Вложения'}, {id:'review',label:'Факты и решения'}, {id:'tasks',label:'Обязательства'}, {id:'reports',label:'Отчёты'}, {id:'notifications',label:'Уведомления'}]
const view = computed(() => typeof route.query.view === 'string' && views.some(v => v.id === route.query.view) ? route.query.view : 'operations')
</script>

<template>
  <section class="mx-panel-main overflow-y-auto" aria-label="Аналитика">
    <header class="px-5 py-3 sticky top-0 z-10 shrink-0" style="border-bottom:1px solid var(--mx-border);background:var(--mx-main)">
      <h1 style="font-size:23px;font-weight:600">Аналитика</h1>
      <nav class="flex gap-1 flex-wrap mt-2" aria-label="Данные и аналитика"><RouterLink v-for="item in views" :key="item.id" :to="{path: '/analytics', query: {view:item.id}}" class="mx-tab" :class="view === item.id ? 'mx-tab-active' : ''">{{ item.label }}</RouterLink></nav>
    </header>
    <FinancePanel v-if="view === 'finance'" /><AttachmentPanel v-else-if="view === 'documents'" />
    <WorkspaceView v-else-if="view !== 'operations'" :initial-tab="view" />
    <AnalyticsOverview v-else />
  </section>
</template>
