import base from './playwright.config'
import { defineConfig } from '@playwright/test'

export default defineConfig({
  ...base,
  testMatch: /(preview-demo|demo-tour|live-tour)\.spec\.ts/,
  timeout: 240_000,
  use: {
    ...base.use,
    video: { mode: 'on', size: { width: 1440, height: 900 } },
    viewport: { width: 1440, height: 900 },
  },
})
