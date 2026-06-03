import type { Config } from 'tailwindcss';

/**
 * Design tokens for the Command HQ "Field Command" brand kit: an olive-drab +
 * brass + parchment palette on stencil/condensed type. Token NAMES are kept
 * stable (ink/mut/faint/line/bg/paper/accent/good/warn/live/term) so every
 * screen adopts the brand without per-component changes; only the values change.
 * Brand-specific names (od/brass/cream/...) are also exposed for accents.
 */
const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Core text + surfaces (parchment field).
        ink: '#2a271d', // dark field ink
        mut: '#6c6149',
        faint: '#998b69',
        line: '#b6a47a',
        line2: '#d2c29a',
        bg: '#d7c7a0', // page field
        paper: '#f7efda', // cards
        paper2: '#efe4c8', // inset / darker parchment

        // Semantic accents, remapped to the brand.
        accent: '#b1842f', // brass — links, bars, notes
        good: '#5b6235', // olive drab — success / online
        warn: '#8f6921', // dark brass — caution
        live: '#b5402f', // signal red — live/alert (reads as military)

        // Brand-named tokens for explicit use (wordmark, nav, chips).
        od: '#5b6235',
        odd: '#444a26',
        brass: '#b1842f',
        brassd: '#8f6921',
        cream: '#f4eed7',

        // Terminal / live-watch panes: olive-tinted dark.
        term: {
          bg: '#23271a',
          ink: '#cdd6b0',
          dim: '#717a55',
          acc: '#d8b25a',
        },
      },
      fontFamily: {
        // Body: condensed humanist; Mono: typewriter; Stencil: stamped headers.
        sans: ['"Barlow Semi Condensed"', 'system-ui', '"Segoe UI"', 'Roboto', 'Arial', 'sans-serif'],
        mono: ['"Courier Prime"', 'ui-monospace', 'Consolas', 'monospace'],
        stencil: ['"Stardos Stencil"', '"Barlow Semi Condensed"', 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        frame: '0 2px 0 rgba(42,39,29,.18)',
      },
    },
  },
  plugins: [],
};

export default config;
