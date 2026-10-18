<script setup lang="ts">
import { useRouter } from 'vue-router'
import { currentUser } from '../composables/session'
import { useAuth } from '../composables/useAuth'
import { defaultPath } from '../router'

// '/' — вход в продукт: авторизованного ведёт в раздел по роли,
// гостя оставляет на приветственном экране App.vue.
const router = useRouter()
const { isAuthModalOpen } = useAuth()
if (currentUser.value) void router.replace(defaultPath())
else isAuthModalOpen.value = true
</script>

<template>
  <div class="flex-1" aria-hidden="true" />
</template>
