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
        // ---- Status: direction and state only ----------------------------
        // Green/red mean up/down. They are never reused as "series 3", and every
        // place they appear carries a second encoding (a sign, a position relative
        // to a zero line, or a label) so meaning never rests on colour alone.
        gain: { DEFAULT: '#26d98a', dim: '#1a9c63' },
        loss: { DEFAULT: '#ff5c7c', dim: '#c73e58' },
        caution: { DEFAULT: '#ffb84d', dim: '#c98a2e' },
        accent: { DEFAULT: '#4d9fff', dim: '#2c6dbf' },

        // ---- Categorical: series identity ---------------------------------
        // Assigned in fixed order, never cycled. Validated against the #141924
        // panel surface: lightness band, chroma floor, adjacent CVD separation
        // (worst 8.4 protan), normal-vision floor (19.8) and 3:1 contrast all
        // pass. The first three also pass all-pairs, which is the cap for
        // scatter-like forms. A fifth series folds into "Other" or facets --
        // it never gets a generated hue.
        series: {
          1: '#3987e5', // blue
          2: '#d95926', // orange
          3: '#199e70', // aqua
          4: '#c98500', // yellow
        },
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
