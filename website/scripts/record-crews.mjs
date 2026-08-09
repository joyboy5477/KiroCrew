/**
 * Recording harness for Issue Radar → Crews: the escalation-guidance flow.
 *
 * A still frame cannot prove a sequence, and this feature's whole point is a
 * SEQUENCE: a crew stops on a decision, the human reads what it is blocked on
 * and what the crew itself recommends, answers in one box, and the crew resumes.
 * So this records it end to end — expanding the second escalation, accepting the
 * crew's recommendation into the guidance box, sending, and the delivered state.
 *
 * Same fixtures and same stub as capture-crews.mjs (see
 * lib/issue-radar-crews-fixtures.mjs); only the driving differs.
 *
 * Produces webm, and mp4 + GIF when ffmpeg is present (it is, in the Playwright
 * image). Usage: node scripts/record-crews.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { makeExtra, seedState } from './lib/issue-radar-crews-fixtures.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crews')
const NAME = 'crews-guidance-flow'
mkdirSync(OUT, { recursive: true })

const SIZE = { width: 1440, height: 900 }

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: SIZE,
    // deviceScaleFactor stays 1 for video: a 2x frame doubles the encode cost
    // and the GIF is downscaled for the PR anyway.
    recordVideo: { dir: OUT, size: SIZE },
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme: 'dark', extra: makeExtra(json) })
  await page.addInitScript((entries) => {
    for (const [k, v] of Object.entries(entries)) localStorage.setItem(k, v)
  }, seedState({ crewView: { kind: 'desk' }, crewFilter: 'all' }))

  await page.goto(`${base}/issue-radar`, { waitUntil: 'domcontentloaded' })
  // By testid, not copy — the label this used to wait on has been renamed once
  // already.
  await page.locator('[data-testid="crew-desk-row"]').first()
    .waitFor({ state: 'visible', timeout: 20000 })
  // Let the board settle so the first frames show a populated desk rather than
  // a skeleton — the recording is evidence, not a loading demo.
  await page.waitForTimeout(1400)

  // 1. Open the collapsed second escalation. Waits on the expanded card's own
  //    blocked-on panel rather than a timeout, so a slow expand cannot make this
  //    pass on an unexpanded card. Addressed by testid, not by prose: the heading
  //    this used to wait on was removed when the panel became a tinted block that
  //    labels itself, and a wait keyed on copy breaks again the next time it moves.
  const collapsed = page.getByRole('button', { name: /show .*escalation/i }).first()
  if (await collapsed.count()) {
    await collapsed.click()
    await page.locator('[data-testid="crew-desk-question"]').first().waitFor({ state: 'visible' })
    await page.waitForTimeout(900)
  }

  // 2. Take the crew's own recommendation as the answer, which is the common
  //    case — the crew did the reasoning and the human is ratifying it.
  const approve = page.getByRole('button', { name: /approve recommendation/i }).first()
  if (await approve.count()) {
    await approve.click()
    await page.waitForTimeout(700)
  }

  // 3. Add a constraint by hand, typed slowly enough to read on playback.
  const box = page.locator('textarea').first()
  await box.click()
  await box.type(
    'Go with (a), but make the skip explicit in the return value so callers can '
    + 'tell the chmod did not happen.',
    { delay: 18 },
  )
  await page.waitForTimeout(700)

  // 4. Send, and hold on the delivered state.
  const send = page.getByRole('button', { name: /send/i }).first()
  if (await send.count() && await send.isEnabled()) {
    await send.click()
    await page.waitForTimeout(1800)
  }

  await context.close() // flushes the video file
  await browser.close()
  srv.close()

  const webm = readdirSync(OUT).filter((f) => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error('playwright wrote no video')
  const src = join(OUT, `${NAME}.webm`)
  renameSync(join(OUT, webm), src)
  console.log('WEBM', src)

  const ff = (args) => spawnSync('ffmpeg', ['-y', ...args], { stdio: 'ignore' }).status === 0
  const mp4 = join(OUT, `${NAME}.mp4`)
  if (ff(['-i', src, '-movflags', 'faststart', '-pix_fmt', 'yuv420p', '-vf',
          'scale=1280:-2', mp4])) {
    console.log('MP4', mp4)
  }
  // Palette-optimised GIF: a plain -i → .gif is 4-5x larger at worse quality.
  const pal = join(OUT, `${NAME}-palette.png`)
  const gif = join(OUT, `${NAME}.gif`)
  if (ff(['-i', src, '-vf', 'fps=10,scale=1000:-1:flags=lanczos,palettegen', pal])
      && ff(['-i', src, '-i', pal, '-lavfi',
             'fps=10,scale=1000:-1:flags=lanczos[x];[x][1:v]paletteuse', gif])) {
    console.log('GIF', gif)
  } else {
    console.log('GIF skipped — ffmpeg unavailable or failed; webm still written')
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
