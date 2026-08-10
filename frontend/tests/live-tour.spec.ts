// Product tour recorded against the live gateway and the deployed RunPod
// endpoint. Not a regression test — no mock, so every timing is a real one and
// the timeouts are sized for real jobs (~22-30s warm, minutes on a cold start).
//
// Run against a non-mock build served on :4173 with /v1 and /health proxied to
// the gateway:
//   npm run build && GATEWAY_URL=http://localhost:8010 npx vite preview --port 4173
//   npx playwright test tests/live-tour.spec.ts --config=playwright.video.config.ts
import { test, expect, type Page } from '@playwright/test'

const SHOTS = 'capture-final'

const beat = (page: Page, ms = 1200) => page.waitForTimeout(ms)

test('live product tour', async ({ page }) => {
  test.setTimeout(15 * 60_000)

  await page.addInitScript(() => {
    localStorage.setItem('gateway-key', 'local-development-key')
  })

  // --- 1. cold open: the thesis ---
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  await beat(page, 2600)

  // --- 2. one real generation: streaming denoise previews off the GPU ---
  await page.getByRole('button', { name: 'lock' }).click()
  await page.getByRole('spinbutton', { name: 'Seed' }).fill('42')
  await page.getByLabel('Prompt').fill('a red fox in falling snow, cinematic lighting')
  await beat(page, 700)
  await page.getByRole('button', { name: /Generate ·/ }).click()

  await expect(page.getByTestId('stage-timeline')).toBeVisible({ timeout: 120_000 })
  // first worker frame: on a warm endpoint ~5s, on a cold one several minutes
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({
    timeout: 8 * 60_000,
  })
  // hold on the frames while they sharpen
  await beat(page, 6000)
  await page.screenshot({ path: `${SHOTS}/live-2-previewing.png` })

  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 5 * 60_000,
  })
  await expect(page.getByText(/bench p50/)).toBeVisible()
  await beat(page, 3200)
  await page.screenshot({ path: `${SHOTS}/live-3-completed.png` })

  // --- 3. lightbox: the real image at full size ---
  await page.getByRole('button', { name: 'View image at full size' }).click()
  await expect(page.getByRole('dialog', { name: 'Image at full size' })).toBeVisible()
  await beat(page, 2600)
  await page.screenshot({ path: `${SHOTS}/live-4-lightbox.png` })
  await page.keyboard.press('Escape')
  await beat(page, 1000)

  // --- 4. sweep: four real jobs, one variable ---
  await page.getByRole('button', { name: 'sweep', exact: true }).click()
  await beat(page, 1600)
  await page.getByRole('button', { name: /Generate sweep/ }).click()
  await beat(page, 4000)
  await expect(page.getByText(/sweep settled/i)).toBeVisible({ timeout: 10 * 60_000 })
  await beat(page, 4000)
  await page.screenshot({ path: `${SHOTS}/live-5-sweep.png` })
  await beat(page, 1500)

  // --- 5. ledger: five real generations on the record ---
  await page.getByRole('link', { name: 'ledger', exact: false }).click()
  await beat(page, 2800)
  await page.screenshot({ path: `${SHOTS}/live-6-ledger.png` })
  await page.mouse.wheel(0, 500)
  await beat(page, 2000)

  // --- 6. detail: model revision @sha, seed, measured timings, copy as curl ---
  await page.getByRole('button', { name: /Open details/ }).first().click()
  await expect(page.getByRole('dialog', { name: 'Job details' })).toBeVisible()
  await beat(page, 4000)
  await page.screenshot({ path: `${SHOTS}/live-7-detail.png` })
  await beat(page, 1200)
  await page.keyboard.press('Escape')
  await beat(page, 1000)

  // --- 7. operate: real metrics off the live gateway ---
  await page.getByRole('link', { name: 'operate', exact: false }).click()
  await beat(page, 4000)
  await page.screenshot({ path: `${SHOTS}/live-8-operate.png` })
  await beat(page, 2500)
})
