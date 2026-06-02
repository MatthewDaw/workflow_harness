import type { Config } from 'tailwindcss';

/**
 * Design tokens promoted from wireframe.html's `:root` block so the SPA matches
 * the prototype's look. Names mirror the wireframe variables (ink/mut/faint/...).
 */
const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#1c1c1c',
        mut: '#6b6b6b',
        faint: '#9a9a9a',
        line: '#cfcfcf',
        line2: '#e3e3e3',
        bg: '#f4f4f2',
        paper: '#ffffff',
        accent: '#3a6ea5',
        good: '#3f7d4e',
        warn: '#9a6b1f',
        live: '#b5402f',
        term: {
          bg: '#1b1d22',
          ink: '#d7dbe0',
          dim: '#7e8794',
          acc: '#7fb2e6',
        },
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '"Segoe UI"', 'Roboto', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', '"SF Mono"', '"Cascadia Code"', 'Consolas', 'monospace'],
      },
      boxShadow: {
        frame: '0 1px 0 rgba(0,0,0,.03)',
      },
    },
  },
  plugins: [],
};

export default config;
