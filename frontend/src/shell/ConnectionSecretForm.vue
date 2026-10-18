<script setup lang="ts">
import { ref } from 'vue'
import { post } from '../composables/api'
const props = defineProps<{endpoint:string;kind:string;label:string;configured:boolean;readonly?:boolean}>()
const emit = defineEmits<{saved:[]}>()
const value=ref(''), busy=ref(false), error=ref(''), confirmClear=ref(false), editing=ref(false)
async function save(clear=false) {
  busy.value=true; error.value=''
  try { await post(props.endpoint,{key:props.kind,...(clear ? {clear:true} : {value:value.value})}); value.value=''; editing.value=false; confirmClear.value=false; emit('saved') }
  catch(e) { error.value=e instanceof Error ? e.message : 'Не удалось изменить подключение' }
  finally { busy.value=false }
}
</script>
<template>
  <div class="border-t border-[var(--mx-border)] pt-3 space-y-2">
    <p>{{ label }}: <span class="mx-muted">{{ configured ? 'Настроено' : 'Не настроено' }}</span></p>
    <template v-if="!readonly"><button class="mx-btn" :disabled="busy" @click="editing=!editing; value=''; confirmClear=false">{{ editing ? 'Отменить' : 'Заменить' }}</button>
      <form v-if="editing" class="flex flex-wrap gap-2" @submit.prevent="save()"><label class="grow">Новое значение · {{ label }}<input v-model="value" class="mx-input" type="password" autocomplete="new-password" required :maxlength="kind === 'webhook_url' ? 255 : kind === 'inbound_token' ? 128 : 4096" /></label><button class="mx-btn mx-btn-primary self-end" :disabled="busy">Сохранить новое значение</button></form>
      <button v-if="configured" class="mx-btn ml-2" :disabled="busy" @click="confirmClear ? save(true) : confirmClear=true">{{ confirmClear ? 'Подтвердить удаление подключения' : 'Удалить подключение' }}</button>
    </template><p v-if="error" role="alert" class="text-rose-300">{{ error }}</p>
  </div>
</template>
