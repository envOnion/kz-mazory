/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: '#060912',
        surface: {
          DEFAULT: '#0b1122',
          card: 'rgba(12, 18, 36, 0.72)',
          cardHover: 'rgba(18, 27, 54, 0.85)',
          pill: 'rgba(15, 23, 46, 0.7)',
          pillHover: 'rgba(26, 38, 74, 0.85)',
          border: 'rgba(56, 75, 128, 0.32)',
          borderActive: 'rgba(99, 102, 241, 0.65)',
        },
        brand: {
          primary: '#6366f1',
          accent: '#38bdf8',
          glow: '#4338ca',
          purple: '#8b5cf6',
        }
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      boxShadow: {
        'glass': '0 8px 32px 0 rgba(0, 0, 0, 0.45)',
        'glow-sm': '0 0 15px -2px rgba(99, 102, 241, 0.4)',
        'glow-lg': '0 0 35px -5px rgba(99, 102, 241, 0.35)',
        'glow-blue': '0 0 25px -4px rgba(56, 189, 248, 0.35)',
      }
    },
  },
  plugins: [],
}
