<script setup lang="ts">
import AttachmentPanel from '../shell/AttachmentPanel.vue'
import WorkspaceView from '../components/WorkspaceView.vue'
import { currentUser } from '../composables/session'

// Клиентский кабинет: только собственные проекты, задачи и уведомления.
// Внутренние чаты, trace и CRM-диагностика клиентам недоступны (серверные границы access.py).
</script>

<template>
  <section class="mx-panel-main overflow-y-auto" aria-label="Рабочий кабинет">
    <div class="max-w-6xl mx-auto p-4 pb-10">
      <h1 style="font-size:23px;font-weight:600" class="mb-1">Рабочий кабинет</h1>
      <p class="mx-muted mb-3">{{ currentUser?.name || currentUser?.phone }}</p>
      <RouterLink to="/profile" class="mx-link">Профиль и выход</RouterLink>
      <WorkspaceView :key="currentUser?.id" />
      <AttachmentPanel />
    </div>
  </section>
</template>
