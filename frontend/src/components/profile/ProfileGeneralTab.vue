<template>
  <div class="space-y-6">
    <div>
      <h3 class="text-base font-semibold text-white">Основная информация</h3>
      <p class="text-xs text-slate-400 mt-0.5">Данные профиля сотрудника и корпоративные контакты</p>
    </div>

    <!-- Avatar Picker Section -->
    <div class="flex flex-col sm:flex-row sm:items-center gap-4 p-4 rounded-2xl bg-[#0e1633]/60 border border-[#2d3a63]/40">
      <img
        :src="form.avatar_url"
        alt="Avatar"
        class="w-16 h-16 rounded-2xl object-cover ring-2 ring-indigo-500/50 shadow-md shrink-0"
      />
      <div class="space-y-1.5 flex-1">
        <div class="text-xs font-medium text-slate-300">Выберите или укажите аватар</div>
        <div class="flex items-center gap-2">
          <button
            v-for="(preset, i) in avatarPresets"
            :key="i"
            type="button"
            @click="form.avatar_url = preset"
            class="w-8 h-8 rounded-xl overflow-hidden ring-1 transition-all cursor-pointer hover:scale-105"
            :class="form.avatar_url === preset ? 'ring-2 ring-indigo-400 scale-105' : 'ring-slate-700/60 opacity-60 hover:opacity-100'"
          >
            <img :src="preset" class="w-full h-full object-cover" />
          </button>
        </div>
      </div>
    </div>

    <!-- Form Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">ФИО</label>
        <div class="relative">
          <input
            v-model="form.full_name"
            type="text"
            required
            class="w-full px-3.5 py-2.5 rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white text-sm focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all"
          />
        </div>
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">Должность</label>
        <input
          v-model="form.role"
          type="text"
          required
          class="w-full px-3.5 py-2.5 rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white text-sm focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all"
        />
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">Отдел / Департамент</label>
        <input
          v-model="form.department"
          type="text"
          class="w-full px-3.5 py-2.5 rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white text-sm focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all"
        />
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">Корпоративный E-mail</label>
        <input
          v-model="form.email"
          type="email"
          class="w-full px-3.5 py-2.5 rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white text-sm focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all"
        />
      </div>

      <div class="md:col-span-2">
        <label class="block text-xs font-medium text-slate-300 mb-1.5">Рабочий номер телефона (для входа и WhatsApp)</label>
        <input
          v-model="form.phone"
          type="tel"
          class="w-full px-3.5 py-2.5 rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white text-sm focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all font-mono"
        />
      </div>
    </div>

    <!-- Submit Action -->
    <div class="flex items-center justify-end gap-3 pt-3 border-t border-slate-700/40">
      <span v-if="saveMessage" class="text-xs text-emerald-400 font-medium">
        {{ saveMessage }}
      </span>
      <button
        type="button"
        @click="handleSave"
        :disabled="isSaving"
        class="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium shadow-[0_0_15px_rgba(99,102,241,0.35)] transition-all cursor-pointer disabled:opacity-50"
      >
        {{ isSaving ? 'Сохранение...' : 'Сохранить изменения' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'
import { useProfile } from '../../composables/useProfile'

const { profile, isSaving, saveMessage, updateProfile } = useProfile()

const avatarPresets = [
  'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=250&q=80',
  'https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80',
  'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?auto=format&fit=crop&w=250&q=80',
  'https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?auto=format&fit=crop&w=250&q=80'
]

const form = reactive({
  full_name: profile.value.full_name,
  role: profile.value.role,
  department: profile.value.department,
  email: profile.value.email,
  phone: profile.value.phone,
  avatar_url: profile.value.avatar_url
})

watch(() => profile.value, (newVal) => {
  form.full_name = newVal.full_name
  form.role = newVal.role
  form.department = newVal.department
  form.email = newVal.email
  form.phone = newVal.phone
  form.avatar_url = newVal.avatar_url
}, { deep: true })

async function handleSave() {
  await updateProfile(form)
}
</script>
