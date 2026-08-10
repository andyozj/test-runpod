// Unmasked screen captures for human review. Not a regression test.
// Run: npx playwright test tests/capture.spec.ts
import { test, expect, type Page } from '@playwright/test'

const OUT = process.env.CAPTURE_DIR ?? 'capture'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('gateway-key', 'local-development-key')
    localStorage.setItem('MOCK_FAST', '1')
  })
})

async function lockSeed(page: Page, seed: string) {
  await page.getByRole('button', { name: 'lock' }).click()
  await page.getByRole('spinbutton', { name: 'Seed' }).fill(seed)
}

async function generate(page: Page, prompt: string) {
  await page.getByLabel('Prompt').fill(prompt)
  await page.getByRole('button', { name: /Generate/ }).click()
}

test('capture all screens', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await page.screenshot({ path: `${OUT}/1-empty.png` })

  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByTestId('stage-timeline')).toBeVisible()
  await page.screenshot({ path: `${OUT}/2-generating.png` })

  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await page.screenshot({ path: `${OUT}/3-completed.png` })

  await page.getByRole('button', { name: 'View image at full size' }).click()
  const lightbox = page.getByRole('dialog', { name: 'Image at full size' })
  await expect(lightbox.getByText(/zoom \d+%/)).toBeVisible()
  await page.screenshot({ path: `${OUT}/7-lightbox.png` })
  await page.keyboard.press('Escape')
  await expect(lightbox).not.toBeVisible()

  await page.getByRole('link', { name: 'ledger' }).click()
  await expect(page.getByText('reproducibility ledger', { exact: false })).toBeVisible()
  await page.waitForTimeout(1500)
  await page.screenshot({ path: `${OUT}/4-gallery.png` })

  await page.getByRole('img', { name: /red fox/i }).first().click()
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/5-detail.png` })

  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: /Open details: antarctic/ }).click()
  await expect(
    page.getByRole('dialog', { name: 'Job details' }).getByText('OOM'),
  ).toBeVisible()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/6-detail-failed.png` })
})

test('capture batch mid-flight', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await lockSeed(page, '42')
  await page
    .getByRole('group', { name: 'Batch size' })
    .getByRole('button', { name: '4', exact: true })
    .click()
  // one cell shed (429), one complete, one previewing, one queued
  await generate(page, 'demo:shed-one demo:mixed a red fox in falling snow')
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({ timeout: 15_000 })
  await expect(
    page.getByTestId('batch-timeline').getByText(/cold start/),
  ).toBeVisible({ timeout: 15_000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/9-batch.png` })
})

test('capture completed steps sweep', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await lockSeed(page, '42')
  await page
    .getByRole('group', { name: 'Mode' })
    .getByRole('button', { name: 'sweep' })
    .click()
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(4, {
    timeout: 45_000,
  })
  await expect(page.getByTestId('sweep-summary')).toBeVisible()
  await page.waitForTimeout(700)
  await page.screenshot({ path: `${OUT}/10-sweep.png` })
})

test('capture operate dashboard', async ({ page }) => {
  // pin the throughput window's trailing hour: bar positions must not drift
  await page.addInitScript(() => localStorage.setItem('MOCK_HOUR', '23'))
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await lockSeed(page, '42')
  // a parked in-flight job so the endpoint strip reads a working fleet
  await generate(page, 'demo:hold a red fox in falling snow')
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({
    timeout: 15_000,
  })
  await page.getByRole('link', { name: 'operate' }).click()
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/12-operate.png` })
})

test('capture operate with no upstream reading', async ({ page }) => {
  // the "unknown, not zero" path: dashes and a STALE chip where a reading would be
  await page.addInitScript(() => {
    localStorage.setItem('MOCK_HOUR', '23')
    localStorage.setItem('MOCK_METRICS', 'unknown')
  })
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/12c-operate-unknown.png` })
})

test.describe('mobile', () => {
  test.use({ viewport: { width: 375, height: 667 } })

  test('capture mobile screens', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByLabel('Prompt')).toBeVisible()
    await page.evaluate(() => document.fonts.ready)
    await page.screenshot({ path: `${OUT}/11-mobile-1-empty.png` })

    await page.getByRole('button', { name: 'params' }).click()
    await expect(page.locator('#params-rail')).toBeVisible()
    await page.screenshot({ path: `${OUT}/11-mobile-2-params.png` })
    await lockSeed(page, '42')
    await page.keyboard.press('Escape')

    await page.getByRole('link', { name: 'ledger' }).click()
    await expect(page.getByText('reproducibility ledger')).toBeVisible()
    await page.waitForTimeout(1500)
    await page.screenshot({ path: `${OUT}/11-mobile-3-gallery.png` })

    await page.getByRole('link', { name: 'generate' }).click()
    await generate(page, 'a red fox in falling snow, cinematic lighting')
    await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
      timeout: 30_000,
    })
    await page.screenshot({ path: `${OUT}/11-mobile-4-completed.png` })
  })
})

test('capture previewing cell', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await lockSeed(page, '42')
  // 'demo:hold' parks the mock at 60% with a preview frame
  await generate(page, 'demo:hold a red fox in falling snow')
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({ timeout: 15_000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/8-previewing.png` })
})

test('capture operate zero state', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('MOCK_HOUR', '23')
    localStorage.setItem('MOCK_METRICS', 'empty')
  })
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/12b-operate-zero.png` })
})
