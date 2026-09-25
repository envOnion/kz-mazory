import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_PROXY_URL || 'http://localhost:8000',
        changeOrigin: true
      },
      '/static': {
        target: process.env.VITE_BACKEND_PROXY_URL || 'http://localhost:8000',
        changeOrigin: true
      },
      '/admin': {
        target: process.env.VITE_BACKEND_PROXY_URL || 'http://localhost:8000',
        changeOrigin: true
      }
    }
  }
})
