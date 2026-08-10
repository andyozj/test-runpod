import { expect, test, type Locator, type Page } from '@playwright/test'

const consoleErrors: string[] = []

test.beforeEach(async ({ page }) => {
  consoleErrors.length = 0
  page.on('console', (msg) => {
    // the 429-shed and 502-drop scenarios legitimately log the browser's
    // resource-load line
    if (msg.type() === 'error' && !/status of (429|502)/.test(msg.text()))
      consoleErrors.push(msg.text())
  })
  page.on('pageerror', (err) => consoleErrors.push(String(err)))
  await page.addInitScript(() => {
    localStorage.setItem('gateway-key', 'local-development-key')
    localStorage.setItem('MOCK_FAST', '1')
  })
})

test.afterEach(() => {
  expect(consoleErrors).toEqual([])
})

function dynMasks(page: Page) {
  return [page.getByTestId('dyn')]
}

async function lockSeed(page: Page, seed: string) {
  await page.getByRole('button', { name: 'lock' }).click()
  await page.getByRole('spinbutton', { name: 'Seed' }).fill(seed)
}

async function generate(page: Page, prompt: string) {
  await page.getByLabel('Prompt').fill(prompt)
  await page.getByRole('button', { name: /Generate/ }).click()
}

async function setBatch(page: Page, n: '2' | '4') {
  await page
    .getByRole('group', { name: 'Batch size' })
    .getByRole('button', { name: n, exact: true })
    .click()
}

test('empty generate view', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await expect(page.locator('footer').getByText('gateway', { exact: true })).toBeVisible()
  await expect(page).toHaveScreenshot('empty-generate.png', {
    mask: dynMasks(page),
  })
})

test('Enter in the prompt submits', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Prompt').fill('a red fox in falling snow')
  await page.getByLabel('Prompt').press('Enter')
  await expect(page.getByTestId('stage-timeline')).toBeVisible()
})

test('param chip focuses its rail input', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'steps 28' }).click()
  await expect(page.getByLabel('Inference steps')).toBeFocused()
})

test('mid-generation stage timeline', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  const timeline = page.getByTestId('stage-timeline')
  await expect(timeline).toBeVisible()
  await expect(timeline.getByText('inference')).toBeVisible()
  await expect(page).toHaveScreenshot('mid-generation.png', {
    mask: dynMasks(page),
  })
})

test('denoising preview fills the cell', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  // 'demo:hold' parks the mock at 60% with preview frame 2: deterministic pixels
  await generate(page, 'demo:hold a red fox in falling snow')
  const frame = page.getByTestId('preview-frame')
  await expect(frame.first()).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/preview · step/)).toBeVisible()
  await expect(page).toHaveScreenshot('previewing.png', {
    mask: dynMasks(page),
  })
})

test('preview frames crossfade, then drop on terminal', async ({ page }) => {
  // demo pacing: one ~10% stride per poll, so successive frames are observed
  await page.addInitScript(() => localStorage.setItem('MOCK_DEMO', '1'))
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  const frames = page.getByTestId('preview-frame')
  // two layered frames = predecessor + successor of the crossfade
  await expect(frames).toHaveCount(2, { timeout: 20_000 })
  // terminal: final image takes over and preview frames leave the DOM
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(frames).toHaveCount(0, { timeout: 10_000 })
  await expect(page.getByText(/bench p50/)).toBeVisible()
})

test('completed with image', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
  const image = page.getByRole('img', { name: 'Generated image' })
  await expect(image).toBeVisible()
  await expect(page).toHaveScreenshot('completed.png', {
    mask: [...dynMasks(page), image],
  })
})

test('lightbox on the completed image', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await page.getByRole('button', { name: 'View image at full size' }).click()
  const lightbox = page.getByRole('dialog', { name: 'Image at full size' })
  await expect(lightbox.getByText(/^zoom \d+%$/)).toBeVisible()
  await expect(lightbox.getByText('1024 × 1024 px')).toBeVisible()
  await expect(lightbox.getByText('seed 42')).toBeVisible()
  await expect(page).toHaveScreenshot('lightbox.png', {
    mask: [lightbox.getByRole('img')],
  })
  // z toggles to 100% actual pixels; Esc closes
  await page.keyboard.press('z')
  await expect(lightbox.getByText('zoom 100%')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(lightbox).not.toBeVisible()
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible()
})

test('download saves the fetched blob under a reproducible name', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  const button = page.getByRole('button', { name: 'download png' })
  await expect(button).toBeVisible({ timeout: 30_000 })
  const downloadPromise = page.waitForEvent('download')
  await button.click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('flux_seed42_1024x1024.png')
})

test('overlay arrow keys step through the filtered list', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  await page.getByRole('button', { name: /Open details: a red fox/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog.getByText('1 / 12')).toBeVisible()
  await page.keyboard.press('ArrowRight')
  await expect(dialog.getByText('2 / 12')).toBeVisible()
  await expect(
    dialog.getByText('lighthouse on a basalt cliff, long exposure'),
  ).toBeVisible()
  await page.keyboard.press('ArrowLeft')
  await expect(dialog.getByText('1 / 12')).toBeVisible()
  await expect(
    dialog.getByText('a red fox in falling snow, cinematic lighting'),
  ).toBeVisible()
  // left edge is a no-op
  await page.keyboard.press('ArrowLeft')
  await expect(dialog.getByText('1 / 12')).toBeVisible()
  // Esc closes the lightbox first, then the overlay
  const zoomButton = dialog.getByRole('button', { name: 'View image at full size' })
  await expect(zoomButton).toBeEnabled()
  await zoomButton.click()
  const lightbox = page.getByRole('dialog', { name: 'Image at full size' })
  await expect(lightbox).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(lightbox).not.toBeVisible()
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
})

test('error envelope: blocked prompt', async ({ page }) => {
  await page.goto('/')
  await generate(page, 'demo:blocked thing')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope).toBeVisible({ timeout: 15_000 })
  await expect(envelope.getByText('PROMPT_BLOCKED')).toBeVisible()
  await expect(page).toHaveScreenshot('error-blocked.png', {
    mask: dynMasks(page),
  })
})

test('429 shed with Retry-After countdown', async ({ page }) => {
  await page.goto('/')
  await generate(page, 'demo:shed the load')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope).toBeVisible()
  await expect(envelope.getByText('QUEUE_SATURATED')).toBeVisible()
  await expect(envelope.getByRole('button', { name: /retry in \d+s/ })).toBeVisible()
  await expect(page).toHaveScreenshot('error-429-shed.png', {
    mask: dynMasks(page),
  })
})

test('cold-start annotation on the queued stage', async ({ page }) => {
  await page.goto('/')
  await generate(page, 'demo:cold mountain at dawn light')
  const timeline = page.getByTestId('stage-timeline')
  await expect(timeline.getByText(/cold start/)).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveScreenshot('cold-start.png', {
    mask: dynMasks(page),
  })
})

test('batch 2×2 mid-batch: mixed cell states', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await setBatch(page, '4')
  // demo:shed-one 429s cell 1; demo:mixed parks the rest at complete /
  // previewing / queued (they settle terminally later)
  await generate(page, 'demo:shed-one demo:mixed a red fox in falling snow')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope).toBeVisible()
  await expect(envelope.getByText('QUEUE_SATURATED')).toBeVisible()
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByRole('button', { name: 'download png' })).toBeVisible()
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({
    timeout: 15_000,
  })
  const timeline = page.getByTestId('batch-timeline')
  await expect(timeline).toBeVisible()
  await expect(timeline.getByText(/cold start/)).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveScreenshot('batch-mixed.png', {
    mask: [
      ...dynMasks(page),
      page.getByRole('img', { name: 'Generated image' }),
      page.getByRole('button', { name: /retry in \d+s|retry cell/ }),
    ],
  })
})

test('batch of 4: distinct seeds, four sparkline points', async ({ page }) => {
  await page.goto('/')
  await setBatch(page, '4')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(4, {
    timeout: 45_000,
  })
  // dice mode: four client-chosen crypto seeds, echoed by the API — all distinct
  const seedTexts = await page.getByTestId('cell-seed').allInnerTexts()
  expect(seedTexts).toHaveLength(4)
  expect(new Set(seedTexts).size).toBe(4)
  await expect(
    page.getByTestId('batch-timeline').getByText(/batch wall/),
  ).toBeVisible()
  // one sparkline data point per completed job: 156 committed + 4
  await expect(page.getByText(/data point 160/)).toBeVisible()
})

// restored: the sweep merge dropped the batch-of-2 coverage (23→22); nothing
// else exercises setBatch('2') or the +1 locked-seed contract
test('batch of 2 with a locked seed: cell seeds increment', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await setBatch(page, '2')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(2, {
    timeout: 45_000,
  })
  const seedTexts = await page.getByTestId('cell-seed').allInnerTexts()
  expect(seedTexts.map((s) => s.replace(/\D/g, ''))).toEqual(['42', '43'])
  await expect(
    page.getByTestId('batch-timeline').getByText(/batch wall/),
  ).toBeVisible()
})

test('batch cancel cancels every in-flight cell', async ({ page }) => {
  await page.goto('/')
  await setBatch(page, '4')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  const timeline = page.getByTestId('batch-timeline')
  await expect(timeline).toBeVisible()
  await timeline.getByRole('button', { name: 'cancel' }).click()
  await expect(page.getByText('JOB_CANCELLED')).toHaveCount(4, {
    timeout: 15_000,
  })
  await expect(timeline.getByText('batch settled')).toBeVisible()
})

test('one-cell 429 leaves the rest of the batch running', async ({ page }) => {
  await page.goto('/')
  await setBatch(page, '4')
  await generate(page, 'demo:shed-one a red fox in falling snow')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope).toBeVisible()
  await expect(envelope.getByText('QUEUE_SATURATED')).toBeVisible()
  // the other three cells complete despite the shed cell
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(3, {
    timeout: 45_000,
  })
  await expect(envelope).toHaveCount(1)
  // after the Retry-After countdown, retrying only that cell fills the grid
  const retry = envelope.getByRole('button', { name: 'retry cell' })
  await expect(retry).toBeEnabled({ timeout: 15_000 })
  await retry.click()
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(4, {
    timeout: 30_000,
  })
})

async function enterSweep(page: Page) {
  await page
    .getByRole('group', { name: 'Mode' })
    .getByRole('button', { name: 'sweep' })
    .click()
}

test('entering sweep with dice auto-locks a fresh seed, visibly', async ({ page }) => {
  await page.goto('/')
  // default state: dice active, no seed
  await expect(
    page.getByRole('button', { name: 'dice', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await enterSweep(page)
  await expect(page.getByRole('button', { name: 'lock' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  const seed = await page.getByRole('spinbutton', { name: 'Seed' }).inputValue()
  expect(seed).not.toBe('')
  expect(Number(seed)).toBeGreaterThanOrEqual(0)
  await expect(
    page.getByText('sweep locks the seed — one variable at a time'),
  ).toBeVisible()
})

test('sweep varies exactly one param, seed locked and constant, summary row', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window)
    const posts: unknown[] = []
    ;(window as unknown as { __posts: unknown[] }).__posts = posts
    window.fetch = async (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof Request
            ? input.url
            : String(input)
      if (url.endsWith('/v1/jobs') && init?.method === 'POST' && init.body) {
        posts.push(JSON.parse(String(init.body)))
      }
      return orig(input, init)
    }
  })
  await page.goto('/')
  await enterSweep(page)
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(4, {
    timeout: 45_000,
  })

  const posts = (await page.evaluate(
    () => (window as unknown as { __posts: unknown[] }).__posts,
  )) as {
    num_inference_steps: number
    seed?: number
    width: number
    height: number
    guidance_scale: number
  }[]
  expect(posts).toHaveLength(4)
  // the varied param, in cell order
  expect(posts.map((p) => p.num_inference_steps)).toEqual([4, 12, 20, 28])
  // everything else held constant, seed locked to the rail's value
  const seed = await page.getByRole('spinbutton', { name: 'Seed' }).inputValue()
  for (const p of posts) {
    expect(p.seed).toBe(Number(seed))
    expect(p.width).toBe(1024)
    expect(p.height).toBe(1024)
    expect(p.guidance_scale).toBe(3.5)
  }

  // per-cell stamps carry the varied value
  const stamps = await page.getByTestId('sweep-stamp').allInnerTexts()
  expect(stamps).toHaveLength(4)
  expect(stamps.map((s) => s.split('·')[0]?.trim())).toEqual([
    '4 steps',
    '12 steps',
    '20 steps',
    '28 steps',
  ])

  // summary row: value→measured wall for each cell, copyable
  const summary = page.getByTestId('sweep-summary')
  await expect(summary).toContainText(
    /4→\d+\.\ds · 12→\d+\.\ds · 20→\d+\.\ds · 28→\d+\.\ds/,
  )

  // ledger: the sweep chip surfaces each cell's varied value
  await page.getByRole('link', { name: 'ledger' }).click()
  await expect(page.getByText(/sweep [0-9a-f]{4} · 4 steps/)).toBeVisible()
  await expect(page.getByText(/sweep [0-9a-f]{4} · 28 steps/)).toBeVisible()
})

test('sweep complete: 2×2 grid with four stamps', async ({ page }) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await enterSweep(page)
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(4, {
    timeout: 45_000,
  })
  await expect(page.getByTestId('sweep-summary')).toBeVisible()
  await expect(page).toHaveScreenshot('sweep-complete.png', {
    mask: [...dynMasks(page), page.getByRole('img', { name: 'Generated image' })],
  })
})

test('gallery grid', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  await expect(page.getByText('reproducibility ledger')).toBeVisible()
  await expect(page.locator('article')).toHaveCount(12)
  // wait for the sample images to finish decoding so masks land on stable cells
  await expect
    .poll(
      () =>
        page.evaluate(() =>
          [...document.querySelectorAll<HTMLImageElement>('article img')].filter(
            (img) => img.complete && img.naturalWidth > 0,
          ).length,
        ),
      { timeout: 20_000 },
    )
    .toBeGreaterThanOrEqual(10)
  await expect(page).toHaveScreenshot('gallery.png', {
    mask: [...dynMasks(page), page.locator('article img')],
  })
})

test('gallery detail overlay', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  await page
    .getByRole('button', { name: /Open details: a red fox/ })
    .click()
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('job metadata')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'copy as curl' })).toBeVisible()
  // the full JobView fetch fills format + model rows
  await expect(dialog.getByText(/black-forest-labs/)).toBeVisible()
  await expect(page).toHaveScreenshot('detail-overlay.png', {
    mask: [...dynMasks(page), dialog.locator('img')],
  })
})

test('detail overlay traps focus and restores it to the opener', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  const opener = page.getByRole('button', { name: /Open details: a red fox/ })
  await opener.click()
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/black-forest-labs/)).toBeVisible()

  // a full Tab cycle stays inside the dialog and reaches copy as curl
  let sawCurl = false
  for (let i = 0; i < 20; i++) {
    await page.keyboard.press('Tab')
    const inside = await page.evaluate(() => {
      const open = document.querySelector('dialog[open]')
      return open !== null && open.contains(document.activeElement)
    })
    expect(inside, `tab ${i + 1} escaped the dialog`).toBe(true)
    if (
      await dialog
        .getByRole('button', { name: 'copy as curl' })
        .evaluate((el) => el === document.activeElement)
    ) {
      sawCurl = true
      break
    }
  }
  expect(sawCurl, 'copy as curl never received focus').toBe(true)

  // background is inert while the dialog is open: focus() on it is a no-op
  expect(
    await page.getByRole('link', { name: 'ledger' }).evaluate((el) => {
      ;(el as HTMLElement).focus()
      return document.activeElement === el
    }),
  ).toBe(false)

  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(opener).toBeFocused()
})

test('a11y: progressbar with valuenow, throttled status, no live timelines', async ({
  page,
}) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'demo:hold a red fox in falling snow')
  const bar = page.getByRole('progressbar', { name: 'Denoising progress' })
  await expect(bar).toBeVisible({ timeout: 15_000 })
  await expect(bar).toHaveAttribute('aria-valuenow', '60')
  // the stride-throttled announcement, not the second-ticking rows
  await expect(page.getByRole('status').filter({ hasText: 'inference 60%' })).toHaveCount(1)
  await expect(page.locator('[aria-live]')).toHaveCount(0)
})

test('error envelope announces as an alert', async ({ page }) => {
  await page.goto('/')
  await generate(page, 'demo:blocked thing')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope).toBeVisible({ timeout: 15_000 })
  await expect(envelope).toHaveRole('alert')
})

test('Retry-After countdown keeps ticking while sibling cells poll', async ({
  page,
}) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await setBatch(page, '4')
  // demo:shed-one 429s cell 1; demo:mixed keeps other cells polling (hold +
  // queued) so the parent re-renders every poll — the countdown must tick
  // through that
  await generate(page, 'demo:shed-one demo:mixed a red fox in falling snow')
  const retry = page.getByRole('button', { name: /retry in \d+s/ })
  await expect(retry).toBeVisible()
  const first = Number((await retry.innerText()).replace(/\D/g, ''))
  await expect
    .poll(
      async () => Number((await retry.innerText()).replace(/\D/g, '')),
      { timeout: 6000 },
    )
    .toBeLessThan(first)
})

test('retry after 429 reuses the idempotency key', async ({ page }) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window)
    const keys: (string | null)[] = []
    ;(window as unknown as { __idemKeys: (string | null)[] }).__idemKeys = keys
    window.fetch = async (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof Request
            ? input.url
            : String(input)
      if (url.endsWith('/v1/jobs') && init?.method === 'POST') {
        keys.push(new Headers(init.headers).get('Idempotency-Key'))
      }
      return orig(input, init)
    }
  })
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'demo:shed-one a red fox in falling snow')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope.getByText('QUEUE_SATURATED')).toBeVisible()
  const retry = envelope.getByRole('button', { name: 'retry' })
  await expect(retry).toBeEnabled({ timeout: 15_000 })
  await retry.click()
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  const keys = (await page.evaluate(
    () => (window as unknown as { __idemKeys: (string | null)[] }).__idemKeys,
  ))
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBeTruthy()
  expect(keys[1]).toBe(keys[0])
})

test('idempotent replay after a dropped response renders the replayed UI', async ({
  page,
}) => {
  await page.goto('/')
  await lockSeed(page, '42')
  // mock: the create lands but the first response is lost; the key-reusing
  // retry replays the finished job as a 200
  await generate(page, 'demo:drop-first a red fox in falling snow')
  const envelope = page.getByTestId('error-envelope')
  await expect(envelope.getByText('UPSTREAM_RESET')).toBeVisible()
  // give the mock job time to finish server-side so the replay is terminal
  await page.waitForTimeout(6000)
  await envelope.getByRole('button', { name: 'retry' }).click()
  await expect(page.getByText('replayed · wall not observed')).toBeVisible({
    timeout: 15_000,
  })
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible()
})

test('detail overlay reuses the gallery card blob fetch', async ({ page }) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window)
    const counts: Record<string, number> = {}
    ;(window as unknown as { __imageFetches: Record<string, number> }).__imageFetches =
      counts
    window.fetch = async (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof Request
            ? input.url
            : String(input)
      if (/\/v1\/jobs\/[^/]+\/image$/.test(url)) {
        counts[url] = (counts[url] ?? 0) + 1
      }
      return orig(input, init)
    }
  })
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  await expect(page.locator('article')).toHaveCount(12)
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await page.getByRole('button', { name: /Open details: a red fox/ }).click()
  await expect(dialog.getByText(/black-forest-labs/)).toBeVisible()
  // remount JobImage for the same job by stepping away and back
  await page.keyboard.press('ArrowRight')
  await expect(dialog.getByText('2 / 12')).toBeVisible()
  await page.keyboard.press('ArrowLeft')
  await expect(dialog.getByText('1 / 12')).toBeVisible()
  await page.keyboard.press('Escape')
  const counts = await page.evaluate(
    () =>
      (window as unknown as { __imageFetches: Record<string, number> })
        .__imageFetches,
  )
  const foxUrl = Object.keys(counts).find((u) => u.includes('7f3a91c4'))
  expect(foxUrl).toBeTruthy()
  expect(counts[foxUrl!]).toBe(1)
})

test('api requests carry an abort signal (30s timeout)', async ({ page }) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window)
    const signals: boolean[] = []
    ;(window as unknown as { __signals: boolean[] }).__signals = signals
    window.fetch = async (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof Request
            ? input.url
            : String(input)
      if (url.includes('/v1/jobs')) signals.push(Boolean(init?.signal))
      return orig(input, init)
    }
  })
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  await expect(page.locator('article')).toHaveCount(12)
  const signals = await page.evaluate(
    () => (window as unknown as { __signals: boolean[] }).__signals,
  )
  expect(signals.length).toBeGreaterThan(0)
  expect(signals.every(Boolean)).toBe(true)
})

test('key popover: aria wiring and focus restored on Esc', async ({ page }) => {
  await page.goto('/')
  const trigger = page.getByRole('button', { name: 'key set' })
  await expect(trigger).toHaveAttribute('aria-haspopup', 'dialog')
  await trigger.click()
  await expect(trigger).toHaveAttribute('aria-controls', 'key-popover')
  await expect(page.getByText('gateway api key')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByText('gateway api key')).not.toBeVisible()
  await expect(trigger).toBeFocused()
})

/** Text/background contrast sampled from a rendered screenshot of the element. */
async function sampledContrast(page: Page, locator: Locator): Promise<number> {
  const b64 = (await locator.screenshot()).toString('base64')
  return page.evaluate(async (data64) => {
    const img = new Image()
    img.src = `data:image/png;base64,${data64}`
    await img.decode()
    const canvas = document.createElement('canvas')
    canvas.width = img.width
    canvas.height = img.height
    const ctx = canvas.getContext('2d')!
    ctx.drawImage(img, 0, 0)
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
    const lumOf = (r: number, g: number, b: number) => {
      const f = (v: number) => {
        v /= 255
        return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
      }
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    }
    const freq = new Map<number, number>()
    let textLum = 0
    for (let i = 0; i < data.length; i += 4) {
      const key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
      freq.set(key, (freq.get(key) ?? 0) + 1)
      // light-on-dark UI: the brightest pixel is the text core
      textLum = Math.max(textLum, lumOf(data[i], data[i + 1], data[i + 2]))
    }
    let bgKey = 0
    let bgCount = 0
    for (const [key, count] of freq) {
      if (count > bgCount) {
        bgCount = count
        bgKey = key
      }
    }
    const bgLum = lumOf((bgKey >> 16) & 255, (bgKey >> 8) & 255, bgKey & 255)
    const [hi, lo] = textLum > bgLum ? [textLum, bgLum] : [bgLum, textLum]
    return (hi + 0.05) / (lo + 0.05)
  }, b64)
}

test('unselected segment options meet 4.5:1 contrast', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  // all unselected in the default state: dice/png/batch-1 are active
  const targets = [
    page.getByRole('button', { name: 'lock' }),
    page.getByRole('button', { name: 'jpeg' }),
    page.getByRole('group', { name: 'Batch size' }).getByRole('button', { name: '2', exact: true }),
    page.getByRole('group', { name: 'Batch size' }).getByRole('button', { name: '4', exact: true }),
  ]
  for (const target of targets) {
    expect(await sampledContrast(page, target)).toBeGreaterThanOrEqual(4.5)
  }
})

// the label tier (--color-ink-label) carries every micro-label, caption and
// footer readout; at 0.47 L it measured 2.8:1 and was the least legible text
// in the app
test('the micro-label tier meets 4.5:1 contrast', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  const targets = [
    page.locator('footer .microlabel').first(),
    page.locator('footer').getByText('mock', { exact: true }),
    page.locator('#params-rail .microlabel').first(),
    page.locator('#params-rail').getByText('jobs active'),
    page.locator('#params-rail figcaption'),
  ]
  for (const target of targets) {
    expect(await sampledContrast(page, target)).toBeGreaterThanOrEqual(4.5)
  }
})

test.describe('negative-offset timezone', () => {
  test.use({ timezoneId: 'Pacific/Honolulu' })

  test('gallery date headings match the ISO day, with a labeled count', async ({
    page,
  }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'ledger' }).click()
    // '2026-08-09' must render as Aug 09 even at UTC-10; the UTC-parse bug
    // shifted every heading back one day
    const heading = page.getByRole('heading', { name: /Aug 09, 2026/ })
    await expect(heading).toBeVisible()
    await expect(heading).toContainText('· 4 jobs')
    await expect(page.getByRole('heading', { name: /Aug 06, 2026/ })).toBeVisible()
  })
})

// --- routing ---

const FOX_JOB_ID = '7f3a91c4-88e2-4d17-9b60-4be1f0d2a6ce'

test('tabs are real links; back and forward traverse views', async ({ page }) => {
  await page.goto('/')
  const ledgerTab = page.getByRole('link', { name: 'ledger' })
  await expect(ledgerTab).toHaveAttribute('href', '/gallery')
  await ledgerTab.click()
  await expect(page).toHaveURL('/gallery')
  await expect(page.getByText('reproducibility ledger')).toBeVisible()
  await page.getByRole('link', { name: 'generate' }).click()
  await expect(page).toHaveURL('/generate')
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL('/gallery')
  await expect(page.getByText('reproducibility ledger')).toBeVisible()
  await page.goBack()
  await expect(page.getByLabel('Prompt')).toBeVisible()
  await page.goForward()
  await expect(page).toHaveURL('/gallery')
})

test('unknown paths redirect to /generate', async ({ page }) => {
  await page.goto('/no/such/path')
  await expect(page).toHaveURL('/generate')
  await expect(page.getByLabel('Prompt')).toBeVisible()
})

test('opening a detail dialog pushes /jobs/:id; back closes it', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'ledger' }).click()
  await page.getByRole('button', { name: /Open details: a red fox/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog).toBeVisible()
  await expect(page).toHaveURL(`/jobs/${FOX_JOB_ID}`)
  await page.goBack()
  await expect(dialog).not.toBeVisible()
  await expect(page).toHaveURL('/gallery')
  await page.goForward()
  await expect(dialog).toBeVisible()
  // Esc pops the pushed entry instead of stranding it in history
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(page).toHaveURL('/gallery')
})

test('/jobs/:id deep link opens the ledger with the detail dialog', async ({ page }) => {
  await page.goto(`/jobs/${FOX_JOB_ID}`)
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog).toBeVisible()
  await expect(
    dialog.getByText('a red fox in falling snow, cinematic lighting'),
  ).toBeVisible()
  // deep link has no in-app history: close replaces with the ledger
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(page).toHaveURL('/gallery')
  await expect(page.getByText('reproducibility ledger')).toBeVisible()
})

test('deep link to an unknown job falls back to the ledger', async ({ page }) => {
  await page.goto('/jobs/ffffffff-0000-0000-0000-000000000000')
  await expect(page).toHaveURL('/gallery')
  await expect(page.getByRole('dialog', { name: 'Job details' })).not.toBeVisible()
})

// --- operate ---

/** Counts every /v1/metrics request the page makes. */
async function countMetricsRequests(page: Page) {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window)
    ;(window as unknown as { __metrics: number }).__metrics = 0
    window.fetch = async (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof Request
            ? input.url
            : String(input)
      if (url.includes('/v1/metrics')) {
        const w = window as unknown as { __metrics: number }
        w.__metrics += 1
      }
      return orig(input, init)
    }
  })
}

function metricsCount(page: Page) {
  return page.evaluate(() => (window as unknown as { __metrics: number }).__metrics)
}

/** Fires a visibilitychange with document.visibilityState forced to `state`. */
async function setVisibility(page: Page, state: 'visible' | 'hidden') {
  await page.evaluate((value) => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => value,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)
}

test('operate dashboard', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('MOCK_HOUR', '23'))
  await page.goto('/')
  await lockSeed(page, '42')
  // demo:hold parks a job IN_PROGRESS: the endpoint strip reads a busy worker
  await generate(page, 'demo:hold a red fox in falling snow')
  await expect(page.getByTestId('preview-frame').first()).toBeVisible({
    timeout: 15_000,
  })
  await page.getByRole('link', { name: 'operate' }).click()
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await expect(page.getByTestId('queue-staleness')).toHaveText(
    'queue reading 4.2 s old · stale past 30 s',
  )
  await expect(page).toHaveScreenshot('operate.png', { mask: dynMasks(page) })
})

/** Reads back the three terms the cost basis puts on screen. */
async function costBasis(page: Page) {
  const text = await page.getByTestId('cost-basis').innerText()
  const m = text.match(
    /basis: ([\d.]+) exec-seconds × \$([\d.]+)\/GPU-hr ÷ 3600 = \$([\d.]+)/,
  )
  if (m === null) throw new Error(`cost basis unparseable: ${text}`)
  return { execS: Number(m[1]), rate: Number(m[2]), usd: Number(m[3]) }
}

test('the cost estimate shows the identity it was computed from', async ({
  page,
}) => {
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  const { execS, rate, usd } = await costBasis(page)
  expect(execS).toBeGreaterThan(0)
  expect(rate).toBe(1.75)
  // the displayed estimate is exactly what the displayed terms produce
  expect(usd).toBeCloseTo((execS * rate) / 3600, 4)
  // and it is the same number the readout shows
  await expect(
    page
      .getByRole('region', { name: 'cost' })
      .getByText(`$${usd.toFixed(4)}`, { exact: true }),
  ).toHaveCount(1)
  // a real span, so the window total is also expressed as a rate
  await expect(page.getByTestId('cost-rate')).toContainText(/\$[\d.]+\/h$/)
})

test('a one-job window states no rate: one sample is not a rate', async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem('MOCK_METRICS', 'single'))
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await expect(page.getByTestId('window-span')).toHaveText(
    'newest 1 completed · one instant',
  )
  // the basis identity still holds for the single job
  const { execS, rate, usd } = await costBasis(page)
  expect(usd).toBeCloseTo((execS * rate) / 3600, 4)
  // ...but nothing is extrapolated from a zero-length window
  await expect(page.getByTestId('cost-rate')).toHaveCount(0)
})

test('the window states the stretch of time it covers, not just a count', async ({
  page,
}) => {
  await page.goto('/operate')
  await expect(page.getByTestId('window-span')).toHaveText(
    /^newest \d+ completed · [\d.]+ (s|min|h|d) span$/,
  )
  const span = await page.getByTestId('window-span').innerText()
  await expect(page.getByTestId('cost-rate')).toContainText(
    span.split('· ')[1].replace(' span', ''),
  )
})

test('an empty window says so rather than inventing a span', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('MOCK_METRICS', 'empty'))
  await page.goto('/operate')
  await expect(page.getByTestId('window-span')).toHaveText(
    'newest 0 completed · no window yet',
  )
  await expect(page.getByTestId('cost-rate')).toHaveCount(0)
  const { execS, usd } = await costBasis(page)
  expect(execS).toBe(0)
  expect(usd).toBe(0)
})

test('an old reading the gateway still vouches for is not dressed as stale', async ({
  page,
}) => {
  // 47.3 s old — past the 30 s threshold the frontend used to hardcode — but
  // the gateway says stale: false, and the gateway's verdict is the only one
  await page.addInitScript(() =>
    localStorage.setItem('MOCK_METRICS', 'old-trusted'),
  )
  await page.goto('/operate')
  const staleness = page.getByTestId('queue-staleness')
  await expect(staleness).toHaveText('queue reading 47.3 s old · stale past 30 s')
  await expect(staleness).not.toContainText('STALE')
  await expect(staleness.locator('.text-warn')).toHaveCount(0)
  // and the queue readouts keep full weight: they are still to be trusted
  await expect(page.locator('.display-number.text-ink-faint')).toHaveCount(0)
})

test('an absent queue reading reads unknown, not zero', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('MOCK_METRICS', 'unknown'))
  await page.goto('/operate')
  const staleness = page.getByTestId('queue-staleness')
  await expect(staleness).toContainText('STALE')
  await expect(staleness).toContainText('unknown, not zero')
  // the four queue readouts are em dashes, never zeros standing in for a reading
  const endpoint = page.getByRole('region', { name: 'endpoint' })
  await expect(endpoint.locator('.display-number', { hasText: '—' })).toHaveCount(4)
  await expect(page.getByTestId('reconciler-liveness')).toContainText(
    'reconciler unknown · no tick observed · stale past 30 s',
  )
})

test('operate zero state: honest zeros, no fabricated data', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('MOCK_HOUR', '23')
    localStorage.setItem('MOCK_METRICS', 'empty')
  })
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await expect(
    page.getByText(/no completed jobs in the window yet/),
  ).toBeVisible()
  await expect(
    page.getByText(/no completions in the last 24 h/),
  ).toBeVisible()
  // percentiles have no samples: em dashes, never zeros pretending to be timings
  // 6 table cells + 4 bar readouts, all em dashes: no zeros posing as timings
  await expect(
    page.getByRole('region', { name: 'latency' }).getByText('—'),
  ).toHaveCount(10)
  await expect(page).toHaveScreenshot('operate-zero.png', { mask: dynMasks(page) })
})

test('operate polls on a 10s loop and pauses while the tab is hidden', async ({
  page,
}) => {
  await countMetricsRequests(page)
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  await expect.poll(() => metricsCount(page)).toBe(1)

  await setVisibility(page, 'hidden')
  // more than one poll interval passes with the tab backgrounded
  await page.waitForTimeout(12_000)
  expect(await metricsCount(page)).toBe(1)

  // returning to the tab refetches immediately rather than showing a stale reading
  await setVisibility(page, 'visible')
  await expect.poll(() => metricsCount(page)).toBe(2)
})

test('operate stops polling when another view is showing', async ({ page }) => {
  await countMetricsRequests(page)
  await page.goto('/operate')
  await expect.poll(() => metricsCount(page)).toBe(1)
  await page.getByRole('link', { name: 'generate' }).click()
  await page.waitForTimeout(12_000)
  expect(await metricsCount(page)).toBe(1)
})

test('a stale queue reading says so', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('MOCK_METRICS', 'stale'))
  await page.goto('/operate')
  const staleness = page.getByTestId('queue-staleness')
  await expect(staleness).toContainText('STALE')
  await expect(staleness).toContainText('47.3 s old')
  // the threshold behind the verdict is the gateway's, and it is on screen
  await expect(staleness).toContainText('stale past 30 s')
  await expect(staleness).toContainText('last cached poll, not live')
  // the reconciler's own liveness is reported next to it
  await expect(page.getByTestId('reconciler-liveness')).toHaveText(
    'reconciler stalled · last tick 96.4 s ago · stale past 30 s',
  )
  // a stale reading dims the four queue readouts it covers
  await expect(page.locator('.display-number.text-ink-faint')).toHaveCount(4)
})

test('operate without a key renders the no-key state', async ({ page }) => {
  await page.addInitScript(() => localStorage.removeItem('gateway-key'))
  await page.goto('/operate')
  // the hidden generate/ledger views render their own no-key cards; assert the
  // one inside the visible view
  const operate = page.locator('main > div.h-full')
  await expect(operate.getByText('no api key')).toBeVisible()
  await expect(page.getByText('endpoint telemetry')).toHaveCount(0)
  await operate.getByRole('button', { name: /use demo key/ }).click()
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
})

test('operate counts match the ledger, and link into it filtered', async ({
  page,
}) => {
  await page.goto('/operate')
  await expect(page.getByText('endpoint telemetry')).toBeVisible()
  const ledger = await page.evaluate(async () => {
    const res = await fetch('/v1/jobs?limit=100', {
      headers: { Authorization: `Bearer ${localStorage.getItem('gateway-key')}` },
    })
    const body = (await res.json()) as { jobs: { status: string }[] }
    const counts: Record<string, number> = {}
    for (const job of body.jobs) counts[job.status] = (counts[job.status] ?? 0) + 1
    return counts
  })
  const panel = page.getByRole('link', { name: /^completed/ })
  await expect(panel).toContainText(String(ledger.COMPLETED))
  await expect(page.getByRole('link', { name: /^failed/ })).toContainText(
    String(ledger.FAILED),
  )
  await expect(page.getByRole('link', { name: /^blocked/ })).toContainText(
    String(ledger.BLOCKED),
  )

  // the link opens the ledger already filtered to that status
  await page.getByRole('link', { name: /^failed/ }).click()
  await expect(page).toHaveURL('/gallery?status=FAILED')
  await expect(page.locator('article')).toHaveCount(ledger.FAILED)
  await expect(page.getByText('status FAILED ×')).toBeVisible()
  // clearing the filter clears the query too
  await page.getByText('status FAILED ×').click()
  await expect(page).toHaveURL('/gallery')
  await expect(page.locator('article')).toHaveCount(12)
})

// --- honest reproduction ---

test('rerun seed restores the job record, not rail state', async ({ page }) => {
  await page.goto('/')
  // poison the rail: a rerun must not inherit these
  await page.getByLabel('Inference steps').evaluate((el) => {
    const input = el as HTMLInputElement
    // React value tracking swallows plain .value writes; use the native setter
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(
      input,
      '50',
    )
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await expect(page.getByRole('button', { name: 'steps 50' })).toBeVisible()
  await page.getByRole('button', { name: 'jpeg' }).click()
  await page.getByRole('link', { name: 'ledger' }).click()
  // macro job record: 512×768, 20 steps, g 3.5, seed 555, png
  await page.getByRole('button', { name: /Open details: macro shot/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog.getByText('job metadata')).toBeVisible()
  await dialog.getByRole('button', { name: 'rerun seed' }).click()
  // rerun from detail closes the dialog and lands on /generate
  await expect(dialog).not.toBeVisible()
  await expect(page).toHaveURL('/generate')
  await expect(page.getByRole('button', { name: 'size 512×768' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'steps 20' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'guidance 3.5' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'seed 555', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'format png', exact: true })).toBeVisible()
  await expect(page.getByRole('spinbutton', { name: 'Seed' })).toHaveValue('555')
  await expect(page.getByRole('button', { name: 'lock' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
})

test('copy as curl carries the full recorded request', async ({ page }) => {
  await page.addInitScript(() => {
    ;(navigator.clipboard as { writeText: (t: string) => Promise<void> }).writeText =
      (t: string) => {
        ;(window as unknown as { __copied: string }).__copied = t
        return Promise.resolve()
      }
  })
  await page.goto(`/jobs/${FOX_JOB_ID}`)
  const dialog = page.getByRole('dialog', { name: 'Job details' })
  await expect(dialog.getByText('job metadata')).toBeVisible()
  // metadata table states steps and guidance
  await expect(dialog.getByText('steps', { exact: true })).toBeVisible()
  await expect(dialog.getByText('guidance', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: 'copy as curl' }).click()
  const copied = await page.evaluate(
    () => (window as unknown as { __copied: string }).__copied,
  )
  expect(copied).toContain('"num_inference_steps":28')
  expect(copied).toContain('"guidance_scale":3.5')
  expect(copied).toContain('"seed":42')
  expect(copied).toContain('"output_format":"png"')
})

test('seed chip on a completed cell adopts the seed as locked', async ({ page }) => {
  await page.goto('/')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
  const seedText = await page
    .getByTestId('cell-seed')
    .getByRole('button', { name: /^\d+$/ })
    .innerText()
  await page.getByRole('button', { name: 'adopt' }).click()
  await expect(page.getByRole('spinbutton', { name: 'Seed' })).toHaveValue(seedText)
  await expect(page.getByRole('button', { name: 'lock' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
})

// --- mock physics ---

/** All completed jobs the mock reports, fetched with the page's key. */
async function fetchSummaries(page: Page) {
  return page.evaluate(async () => {
    const res = await fetch('/v1/jobs?limit=100', {
      headers: { Authorization: `Bearer ${localStorage.getItem('gateway-key')}` },
    })
    const body = (await res.json()) as {
      jobs: {
        status: string
        inference_seconds: number | null
        created_at: string
        completed_at: string | null
      }[]
    }
    return body.jobs
  })
}

test('inference_seconds never exceeds wall, across scenarios', async ({ page }) => {
  await page.goto('/')
  await setBatch(page, '4')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(4, {
    timeout: 45_000,
  })
  // per-cell captions: wall ≥ inf for every completed cell
  // .panel = cell captions; the rail's sparkline caption is also a figcaption
  for (const caption of await page.locator('figcaption.panel').allInnerTexts()) {
    const wall = /wall\s*([\d.]+) s/i.exec(caption)
    const inf = /inf(?:erence)?\s*([\d.]+) s/i.exec(caption)
    expect(wall).toBeTruthy()
    expect(inf).toBeTruthy()
    expect(Number(inf![1])).toBeLessThanOrEqual(Number(wall![1]))
  }
  // API-level invariant over everything the mock has served, static ledger included
  const jobs = await fetchSummaries(page)
  expect(jobs.length).toBeGreaterThanOrEqual(16)
  for (const job of jobs) {
    if (job.status !== 'COMPLETED' || job.inference_seconds === null) continue
    const wallS =
      (new Date(job.completed_at!).getTime() - new Date(job.created_at).getTime()) /
      1000
    expect(job.inference_seconds).toBeLessThanOrEqual(wallS)
  }
})

test('cold run: split persists on the caption, hollow sparkline point', async ({
  page,
}) => {
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'demo:cold fox, dawn light')
  await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
  const split = page.getByTestId('cold-split')
  await expect(split).toBeVisible()
  await expect(split).toContainText(/queued [\d.]+ s \(likely cold\) · inference [\d.]+ s/)
  await expect(split.locator('[title*="scale-from-zero"]')).toBeVisible()
  // the sparkline renders the cold wall time as a hollow point
  await expect(page.locator('svg rect[data-cold]')).toHaveCount(1)
  // and inference on the caption stays inside the wall
  const caption = await page.locator('figcaption.panel').innerText()
  const wall = Number(/wall\s*([\d.]+) s/i.exec(caption)![1])
  const inf = Number(/inference\s*([\d.]+) s/i.exec(caption)![1])
  expect(inf).toBeLessThanOrEqual(wall)
})

test('demo tokens require the demo: prefix', async ({ page }) => {
  await page.goto('/')
  // plain english containing "cold" and "blocked" is just a prompt
  await generate(page, 'cold beer on a blocked road')
  await expect(page.getByRole('img', { name: 'Generated image' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByText(/demo scenario/)).not.toBeVisible()
  // the prefixed token triggers the scenario and stamps the nameplate
  await generate(page, 'demo:cold fox, dawn light')
  await expect(page.getByText('demo scenario: cold')).toBeVisible()
  await expect(
    page.getByTestId('stage-timeline').getByText(/cold start/),
  ).toBeVisible({ timeout: 15_000 })
})

test('mixed batch settles terminally: done, OOM and timeout', async ({ page }) => {
  await page.goto('/')
  await setBatch(page, '4')
  await generate(page, 'demo:mixed a red fox in falling snow')
  const timeline = page.getByTestId('batch-timeline')
  await expect(timeline).toBeVisible()
  await expect(timeline.getByText('batch settled')).toBeVisible({ timeout: 35_000 })
  await expect(page.getByRole('img', { name: 'Generated image' })).toHaveCount(1)
  await expect(page.getByText('OOM', { exact: true })).toBeVisible()
  await expect(page.getByText('TIMED_OUT').first()).toBeVisible()
  // the OOM copy is templated from the actual request dims
  await expect(page.getByText(/CUDA out of memory while denoising at 1024×1024, 28 steps/)).toBeVisible()
})

async function imageFingerprint(page: Page): Promise<string> {
  const src = await page
    .getByRole('img', { name: 'Generated image' })
    .getAttribute('src')
  expect(src).toBeTruthy()
  return page.evaluate(async (url) => {
    const res = await fetch(url!)
    const bytes = new Uint8Array(await res.arrayBuffer())
    let hash = 0
    for (const b of bytes) hash = (hash * 31 + b) >>> 0
    return `${bytes.length}:${hash}`
  }, src)
}

test('rerun with the same seed matches bytes; a different seed differs', async ({
  page,
}) => {
  test.setTimeout(90_000)
  await page.goto('/')
  await lockSeed(page, '42')
  await generate(page, 'a red fox in falling snow, cinematic lighting')
  await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
  const first = await imageFingerprint(page)

  // rerun, seed still locked at 42: provably the same image
  await page.getByRole('button', { name: /Generate/ }).click()
  await expect(page.getByTestId('stage-timeline')).toBeVisible()
  await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
  const rerun = await imageFingerprint(page)
  expect(rerun).toBe(first)

  // reroll onto a neighboring seed: provably different pixels
  await page.getByRole('spinbutton', { name: 'Seed' }).fill('43')
  await page.getByRole('button', { name: /Generate/ }).click()
  await expect(page.getByTestId('stage-timeline')).toBeVisible()
  await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
  const rerolled = await imageFingerprint(page)
  expect(rerolled).not.toBe(first)
})

// --- thesis + pre-key ---

test('empty cell: thesis card starts the guided steps sweep', async ({ page }) => {
  await page.goto('/')
  await expect(
    page.getByText('This is an instrument, not a gallery', { exact: false }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'run the steps experiment' }).click()
  // a steps sweep on the fox example with a locked seed, no typing required
  await expect(page.getByLabel('Prompt')).toHaveValue(
    'a red fox in falling snow, cinematic lighting',
  )
  await expect(page.getByText('4 steps', { exact: true })).toBeVisible()
  await expect(page.getByText('28 steps', { exact: true })).toBeVisible()
  await expect(page.getByTestId('batch-timeline')).toBeVisible()
  await expect(page.getByTestId('batch-timeline')).toContainText('sweep timeline')
  const seed = await page.getByRole('spinbutton', { name: 'Seed' }).inputValue()
  expect(seed).not.toBe('')
})

test('pre-key: prompt bar visibly disabled, demo key is one click', async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.removeItem('gateway-key'))
  await page.goto('/')
  await expect(page.getByLabel('Prompt')).toBeDisabled()
  // the hidden ledger view renders its own no-key card; assert the visible one
  await expect(page.getByText('no api key').first()).toBeVisible()
  // "set key" is a real button that opens the header popover
  await page.getByRole('button', { name: 'set key', exact: true }).nth(1).click()
  await expect(page.getByText('gateway api key')).toBeVisible()
  await page.keyboard.press('Escape')
  // the one-click demo key unlocks the instrument
  await page.getByRole('button', { name: /use demo key/ }).first().click()
  await expect(page.getByLabel('Prompt')).toBeEnabled()
  await expect(page.locator('footer').getByText('set', { exact: true })).toBeVisible()
})

const RESPONSIVE_VIEWPORTS = [
  { width: 375, height: 667 },
  { width: 768, height: 1024 },
]

for (const viewport of RESPONSIVE_VIEWPORTS) {
  const tag = `${viewport.width}x${viewport.height}`
  test.describe(`viewport ${tag}`, () => {
    test.use({ viewport })

    test(`empty + completed + gallery at ${tag}`, async ({ page }) => {
      await page.goto('/')
      await expect(page.getByLabel('Prompt')).toBeVisible()
      // no horizontal overflow: the rail must not push the canvas offscreen
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true)
      // below lg the rail is a drawer behind the header params button
      const rail = page.locator('#params-rail')
      await expect(rail).toBeHidden()
      const toggle = page.getByRole('button', { name: 'params' })
      await toggle.click()
      await expect(rail).toBeVisible()
      await page.getByRole('button', { name: 'lock' }).click()
      await page.getByRole('spinbutton', { name: 'Seed' }).fill('42')
      await page.keyboard.press('Escape')
      await expect(rail).toBeHidden()
      await expect(page).toHaveScreenshot(`empty-${tag}.png`, {
        mask: dynMasks(page),
      })

      // gallery before generating: only fixed-date sample jobs in the baseline
      await page.getByRole('link', { name: 'ledger' }).click()
      await expect(page.locator('article')).toHaveCount(12)
      await expect
        .poll(
          () =>
            page.evaluate(() =>
              [...document.querySelectorAll<HTMLImageElement>('article img')].filter(
                (img) => img.complete && img.naturalWidth > 0,
              ).length,
            ),
          { timeout: 20_000 },
        )
        .toBeGreaterThanOrEqual(10)
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true)
      await expect(page).toHaveScreenshot(`gallery-${tag}.png`, {
        mask: [...dynMasks(page), page.locator('article img')],
      })

      await page.getByRole('link', { name: 'generate' }).click()
      await generate(page, 'a red fox in falling snow, cinematic lighting')
      await expect(page.getByText(/bench p50/)).toBeVisible({ timeout: 30_000 })
      const image = page.getByRole('img', { name: 'Generated image' })
      await expect(image).toBeVisible()
      await expect(page).toHaveScreenshot(`completed-${tag}.png`, {
        mask: [...dynMasks(page), image],
      })
    })
  })
}
