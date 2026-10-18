<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessageSquare, BarChart2, GitBranch, ListChecks, ArrowLeftRight, Settings, User, LogOut } from 'lucide-vue-next'
import { currentUser, logoutSession } from '../composables/session'
import ShellNotifications from './ShellNotifications.vue'
import SectionSearch from './SectionSearch.vue'
import MazoryLogo from '../components/icons/MazoryLogo.vue'
import { useProfile } from '../composables/useProfile'

// Узкая навигационная полоса: фиксированный порядок разделов §3.3,
// профиль и выход — внизу. Никаких модельных пунктов.
const props = defineProps<{ attention: number; attentionStale?:boolean }>()
const route = useRoute()
const router = useRouter()

const SECTIONS = [
  { path: '/messenger', label: 'Чаты', icon: MessageSquare },
  { path: '/analytics', label: 'Аналитика', icon: BarChart2 },
  { path: '/threads', label: 'Треды', icon: GitBranch },
  { path: '/queue', label: 'Очередь', icon: ListChecks },
  { path: '/crm-changes', label: 'Изменения CRM', icon: ArrowLeftRight },
  { path: '/settings', label: 'Настройки', icon: Settings },
]

const active = computed(() => route.path)
const me = computed(() => currentUser.value)
const loggingOut = ref(false)
const { profile, fetchProfile } = useProfile()
onMounted(fetchProfile)

async function signOut() {
  loggingOut.value = true
  await logoutSession()
  await router.replace('/')
}

// Возврат фокуса: после смены раздела фокус на основном контейнере.
watch(() => route.path, () => {
  document.querySelector<HTMLElement>('[data-main-region]')?.focus({ preventScroll: true })
})
</script>

<template>
  <nav class="flex flex-col items-center shrink-0" :style="{ width: 'var(--mx-rail-w)', background: 'var(--mx-rail)', borderRight: '1px solid var(--mx-border)' }" aria-label="Разделы">
    <div class="py-4 mx-accent font-semibold tracking-wide" title="Mazory" style="font-size:17px"><MazoryLogo :size="30" :glow="false" /></div>
    <div class="flex-1 flex flex-col gap-1 w-full items-center">
      <SectionSearch :sections="[...SECTIONS,{path:'/profile',label:'Профиль и безопасность'}]" />
      <RouterLink
        v-for="section in SECTIONS" :key="section.path" :to="section.path" :data-testid="section.path === '/analytics' ? 'nav-kpi-dashboard' : undefined"
        class="relative flex flex-col items-center gap-1 py-2.5 w-[calc(var(--mx-rail-w)-6px)] rounded-xl mx-muted"
        :class="active.startsWith(section.path) ? '!text-[var(--mx-text)] bg-[rgba(152,126,255,0.12)]' : 'hover:bg-[rgba(255,255,255,0.04)]'"
        :aria-current="active.startsWith(section.path) ? 'page' : undefined"
        :style="{ color: active.startsWith(section.path) ? 'var(--mx-text)' : 'var(--mx-muted)' }"
      >
        <component :is="section.icon" :size="19" />
        <span class="text-[10px] leading-tight text-center">{{ section.label }}</span>
        <span
          v-if="section.path === '/messenger' && (props.attention > 0 || props.attentionStale)"
          class="absolute top-1.5 right-2 min-w-[16px] h-4 px-1 rounded-full text-[10px] font-semibold flex items-center justify-center"
          style="background: var(--mx-accent); color: #12101d"
          :title="props.attentionStale ? 'Счётчик временно недоступен' : `Требует внимания: ${props.attention}`"
        >{{ props.attentionStale ? '?' : props.attention > 99 ? '99+' : props.attention }}</span>
      </RouterLink>
    </div>
    <div class="flex flex-col items-center gap-1 pb-4 w-full">
      <ShellNotifications />
      <RouterLink
        to="/profile" data-testid="nav-profile" class="flex flex-col items-center gap-1 py-2.5 w-[calc(var(--mx-rail-w)-6px)] rounded-xl"
        :style="{ color: active.startsWith('/profile') ? 'var(--mx-text)' : 'var(--mx-muted)' }"
      >
        <img v-if="profile.avatar_url" :src="profile.avatar_url" :alt="profile.full_name" data-testid="header-avatar" class="w-6 h-6 rounded-lg object-cover" />
        <User v-else :size="19" /><span class="text-[10px]">Профиль</span>
      </RouterLink>
      <button class="flex flex-col items-center gap-1 py-2.5 w-[calc(var(--mx-rail-w)-6px)] rounded-xl hover:bg-[rgba(255,255,255,0.04)]" :style="{ color: 'var(--mx-muted)' }" :disabled="loggingOut" title="Выйти" @click="signOut">
        <LogOut :size="19" /><span class="text-[10px]">Выход</span>
      </button>
      <div v-if="me" class="text-[10px] mx-muted text-center px-1 truncate w-full" :title="me.name || me.phone">{{ me.name || me.phone }}</div>
    </div>
  </nav>
</template>
