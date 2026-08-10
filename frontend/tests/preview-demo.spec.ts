// Video demo of the streaming denoising-preview flow. Not a regression test.
// Run: npx playwright test tests/preview-demo.spec.ts --config=playwright.video.config.ts
import { test, expect } from '@playwright/test'

test('preview flow demo', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('gateway-key', 'local-development-key')
    // demo pacing: ~1 progress stride per poll so all 4 preview frames appear
    localStorage.setItem('MOCK_DEMO', '1')
  })
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(800)

  // submit
  await page.getByRole('button', { name: 'lock' }).click()
  await page.getByRole('spinbutton', { name: 'Seed' }).fill('42')
  await page.getByLabel('Prompt').fill('a red fox in falling snow, cinematic lighting')
  await page.getByRole('button', { name: /Generate ·/ }).click()

  // queued → first preview frame with its STEP stamp
  await expect(page.getByTestId('stage-timeline')).toBeVisible()
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({
    timeout: 20_000,
  })
  await expect(page.getByText(/preview · step/)).toBeVisible()

  // frames sharpen until the final image materializes over the last preview
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByText(/bench p50/)).toBeVisible()
  await page.waitForTimeout(1500)

  // lightbox
  await page.getByRole('button', { name: 'View image at full size' }).click()
  const lightbox = page.getByRole('dialog', { name: 'Image at full size' })
  await expect(lightbox.getByText(/zoom \d+%/)).toBeVisible()
  await page.waitForTimeout(2000)
})
