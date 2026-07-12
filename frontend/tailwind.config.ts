/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        dark: {
          900: '#0a0a0f',
          800: '#0f0f16',
          700: '#14141f',
          600: '#1a1a2e',
          500: '#23233a',
        },
        neon: {
          red: '#ff3355',
          cyan: '#00e5ff',
          gold: '#ffb830',
          purple: '#a855f7',
          pink: '#ec4899',
        },
        emerald: {
          300: '#6ee7b7',
          500: '#10b981',
          900: '#064e3b',
        },
        yellow: {
          300: '#fde047',
          500: '#eab308',
          900: '#713f12',
        },
        surface: {
          DEFAULT: '#14141f',
          hover: '#1a1a2e',
          border: '#2a2a4a',
        },
      },
      fontFamily: {
        display: ['"Clash Display"', 'system-ui', 'sans-serif'],
        body: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      animation: {
        'pulse-glow': 'pulse-glow 2s ease-in-out infinite',
        'slide-up': 'slide-up 0.3s ease-out',
        'fade-in': 'fade-in 0.2s ease-out',
        'progress-bar': 'progress-bar 1.5s ease-in-out infinite',
      },
      keyframes: {
        'pulse-glow': {
          '0%, 100%': { boxShadow: '0 0 15px rgba(255,51,85,0.3)' },
          '50%': { boxShadow: '0 0 30px rgba(255,51,85,0.6)' },
        },
        'slide-up': {
          from: { opacity: '0', transform: 'translateY(10px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'progress-bar': {
          '0%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(100%)' },
        },
      },
    },
  },
  plugins: [],
}
