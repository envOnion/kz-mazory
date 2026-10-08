<template><div class="overflow-x-auto max-w-full"><table class="data-table text-sm"><thead><tr><th v-for="column in selected" :key="column.name">{{ datasetLabel(dataset, column.name) }}{{ column.unit ? ` (${column.unit})` : '' }}</th></tr></thead><tbody><tr v-for="(row, index) in dataset.rows" :key="index"><td v-for="column in selected" :key="column.name">{{ display(row[column.name] ?? null, undefined, column.type) }}</td></tr></tbody></table></div></template>
<script setup lang="ts">
import { computed } from 'vue'
import type { AnalyticsDataset } from '../types/analytics'
import { display, datasetLabel } from './options'
const props = defineProps<{ dataset: AnalyticsDataset; columns?: string[] }>()
const selected = computed(() => props.columns ? props.dataset.columns.filter(c => props.columns?.includes(c.name)) : props.dataset.columns)
</script>
