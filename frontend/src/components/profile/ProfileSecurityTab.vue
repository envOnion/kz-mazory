<template>
  <div class="space-y-6">
    <div>
      <h3 class="text-base font-semibold text-white">Безопасность и активные сессии</h3>
      <p class="text-xs text-slate-400 mt-0.5">Информация об авторизованных устройствах и параметрах безопасности</p>
    </div>

    <!-- Active Device Card -->
    <div class="p-4 rounded-2xl bg-[#0e1633]/70 border border-[#2d3a63]/40 space-y-3">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-xl bg-indigo-500/15 border border-indigo-400/30 flex items-center justify-center text-indigo-300">
            <Laptop class="w-5 h-5" />
          </div>
          <div>
            <div class="text-xs font-bold text-white flex items-center gap-2">
              <span>Текущее устройство (Браузер)</span>
              <span class="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"></span>
            </div>
            <div class="text-[11px] text-slate-400 mt-0.5">
              macOS • Chrome WebKit • IP: 127.0.0.1
            </div>
          </div>
        </div>
        <span class="text-[11px] text-emerald-400 font-medium">Активно сейчас</span>
      </div>

      <div class="text-[11px] text-slate-400 pt-2 border-t border-slate-700/40">
        Сессия защищена с использованием JWT (HS256) и одноразового СМС-подтверждения через Redis.
      </div>
    </div>

    <!-- Token Security Info -->
    <div class="p-4 rounded-2xl bg-[#0e1633]/50 border border-[#2d3a63]/40 space-y-2">
      <div class="text-xs font-semibold text-white">Параметры безопасности</div>
      <div class="text-xs text-slate-300 flex items-center justify-between">
        <span>Время жизни сессии доступа (Access)</span>
        <span class="font-mono text-indigo-300">7 дней</span>
      </div>
      <div class="text-xs text-slate-300 flex items-center justify-between">
        <span>Период обновления токена (Refresh)</span>
        <span class="font-mono text-indigo-300">30 дней</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Laptop } from 'lucide-vue-next'
</script>
