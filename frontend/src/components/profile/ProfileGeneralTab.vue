<template><form class="space-y-5" @submit.prevent="save"><h3 class="font-semibold">Профиль и контакты</h3><label class="block">ФИО<input class="field w-full mt-1" v-model="name" required /></label><label class="block">Email<input class="field w-full mt-1" v-model="email" type="email" /></label><p>Телефон для входа: {{ profile.phone }}</p><p class="text-sm text-slate-400">{{ profile.role }} · {{ profile.department }}</p><button class="btn-primary" :disabled="isSaving">Сохранить изменения</button><p role="status">{{ saveMessage }}</p></form></template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import { useProfile } from '../../composables/useProfile'
const { profile, isSaving, saveMessage, updateProfile } = useProfile()
const name = ref(profile.value.full_name), email = ref(profile.value.email)
watch(profile, value => { name.value = value.full_name; email.value = value.email })
async function save() { await updateProfile({ full_name: name.value, email: email.value }) }
</script>
