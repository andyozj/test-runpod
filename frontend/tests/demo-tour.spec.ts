// Full product tour for demo recording. Not a regression test.
// Run: npx playwright test tests/demo-tour.spec.ts --config=playwright.video.config.ts
import { test, expect, type Page } from '@playwright/test'

const beat = (page: Page, ms = 1200) => page.waitForTimeout(ms)

test('full product tour', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('gateway-key', 'demo:local-development-key')
    localStorage.setItem('MOCK_DEMO', '1')
  })

  // --- 1. cold open: the thesis ---
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await beat(page, 2600)

  // --- 2. single generation: streaming denoise previews ---
  await page.getByRole('button', { name: 'lock' }).click()
  await page.getByRole('spinbutton', { name: 'Seed' }).fill('42')
  await page.getByLabel('Prompt').fill('a red fox in falling snow, cinematic lighting')
  await beat(page, 700)
  await page.getByRole('button', { name: /Generate ·/ }).click()

  await expect(page.getByTestId('preview-frame').first()).toBeVisible({ timeout: 25_000 })
  await beat(page, 2000)
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await beat(page, 2400)

  // --- 3. lightbox: full-size inspection ---
  await page.getByRole('button', { name: 'View image at full size' }).click()
  await expect(page.getByRole('dialog', { name: 'Image at full size' })).toBeVisible()
  await beat(page, 2000)
  await page.keyboard.press('Escape')
  await beat(page, 800)

  // --- 4. sweep: the parameter experiment ---
  await page.getByRole('button', { name: 'sweep', exact: true }).click()
  await beat(page, 1400)
  await page.getByRole('button', { name: /Generate sweep/ }).click()
  await beat(page, 3000)
  await expect(page.getByText(/sweep settled/i)).toBeVisible({ timeout: 60_000 })
  await beat(page, 3200)

  // --- 5. ledger: the reproducibility record ---
  await page.getByRole('link', { name: 'ledger', exact: false }).click()
  await beat(page, 2600)
  await page.mouse.wheel(0, 500)
  await beat(page, 1800)

  // --- 6. detail: metadata + copy as curl ---
  await page.getByRole('button', { name: /Open details/ }).first().click()
  await expect(page.getByRole('dialog', { name: 'Job details' })).toBeVisible()
  await beat(page, 3000)
  await page.keyboard.press('Escape')
  await beat(page, 800)

  // --- 7. operate: the endpoint dashboard ---
  await page.getByRole('link', { name: 'operate', exact: false }).click()
  await beat(page, 3600)
})
