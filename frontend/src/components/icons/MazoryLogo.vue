<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 48 48"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    class="shrink-0 transition-transform duration-300"
    :class="{ 'drop-shadow-[0_0_20px_rgba(99,102,241,0.6)]': glow }"
  >
    <defs>
      <!-- Gradient for the left silver/white ribbon -->
      <linearGradient id="silverRibbon" x1="6" y1="38" x2="24" y2="10" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#C5D3E8" />
        <stop offset="60%" stop-color="#E2EAF8" />
        <stop offset="100%" stop-color="#FFFFFF" />
      </linearGradient>

      <!-- Gradient for the right vibrant blue/purple ribbon -->
      <linearGradient id="purpleRibbon" x1="18" y1="12" x2="42" y2="38" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#5B58EC" />
        <stop offset="50%" stop-color="#7062FA" />
        <stop offset="100%" stop-color="#935BF8" />
      </linearGradient>

      <!-- Ambient glow filter -->
      <filter id="mGlow" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="3" result="blur" />
        <feComposite in="SourceGraphic" in2="blur" operator="over" />
      </filter>
    </defs>

    <g :filter="glow ? 'url(#mGlow)' : undefined">
      <!-- Right purple/indigo angled polygon -->
      <path
        d="M24 16L34 7L43 16L32 39L22 39L30.5 22L24 16Z"
        fill="url(#purpleRibbon)"
      />
      <!-- Left white/silver angled polygon with overlap -->
      <path
        d="M14 7L24 16L15.5 32L6 39L6 23.5L14 7Z"
        fill="url(#silverRibbon)"
      />
      <!-- Inner fold shadow accent -->
      <path
        d="M20 20L24 16L27 20L22 29L18 29L20 20Z"
        fill="#3F42A8"
        fill-opacity="0.35"
      />
    </g>
  </svg>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    size?: number | string
    glow?: boolean
  }>(),
  {
    size: 32,
    glow: false,
  }
)
</script>
