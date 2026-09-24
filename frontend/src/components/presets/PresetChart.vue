<template>
  <div class="w-full bg-[#0b1021]/80 border border-slate-800/80 rounded-2xl p-5 shadow-2xl backdrop-blur-xl">
    <div class="flex items-center justify-between mb-4">
      <div>
        <h3 class="text-sm font-semibold text-white tracking-wide flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-indigo-400 animate-pulse"></span>
          {{ data.title || 'Аналитический график' }}
        </h3>
        <p class="text-xs text-slate-400 mt-0.5">Данные актуализированы из PostgreSQL Data Mart</p>
      </div>
      <div class="flex items-center gap-2 text-xs text-slate-400 bg-slate-900/60 px-2.5 py-1 rounded-lg border border-slate-800">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
        <span>Точные агрегаты</span>
      </div>
    </div>

    <!-- Chart container with fixed height for responsiveness -->
    <div class="relative w-full h-72 sm:h-80">
      <Bar
        v-if="chartType === 'bar'"
        :data="barData"
        :options="barOptions"
      />
      <Doughnut
        v-else-if="chartType === 'doughnut'"
        :data="doughnutData"
        :options="doughnutOptions"
      />
      <Bar
        v-else
        :data="barData"
        :options="barOptions"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  Chart as ChartJS,
  Title,
  Tooltip,
  Legend,
  BarElement,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  ArcElement,
  type ChartOptions,
  type ChartData
} from 'chart.js'
import { Bar, Doughnut } from 'vue-chartjs'

ChartJS.register(
  Title,
  Tooltip,
  Legend,
  BarElement,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  ArcElement
)

interface ChartDataset {
  label: string
  data: number[]
  backgroundColor?: string | string[]
  borderColor?: string | string[]
  borderWidth?: number
  borderRadius?: number
}

interface Props {
  data: {
    chart_type?: string
    title?: string
    labels: string[]
    datasets: ChartDataset[]
  }
}

const props = defineProps<Props>()

const chartType = computed(() => props.data.chart_type || 'bar')

const barData = computed<ChartData<'bar'>>(() => ({
  labels: props.data.labels || [],
  datasets: (props.data.datasets || []) as unknown as ChartData<'bar'>['datasets']
}))

const doughnutData = computed<ChartData<'doughnut'>>(() => ({
  labels: props.data.labels || [],
  datasets: (props.data.datasets || []) as unknown as ChartData<'doughnut'>['datasets']
}))

const barOptions: ChartOptions<'bar'> = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      position: 'top',
      labels: {
        color: '#94a3b8',
        font: {
          family: 'Inter, system-ui, sans-serif',
          size: 12
        },
        usePointStyle: true,
        padding: 16
      }
    },
    tooltip: {
      backgroundColor: '#0f172a',
      titleColor: '#f8fafc',
      bodyColor: '#cbd5e1',
      borderColor: '#334155',
      borderWidth: 1,
      padding: 12,
      cornerRadius: 10,
      callbacks: {
        label: (context) => {
          const val = context.raw as number
          return ` ${context.dataset.label}: ${val.toLocaleString('ru-RU')} млн ₸`
        }
      }
    }
  },
  scales: {
    x: {
      grid: {
        color: 'rgba(51, 65, 85, 0.3)'
      },
      ticks: {
        color: '#94a3b8',
        font: {
          size: 11
        }
      }
    },
    y: {
      grid: {
        color: 'rgba(51, 65, 85, 0.3)'
      },
      ticks: {
        color: '#94a3b8',
        font: {
          size: 11
        },
        callback: (value) => `${value} млн ₸`
      }
    }
  }
}

const doughnutOptions: ChartOptions<'doughnut'> = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      position: 'right',
      labels: {
        color: '#94a3b8',
        padding: 16,
        usePointStyle: true
      }
    }
  }
}
</script>
