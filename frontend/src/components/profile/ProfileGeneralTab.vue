<template>
  <form class="space-y-6" @submit.prevent="save" :aria-busy="isSaving || isLoading">
    <h3 class="font-semibold">Профиль и контакты</h3>
    <fieldset class="space-y-5 min-w-0" :disabled="isSaving || isLoading">
      <div>
        <h4 class="text-sm font-medium mb-3">Фото профиля</h4>
        <div class="flex flex-col sm:flex-row sm:items-center gap-4">
          <img v-if="photo" :src="photo" alt="Предпросмотр фото профиля" class="w-24 h-24 rounded-3xl object-cover ring-2 ring-indigo-400/50 shrink-0" />
          <div v-else class="w-24 h-24 rounded-3xl bg-indigo-500/15 border border-indigo-400/30 flex items-center justify-center text-2xl font-semibold text-indigo-200 shrink-0" role="img" aria-label="Фото профиля не добавлено">
            {{ profileInitials(name) }}
          </div>
          <div class="space-y-2 min-w-0">
            <div class="flex flex-wrap gap-2">
              <button type="button" class="btn inline-flex items-center gap-2" @click="fileInput?.click()">
                <Camera class="w-4 h-4" aria-hidden="true" />
                {{ photo ? 'Изменить фото' : 'Добавить фото' }}
              </button>
              <button v-if="photo" type="button" class="btn text-rose-300" @click="removePhoto">Удалить фото</button>
              <button v-if="selectedFile || removeAvatar || avatarError || isReading" type="button" class="btn" @click="resetPhoto">Отменить выбор</button>
            </div>
            <input ref="fileInput" type="file" class="sr-only" tabindex="-1" aria-label="Выбрать фото профиля" aria-describedby="avatar-help" accept="image/jpeg,image/png,image/webp" @change="choosePhoto" />
            <p id="avatar-help" class="text-xs text-slate-400">JPEG, PNG или WebP · до 5 МиБ и 20 Мп. Фото обрежется по центру до квадрата.</p>
            <p v-if="isReading" class="text-xs text-slate-300" role="status">Подготовка фото…</p>
            <p v-else-if="selectedFile" class="text-xs text-indigo-300">Новое фото появится после сохранения.</p>
            <p v-else-if="removeAvatar" class="text-xs text-indigo-300">Фото будет удалено после сохранения.</p>
          </div>
        </div>
        <p v-if="avatarError" class="text-sm text-rose-300 mt-3" role="alert">{{ avatarError }}</p>
      </div>
      <label class="block">ФИО<input class="field w-full mt-1" v-model="name" @input="nameEdited = true" required /></label>
      <label class="block">Email<input class="field w-full mt-1" v-model="email" @input="emailEdited = true" type="email" /></label>
      <p>Телефон для входа: {{ profile.phone }}</p>
      <p v-if="profile.role || profile.department" class="text-sm text-slate-400">{{ [profile.role, profile.department].filter(Boolean).join(' · ') }}</p>
      <button class="btn-primary" :disabled="isReading || !!avatarError">{{ isSaving ? 'Сохранение…' : 'Сохранить изменения' }}</button>
    </fieldset>
    <p v-if="isLoading" class="text-sm text-slate-400" role="status">Загрузка профиля…</p>
    <p role="status" class="text-sm">{{ saveMessage }}</p>
  </form>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { Camera } from 'lucide-vue-next'
import { useProfile } from '../../composables/useProfile'
import { profileInitials } from '../../composables/profileInitials'
import type { ProfileUpdate } from '../../types/platform'

const { profile, isLoading, isSaving, saveMessage, updateProfile } = useProfile()
const name = ref(profile.value.full_name), email = ref(profile.value.email)
const nameEdited = ref(false), emailEdited = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null), preview = ref('')
const removeAvatar = ref(false), isReading = ref(false), avatarError = ref('')
const photo = computed(() => removeAvatar.value ? '' : preview.value || profile.value.avatar_url)
let selection = 0

watch(profile, value => {
  if (!nameEdited.value) name.value = value.full_name
  if (!emailEdited.value) email.value = value.email
})
onUnmounted(() => { selection++ })

function resetPhoto() {
  selection++
  selectedFile.value = null
  preview.value = ''
  removeAvatar.value = false
  isReading.value = false
  avatarError.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

function removePhoto() {
  resetPhoto()
  removeAvatar.value = !!profile.value.avatar_url
}

async function choosePhoto(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  resetPhoto()
  saveMessage.value = ''
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    avatarError.value = 'Выберите фото в формате JPEG, PNG или WebP.'
    return
  }
  if (file.size > 5 * 1024 * 1024) {
    avatarError.value = 'Фото должно быть не больше 5 МиБ.'
    return
  }
  const current = selection
  isReading.value = true
  try {
    const url = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error())
      reader.onerror = () => reject(new Error())
      reader.readAsDataURL(file)
    })
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const candidate = new Image()
      candidate.onload = () => resolve(candidate)
      candidate.onerror = () => reject(new Error())
      candidate.src = url
    })
    if (current !== selection) return
    if (image.naturalWidth * image.naturalHeight > 20_000_000) {
      avatarError.value = 'Фото должно содержать не больше 20 миллионов пикселей.'
      return
    }
    selectedFile.value = file
    preview.value = url
  } catch {
    if (current === selection) avatarError.value = 'Не удалось прочитать фото. Выберите корректный JPEG, PNG или WebP.'
  } finally {
    if (current === selection) isReading.value = false
  }
}

async function save() {
  if (isLoading.value || isSaving.value || isReading.value || avatarError.value) return
  const data: ProfileUpdate = { full_name: name.value, email: email.value }
  if (selectedFile.value) data.avatar = selectedFile.value
  if (removeAvatar.value) data.remove_avatar = true
  if (await updateProfile(data)) {
    nameEdited.value = emailEdited.value = false
    name.value = profile.value.full_name
    email.value = profile.value.email
    resetPhoto()
  }
}
</script>
