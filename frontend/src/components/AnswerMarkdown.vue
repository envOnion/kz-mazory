<template><div class="answer-markdown" v-html="html" /></template>
<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
const props = defineProps<{ text: string }>()
const html = computed(() => DOMPurify.sanitize(marked.parse(props.text, { async: false, breaks: true }), {
  ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'a', 'h3', 'h4', 'table', 'thead', 'tbody', 'tr', 'th', 'td'],
  ALLOWED_ATTR: ['href', 'title'], ALLOW_DATA_ATTR: false,
}))
</script>
<style scoped>
.answer-markdown { overflow-wrap: anywhere; line-height: 1.65; }
.answer-markdown :deep(p + p), .answer-markdown :deep(ul), .answer-markdown :deep(ol) { margin-top: .65rem; }
.answer-markdown :deep(ul) { list-style: disc; padding-left: 1.25rem; }
.answer-markdown :deep(ol) { list-style: decimal; padding-left: 1.25rem; }
.answer-markdown :deep(a) { color: #aaa7ff; text-decoration: underline; }
.answer-markdown :deep(pre), .answer-markdown :deep(table) { max-width: 100%; overflow-x: auto; }
.answer-markdown :deep(code) { font-size: .9em; background: #1d263a; padding: 2px 4px; border-radius: 4px; }
</style>
