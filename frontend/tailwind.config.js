/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Terminal palette. Deliberately desaturated so the only saturated colour
        // on screen is data -- gains, losses and warnings read instantly.
        terminal: {
          950: '#07090d',
          900: '#0b0e14',
          850: '#0f131b',
          800: '#141924',
          750: '#1a2030',
          700: '#232b3d',
          600: '#2f3a52',
          500: '#465575',
        },
        // Green/red are for direction only. Never use them for a chart series that
        // is not signed, or the whole screen stops meaning anything.
        gain: { DEFAULT: '#26d98a', dim: '#1a9c63' },
        loss: { DEFAULT: '#ff5c7c', dim: '#c73e58' },
        accent: { DEFAULT: '#4d9fff', dim: '#2c6dbf' },
        caution: { DEFAULT: '#ffb84d', dim: '#c98a2e' },
      },
      fontFamily: {
        // Tabular figures matter: a column of prices that shifts horizontally as
        // digits change is genuinely harder to scan.
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['JetBrains Mono', 'SFMono-Regular', 'Consolas', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.04)',
        glow: '0 0 24px -8px rgba(77,159,255,0.45)',
      },
      animation: {
        'pulse-soft': 'pulse-soft 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        shimmer: 'shimmer 1.6s linear infinite',
      },
      keyframes: {
        'pulse-soft': { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.45' } },
        shimmer: {
          '0%': { backgroundPosition: '-500px 0' },
          '100%': { backgroundPosition: '500px 0' },
        },
      },
    },
  },
  plugins: [],
};
