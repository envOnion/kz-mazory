<script setup lang="ts">
import { useRouter } from 'vue-router'
import UserProfileView from '../components/UserProfileView.vue'
import { defaultPath } from '../router'

// Профиль существующего сотрудника — в новой оболочке без возврата в старую админку.
const router = useRouter()
function afterLogout() { void router.replace('/') }
function back() { void router.replace(defaultPath()) }
</script>

<template>
  <section class="mx-panel-main overflow-y-auto" aria-label="Профиль">
    <div class="max-w-4xl mx-auto p-4 pb-10">
      <a class="mx-link block mb-4" href="/admin/security/">Настройка MFA для административного доступа →</a>
      <UserProfileView :back-to-chat-label="'Назад к работе'" @back-to-chat="back" @logged-out="afterLogout" />
    </div>
  </section>
</template>
