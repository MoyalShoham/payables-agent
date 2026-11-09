/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': process.env.PAYABLES_API ?? 'http://localhost:8000' } },
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] },
})
