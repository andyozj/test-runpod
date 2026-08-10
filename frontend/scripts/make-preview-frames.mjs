// Generates public/mock/preview-{1..4}.jpg from public/mock/sample.png:
// four simulated denoising stages (heavy blur+noise → nearly clear), 288px,
// each ≤15kB per the gateway preview contract. Rendered via Playwright
// chromium canvas (no sharp/jimp in the tree). Run manually, commit outputs:
//   node scripts/make-preview-frames.mjs
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const sample = readFileSync(join(root, 'public/mock/sample.png'))

const SIZE = 288
const LIMIT = 15 * 1024
// blur px / noise amplitude / jpeg quality per denoising stage
const FRAMES = [
  { blur: 14, noise: 40, quality: 0.32, seed: 101 },
  { blur: 8, noise: 24, quality: 0.42, seed: 202 },
  { blur: 3.5, noise: 10, quality: 0.55, seed: 303 },
  { blur: 0.8, noise: 3, quality: 0.62, seed: 404 },
]

const browser = await chromium.launch()
const page = await browser.newPage()
const results = await page.evaluate(
  async ({ src, frames, size }) => {
    const img = new Image()
    img.src = src
    await img.decode()
    // cover-crop to square
    const s = Math.min(img.naturalWidth, img.naturalHeight)
    const sx = (img.naturalWidth - s) / 2
    const sy = (img.naturalHeight - s) / 2
    return frames.map((f) => {
      const c = document.createElement('canvas')
      c.width = size
      c.height = size
      const ctx = c.getContext('2d')
      ctx.filter = `blur(${f.blur}px)`
      const pad = Math.ceil(f.blur * 2) // overdraw so blur has no vignette
      ctx.drawImage(img, sx, sy, s, s, -pad, -pad, size + 2 * pad, size + 2 * pad)
      ctx.filter = 'none'
      const d = ctx.getImageData(0, 0, size, size)
      let state = f.seed >>> 0 // deterministic LCG noise
      const rand = () => ((state = (state * 1664525 + 1013904223) >>> 0), state / 2 ** 32)
      for (let i = 0; i < d.data.length; i += 4) {
        const n = (rand() - 0.5) * 2 * f.noise
        d.data[i] += n
        d.data[i + 1] += n
        d.data[i + 2] += n
      }
      ctx.putImageData(d, 0, 0)
      return c.toDataURL('image/jpeg', f.quality).split(',')[1]
    })
  },
  {
    src: `data:image/png;base64,${sample.toString('base64')}`,
    frames: FRAMES,
    size: SIZE,
  },
)
await browser.close()

results.forEach((b64, i) => {
  const bytes = Buffer.from(b64, 'base64')
  if (bytes.length > LIMIT) {
    throw new Error(`preview-${i + 1}.jpg is ${bytes.length}B, over the 15kB contract limit`)
  }
  const out = join(root, `public/mock/preview-${i + 1}.jpg`)
  writeFileSync(out, bytes)
  console.log(`preview-${i + 1}.jpg ${bytes.length}B`)
})
