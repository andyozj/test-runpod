// Generates public/mock/sample-{1..5}.png from public/mock/sample.png:
// five visually distinct, deterministic variants (hue-rotate + offset crop).
// The mock serves sample.png for seed % 6 === 0 and sample-K.png for K = seed % 6,
// so rerun-same-seed provably returns identical bytes and reroll differs.
// Rendered via Playwright chromium canvas (no sharp/jimp in the tree).
// Run manually, commit outputs:
//   node scripts/make-seed-variants.mjs
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const sample = readFileSync(join(root, 'public/mock/sample.png'))

// 768 keeps five committed PNGs near 1MB each; cards and cells render ≤ ~700px
const SIZE = 768
// hue shift deg / crop zoom / crop x/y offset (fractions of the zoom margin)
const VARIANTS = [
  { hue: 60, zoom: 1.06, ox: 0.0, oy: 1.0 },
  { hue: 120, zoom: 1.1, ox: 1.0, oy: 0.0 },
  { hue: 180, zoom: 1.08, ox: 0.5, oy: 0.5 },
  { hue: 240, zoom: 1.12, ox: 1.0, oy: 1.0 },
  { hue: 300, zoom: 1.05, ox: 0.0, oy: 0.0 },
]

const browser = await chromium.launch()
const page = await browser.newPage()
const results = await page.evaluate(
  async ({ src, variants, size }) => {
    const img = new Image()
    img.src = src
    await img.decode()
    return variants.map((v) => {
      const c = document.createElement('canvas')
      c.width = size
      c.height = size
      const ctx = c.getContext('2d')
      const w = img.naturalWidth / v.zoom
      const h = img.naturalHeight / v.zoom
      const sx = (img.naturalWidth - w) * v.ox
      const sy = (img.naturalHeight - h) * v.oy
      ctx.filter = `hue-rotate(${v.hue}deg)`
      ctx.drawImage(img, sx, sy, w, h, 0, 0, size, size)
      return c.toDataURL('image/png').split(',')[1]
    })
  },
  { src: `data:image/png;base64,${sample.toString('base64')}`, variants: VARIANTS, size: SIZE },
)
await browser.close()

results.forEach((b64, i) => {
  const bytes = Buffer.from(b64, 'base64')
  const out = join(root, `public/mock/sample-${i + 1}.png`)
  writeFileSync(out, bytes)
  console.log(`sample-${i + 1}.png ${bytes.length}B`)
})
