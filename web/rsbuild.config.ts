import { defineConfig } from '@rsbuild/core'
import { pluginReact } from '@rsbuild/plugin-react'
import { pluginTailwindcss } from '@rsbuild/plugin-tailwindcss'
import { tanstackRouter } from '@tanstack/router-plugin/rspack'

export default defineConfig({
  server: {
    port: 3000,
  },
  source: {
    entry: {
      index: './src/main.tsx',
    },
  },
  html: {
    mountId: 'app',
    title: 'PGI Quality Platform',
  },
  plugins: [pluginReact(), pluginTailwindcss()],
  tools: {
    rspack: {
      plugins: [
        tanstackRouter({
          target: 'react',
          autoCodeSplitting: true,
        }),
      ],
    },
  },
})
