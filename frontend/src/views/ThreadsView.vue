<script setup lang="ts">
import ThreadBrowser from '../components/review/ThreadBrowser.vue'
</script>
<template>
  <section class="mx-panel-main overflow-y-auto" aria-label="Треды">
    <header class="px-5 py-3 border-b border-[var(--mx-border)]"><h1 class="text-[23px] font-semibold">Треды</h1></header>
    <div class="p-4"><ThreadBrowser /></div>
  </section>
</template>
