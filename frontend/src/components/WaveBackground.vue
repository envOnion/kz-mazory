<template>
  <div ref="hostRef" class="wave-background pointer-events-none fixed inset-0 z-0 overflow-hidden bg-[#060912]" aria-hidden="true">
    <canvas ref="canvasRef" class="block h-full w-full bg-transparent"></canvas>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const props = withDefaults(
  defineProps<{
    speed?: number
    intensity?: number
    density?: number
    parallax?: number
  }>(),
  {
    speed: 1.0,
    intensity: 1.0,
    density: 1.0,
    parallax: 0.6,
  }
)

const hostRef = ref<HTMLDivElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)

let cleanupFn: (() => void) | null = null

onMounted(() => {
  const canvasEl = canvasRef.value
  const hostEl = hostRef.value
  if (!canvasEl || !hostEl) return

  const rawCtx = canvasEl.getContext('2d', { alpha: true })
  if (!rawCtx) return
  const ctx = rawCtx
  const canvas = canvasEl
  const host = hostEl

  let destroyed = false

  const SETTINGS = {
    speed: props.speed,
    intensity: props.intensity,
    density: props.density,
    parallax: props.parallax,
    maxPixelRatio: 1.5,
    maxFPS: 60,
    reducedMotion: 'slow',
  }

  const TAU = Math.PI * 2
  const LEVELS = 26
  const SIZES = 7
  const radii = [0.48, 0.63, 0.80, 1.02, 1.28, 1.62, 2.02]
  const media = window.matchMedia('(prefers-reduced-motion: reduce)')
  const finePointer = window.matchMedia('(pointer: fine)')
  const colors: string[] = []
  const buckets: number[][] = []
  const counts = new Uint32Array(LEVELS * SIZES)

  for (let i = 0; i < LEVELS; i++) {
    const a = i / (LEVELS - 1)
    colors.push(`rgba(${Math.round(72 + a * 42)},${Math.round(87 + a * 44)},255,${a.toFixed(4)})`)
  }

  let width = 1,
    height = 1,
    dpr = 1,
    sizeScale = 1
  let particles = new Float32Array(0),
    pointCount = 0
  let sceneTime = 0,
    raf = 0
  let lastTick: number | null = null,
    lastPaint = -Infinity,
    paused = false
  let lost = false,
    pageSuspended = false,
    forceMotion = false
  let px = 0,
    py = 0,
    tx = 0,
    ty = 0
  let resizeFrame = 0

  const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v))
  const smooth = (a: number, b: number, v: number) => {
    const x = clamp((v - a) / (b - a), 0, 1)
    return x * x * (3 - 2 * x)
  }

  function motionFactor() {
    if (forceMotion || !media.matches) return 1
    if (SETTINGS.reducedMotion === 'pause') return 0
    return SETTINGS.reducedMotion === 'slow' ? 0.5 : 1
  }

  function shouldRun() {
    return (
      !destroyed &&
      !paused &&
      !lost &&
      !pageSuspended &&
      !document.hidden &&
      SETTINGS.speed > 0 &&
      motionFactor() > 0
    )
  }

  function buildParticles() {
    const nx = Math.round(clamp(width / 8.2, 82, 192) * SETTINGS.density)
    const ny = Math.round((width < 700 ? 62 : 79) * Math.sqrt(SETTINGS.density))
    pointCount = nx * ny
    particles = new Float32Array(pointCount * 8)
    let k = 0
    for (let j = 0; j < ny; j++) {
      const v = j / (ny - 1)
      const perspective = 0.4 + 1.08 * Math.pow(v, 1.12)
      const v153 = Math.pow(v, 1.53)
      const v127 = Math.pow(v, 1.27)
      const size = Math.floor(clamp(v * 6.0, 0, SIZES - 1))
      for (let i = 0; i < nx; i++) {
        const hash = Math.sin(i * 127.1 + j * 311.7) * 43758.5453
        particles[k++] = (i / (nx - 1)) * 2 - 1
        particles[k++] = v
        particles[k++] = hash - Math.floor(hash)
        particles[k++] = perspective
        particles[k++] = v153
        particles[k++] = v127
        particles[k++] = size
        particles[k++] = 0
      }
    }
    for (let i = 0; i < LEVELS * SIZES; i++) buckets[i] = []
  }

  function circles(pts: number[], count: number, radius: number) {
    ctx.beginPath()
    for (let i = 0; i < count; i += 2) {
      ctx.moveTo(pts[i] + radius, pts[i + 1])
      ctx.arc(pts[i], pts[i + 1], radius, 0, TAU)
    }
    ctx.fill()
  }

  function draw(time: number) {
    if (destroyed || lost) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)
    counts.fill(0)

    const portrait = 1.06 - 0.06 * smooth(0.7, 1.6, width / height)
    const lowerSwell = Math.sin(0.64 + time * 0.3) * 0.8
    const leftSwell = Math.sin(0.72 + time * 0.34) * 0.3

    for (const layer of [0, 2, 3, 1]) {
      for (let i = 0; i < particles.length; i += 8) {
        const u = particles[i],
          v = particles[i + 1],
          seed = particles[i + 2]
        const perspective = particles[i + 3]
        let s = 0.5 + u * 1.24 * perspective
        let x: number, y: number, a: number, b: number, crest: number, strength: number

        if (layer === 0) {
          a = s * 10.3 - 4.3 - time * 0.22
          b = s * 8 + v * 3 - 1.04 - time * 0.54
          const horizon =
            0.105 + Math.sin(a) * 0.038 + Math.sin(s * 18 + 0.4 + time * 0.18) * 0.011
          x = s + Math.sin(v * 5.1 + 0.96 + time * 0.36) * 0.018
          y = horizon + (v - 0.5) * 0.3 + Math.sin(v * TAU) * 0.064 + Math.sin(b) * 0.023
          crest = Math.pow(0.5 + 0.5 * Math.sin(s * 8 + v * 2 - 0.96 - time * 0.48), 3)
          strength =
            0.66 *
            smooth(0.035, 0.2, v) *
            (1 - smooth(0.73, 1, v)) *
            smooth(-0.04, 0.3, s)
        } else if (layer === 1) {
          a = s * 6.3 - v * 3.6 + 0.9 + lowerSwell
          b = s * 10.9 + v * 4.8 - 1.04 - time * 0.57
          x = s + Math.sin(v * 4 - 0.8 - time * 0.3) * 0.025
          y = 0.675 + particles[i + 4] * 0.8 - Math.cos(a) * 0.142 + Math.sin(b) * 0.065
          crest = Math.pow(0.5 + 0.5 * Math.sin(a + 0.65), 3)
          strength = 0.95 * smooth(0.015, 0.15, v)
        } else if (layer === 2) {
          s = 0.08 + u * 0.7 * perspective
          a = s * 8 - 0.9 + leftSwell
          b = v * TAU + s * 2.2 + 0.88 + time * 0.48
          x = s + Math.sin(v * 3.8 + 0.8 + time * 0.3) * 0.018
          y = 0.405 + s * 0.28 + Math.sin(a) * 0.045 + (v - 0.45) * 0.34 + Math.sin(b) * 0.055
          crest = Math.pow(0.5 + 0.5 * Math.sin(s * 10 + v * 3 - 0.96 - time * 0.44), 3)
          strength =
            0.63 *
            smooth(0.04, 0.19, v) *
            (1 - smooth(0.76, 1, v)) *
            (1 - smooth(0.34, 0.69, s))
        } else {
          s = 0.53 + u * 0.8 * perspective
          a = s * 7.3 - v * 4.8 - 1.36 - time * 0.49
          b = s * 13.8 + v * 4.1 + 0.96 + time * 0.35
          x = 1.13 - particles[i + 5] * 0.4 + Math.sin(a) * 0.083 + Math.sin(b) * 0.03
          y = s
          crest = Math.pow(0.5 + 0.5 * Math.cos(a - 0.5), 3)
          strength =
            0.78 *
            smooth(0.045, 0.17, v) *
            (1 - smooth(0.78, 1, v)) *
            smooth(0.12, 0.28, s) *
            (1 - smooth(0.83, 1.04, s))
        }

        x = 0.5 + (x - 0.5) * portrait + px * (0.25 + v * 0.75)
        y += py * (0.25 + v * 0.75)
        if (x < -0.01 || x > 1.01 || y < -0.01 || y > 1.01) continue

        const distance = Math.hypot((x - 0.5) / 0.51, (y - 0.47) / 0.44)
        const calm = smooth(0.52, 1.05, distance)
        const shimmer = 0.89 + 0.11 * Math.sin(time * 0.6 + seed * TAU)
        const alpha =
          strength *
          calm *
          (0.68 + seed * 0.48) *
          (0.43 + crest * 0.86) *
          shimmer *
          SETTINGS.intensity
        const level = Math.min(LEVELS - 1, Math.round(alpha * (LEVELS - 1)))
        if (level < 1) continue

        let sizeIndex = particles[i + 6]
        if (layer === 0) sizeIndex = Math.max(0, sizeIndex - 1)
        const index = sizeIndex * LEVELS + level
        const bucket = buckets[index]
        const n = counts[index]
        bucket[n] = x * width
        bucket[n + 1] = y * height
        counts[index] = n + 2
      }
    }

    ctx.globalCompositeOperation = 'lighter'
    for (let size = 0; size < SIZES; size++) {
      const r = radii[size] * sizeScale
      for (let level = 1; level < LEVELS; level++) {
        const index = size * LEVELS + level
        const count = counts[index]
        if (!count) continue
        const bucket = buckets[index]
        ctx.fillStyle = colors[level]
        if (size >= 4 && level >= 6) {
          ctx.globalAlpha = 0.08
          circles(bucket, count, r * 2.0)
        }
        ctx.globalAlpha = 0.82
        circles(bucket, count, r)
      }
    }
    ctx.globalAlpha = 1
    ctx.globalCompositeOperation = 'source-over'
  }

  function resize() {
    if (destroyed || lost || !host) return
    const rect = host.getBoundingClientRect()
    const nextWidth = Math.max(1, Math.round(rect.width))
    const nextHeight = Math.max(1, Math.round(rect.height))
    const nextDpr = clamp(window.devicePixelRatio || 1, 1, SETTINGS.maxPixelRatio)
    if (nextWidth === width && nextHeight === height && nextDpr === dpr && pointCount) return
    width = nextWidth
    height = nextHeight
    dpr = nextDpr
    canvas.width = Math.round(width * dpr)
    canvas.height = Math.round(height * dpr)
    sizeScale = clamp(width / 1280, 0.82, 1.18)
    buildParticles()
    draw(sceneTime)
  }

  function onResize() {
    if (!resizeFrame && !destroyed) {
      resizeFrame = requestAnimationFrame(() => {
        resizeFrame = 0
        resize()
      })
    }
  }

  function frame(now: number) {
    raf = 0
    if (!shouldRun()) return
    const elapsed = lastTick === null ? 0 : clamp((now - lastTick) / 1000, 0, 0.25)
    lastTick = now
    sceneTime += elapsed * SETTINGS.speed * motionFactor()
    const damping = 1 - Math.exp(-elapsed * 3.5)
    px += (tx - px) * damping
    py += (ty - py) * damping
    if (now - lastPaint >= 1000 / SETTINGS.maxFPS - 0.75) {
      lastPaint = now
      draw(sceneTime)
    }
    raf = requestAnimationFrame(frame)
  }

  function syncPlayback() {
    cancelAnimationFrame(raf)
    raf = 0
    lastTick = null
    lastPaint = -Infinity
    if (shouldRun()) raf = requestAnimationFrame(frame)
  }

  function onPointer(event: PointerEvent) {
    if (destroyed || !finePointer.matches || media.matches) return
    tx = (event.clientX / width - 0.5) * 0.018 * SETTINGS.parallax
    ty = (event.clientY / height - 0.5) * 0.014 * SETTINGS.parallax
  }

  function resetPointer() {
    tx = 0
    ty = 0
  }

  function onVisibilityChange() {
    if (destroyed) return
    if (document.hidden) {
      cancelAnimationFrame(raf)
      raf = 0
    } else {
      syncPlayback()
    }
  }

  const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(onResize) : null
  if (observer) observer.observe(host)
  window.addEventListener('resize', onResize, { passive: true })
  window.addEventListener('pointermove', onPointer, { passive: true })
  document.documentElement.addEventListener('pointerleave', resetPointer, { passive: true })
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('focus', onVisibilityChange)

  resize()
  syncPlayback()

  cleanupFn = () => {
    destroyed = true
    syncPlayback()
    cancelAnimationFrame(resizeFrame)
    if (observer) observer.disconnect()
    window.removeEventListener('resize', onResize)
    window.removeEventListener('pointermove', onPointer)
    document.documentElement.removeEventListener('pointerleave', resetPointer)
    document.removeEventListener('visibilitychange', onVisibilityChange)
    window.removeEventListener('focus', onVisibilityChange)
  }
})

onUnmounted(() => {
  if (cleanupFn) cleanupFn()
})
</script>
