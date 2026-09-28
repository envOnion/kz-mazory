<template><form @submit.prevent="save" class="space-y-5"><h3 class="font-semibold">Уведомления и тихие часы</h3><label class="block">Часовой пояс<select class="field ml-2" v-model="zone"><option>Asia/Almaty</option><option>Asia/Astana</option><option>Europe/Moscow</option><option>Europe/Minsk</option><option>UTC</option></select></label><label class="block"><input type="checkbox" v-model="prefs.whatsapp" /> Отправлять в WhatsApp</label><label class="block"><input type="checkbox" v-model="prefs.reminder" /> Напоминания по обязательствам</label><label class="block"><input type="checkbox" v-model="prefs.daily_digest" /> Ежедневная сводка</label><div class="flex flex-wrap gap-3"><label>Тихие часы с <input class="field" type="time" v-model="prefs.quiet_start" required /></label><label>до <input class="field" type="time" v-model="prefs.quiet_end" required /></label></div><label class="block">Время сводки <input class="field" type="time" v-model="prefs.digest_time" required /></label><p class="text-xs text-slate-400">В тихие часы сообщения ждут следующего разрешённого времени. Одинаковое время начала и конца отключает тихие часы.</p><button class="btn-primary" :disabled="isSaving">Сохранить настройки</button><p role="status">{{ saveMessage }}</p></form></template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import { useProfile } from '../../composables/useProfile'
import type { NotificationPreferences } from '../../types/platform'
const { profile, isSaving, saveMessage, updateProfile } = useProfile()
const zone = ref(profile.value.timezone)
const prefs = ref<NotificationPreferences>({ quiet_start: '21:00', quiet_end: '09:00', digest_time: '09:00', whatsapp: true, reminder: true, daily_digest: true, ...profile.value.notification_preferences })
watch(profile, p => { zone.value = p.timezone; prefs.value = { ...prefs.value, ...p.notification_preferences } })
async function save() { await updateProfile({ timezone: zone.value, notification_preferences: prefs.value }) }
</script>
