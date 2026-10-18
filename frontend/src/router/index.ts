import { createRouter, createWebHistory } from 'vue-router'
import { currentUser, refreshSession } from '../composables/session'

const AUTH_PATHS = ['/messenger', '/analytics', '/threads', '/queue', '/crm-changes', '/settings', '/profile', '/client']
const STAFF_ONLY = ['/messenger', '/analytics', '/threads', '/queue', '/crm-changes', '/settings']

export function defaultPath(): string {
  return currentUser.value?.roles.includes('client') ? '/client' : '/messenger'
}

export function isClient(): boolean {
  return currentUser.value?.roles.includes('client') === true
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: () => import('../views/HomeView.vue') },
    { path: '/messenger', name: 'messenger', component: () => import('../views/MessengerView.vue') },
    { path: '/analytics', name: 'analytics', component: () => import('../views/AnalyticsView.vue') },
    { path: '/threads', name: 'threads', component: () => import('../views/ThreadsView.vue') },
    { path: '/queue', name: 'queue', component: () => import('../views/QueueView.vue') },
    { path: '/crm-changes', name: 'crm-changes', component: () => import('../views/CrmChangesView.vue') },
    { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue') },
    { path: '/profile', name: 'profile', component: () => import('../views/ProfileRoute.vue') },
    { path: '/client', name: 'client', component: () => import('../views/ClientPortalView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

let booted = false
router.beforeEach(async (to) => {
  if (!booted) {
    booted = true
    try { await refreshSession() } catch { /* остальная обработка — в guard ниже */ }
  }
  const user = currentUser.value
  if (!user) return AUTH_PATHS.some(p => to.path.startsWith(p)) ? { path: '/' } : true
  if (isClient()) {
    if (to.path === '/') return { path: '/client' }
    if (STAFF_ONLY.some(p => to.path.startsWith(p))) return { path: '/client' }
    return true
  }
  if (to.path === '/client') return { path: '/messenger' }
  if (to.path === '/') return { path: '/messenger' }
  return true
})

export default router
