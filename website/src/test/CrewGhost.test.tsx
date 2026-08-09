/**
 * CrewGhost — the crew roster's identity avatar.
 *
 * These assertions read the DRAW PROGRAM, not the rendered bitmap. The test DOM
 * is happy-dom, whose `HTMLCanvasElement.getContext()` returns `null` unless a
 * canvas adapter is configured (`lib/nodes/html-canvas-element/HTMLCanvasElement.js`
 * — no adapter is set in this repo's `environmentOptions`), so there are no real
 * pixels to sample and `getImageData` does not exist. A recording stub context
 * is installed in its place (the same `vi.spyOn(HTMLCanvasElement.prototype,
 * 'getContext')` trick `Strands.strictmode.test.tsx` uses for WebGL) and every
 * `fillRect` is captured with the `fillStyle`/`globalAlpha` in force at the time.
 *
 * That op list is a faithful proxy for "the same pixels": the component paints
 * exclusively through `fillRect`, so an identical op list can only produce an
 * identical image, and any change of outfit, colour, or geometry changes the
 * list. What it deliberately does NOT prove is rasterisation — sub-pixel
 * coverage of the fractional accessory rects is the browser's business.
 *
 * Style follows the `CrewAvatar` block in `CrewRoster.test.tsx`: render, read one
 * stable representation of the art, and compare — never a DOM structure that a
 * restyle would break.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import CrewGhost, { CREW_GHOST_FRAME, djb2, ghostVariantCount } from '../apps/issue-radar/components/CrewGhost'

/* ── Recording 2D context ── */

interface Rect { x: number; y: number; w: number; h: number; fill: string; alpha: number }

interface RecordingContext {
  fillStyle: string
  globalAlpha: number
  imageSmoothingEnabled: boolean
  rects: Rect[]
  transforms: number[][]
  clears: number
  setTransform(a: number, b: number, c: number, d: number, e: number, f: number): void
  clearRect(x: number, y: number, w: number, h: number): void
  fillRect(x: number, y: number, w: number, h: number): void
}

function makeRecordingContext(): RecordingContext {
  return {
    fillStyle: '#000000',
    globalAlpha: 1,
    imageSmoothingEnabled: true,
    rects: [],
    transforms: [],
    clears: 0,
    setTransform(a, b, c, d, e, f) { this.transforms.push([a, b, c, d, e, f]) },
    clearRect() { this.clears++ },
    fillRect(x, y, w, h) {
      this.rects.push({ x, y, w, h, fill: this.fillStyle, alpha: this.globalAlpha })
    },
  }
}

/** Contexts handed out during the current test, in creation order. */
let contexts: RecordingContext[] = []

/** One canvas per render here, so the sole context is the one under test. */
const only = () => {
  expect(contexts).toHaveLength(1)
  return contexts[0]
}

/** The art as a comparable string — "the pixels", for equality assertions. */
const artOf = (ctx: RecordingContext) =>
  ctx.rects.map(r => `${r.fill}|${r.alpha}|${r.x},${r.y},${r.w},${r.h}`).join(' ')

beforeEach(() => {
  contexts = []
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    const ctx = makeRecordingContext()
    contexts.push(ctx)
    return ctx as unknown as RenderingContext
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  window.devicePixelRatio = originalDpr
  window.matchMedia = originalMatchMedia
})

/* ── Environment knobs ──
 * happy-dom exposes a real `devicePixelRatio` setter, and `matchMedia` is
 * replaced by assignment rather than a spy (the convention in useTheme.test.tsx
 * / themeScoper.test.tsx), so both are restored by hand above. */
const originalDpr = window.devicePixelRatio
const originalMatchMedia = window.matchMedia

const setDpr = (value: number) => { window.devicePixelRatio = value }

/* ── Helpers ── */

function paint(props: { seed: string; size?: number; variant?: number | null; blush?: boolean }) {
  const { container, unmount } = render(<CrewGhost {...props} />)
  const canvas = container.querySelector('canvas')!
  return { canvas, ctx: only(), art: artOf(only()), unmount }
}

/** Draw one variant straight, with no seed influence. */
function paintVariant(variant: number, blush = false) {
  const out = paint({ seed: 'ignored', variant, blush })
  out.unmount()
  return out
}

/** Art for a pinned variant, isolated from the surrounding test's contexts. */
function paintVariantArt(variant: number): string {
  const saved = contexts
  contexts = []
  const out = paintVariant(variant)
  contexts = saved
  return out.art
}

describe('CrewGhost', () => {
  it('draws the same pixels for the same seed', () => {
    const first = paint({ seed: 'sombrero' })
    first.unmount()
    contexts = []

    const second = paint({ seed: 'sombrero' })
    expect(second.art).toBe(first.art)
    // Not vacuously equal: a ghost is ~40 runs of body bitmap plus accessories.
    expect(second.ctx.rects.length).toBeGreaterThan(20)
  })

  it('draws a different ghost for a seed that hashes to another outfit', () => {
    // Pick two seeds that provably select different looks, so this cannot pass
    // by accident on a hash collision.
    const a = 'andromeda'
    const b = ['whirlpool', 'pinwheel', 'tadpole', 'fornax', 'grus'].find(
      s => djb2(s) % ghostVariantCount !== djb2(a) % ghostVariantCount,
    )!
    expect(djb2(b) % ghostVariantCount).not.toBe(djb2(a) % ghostVariantCount)

    const first = paint({ seed: a })
    first.unmount()
    contexts = []
    const second = paint({ seed: b })

    expect(second.art).not.toBe(first.art)
  })

  it('gives two seeds that land on the same outfit the same face', () => {
    // The honest limit of the distinctness above: with 8 looks, seeds collide by
    // design. Documented here so nobody reads determinism as uniqueness.
    const seeds = ['carina', 'draco', 'medusa', 'cocoon', 'tucana', 'leo', 'hoag', 'mayall', 'cigar']
    const target = djb2(seeds[0]) % ghostVariantCount
    const twin = seeds.slice(1).find(s => djb2(s) % ghostVariantCount === target)
    if (!twin) return // no collision among these names; nothing to assert

    const first = paint({ seed: seeds[0] })
    first.unmount()
    contexts = []
    const second = paint({ seed: twin })
    expect(second.art).toBe(first.art)
  })

  it('lets `variant` override the seed', () => {
    const seed = 'butterfly'
    const natural = djb2(seed) % ghostVariantCount
    const pinned = (natural + 3) % ghostVariantCount

    const bySeed = paint({ seed })
    bySeed.unmount()
    contexts = []
    const overridden = paint({ seed, variant: pinned })
    overridden.unmount()
    contexts = []
    // The pinned look must be the outfit itself, not merely "something else":
    // a different seed asking for the same variant gets identical art.
    const sameVariantOtherSeed = paint({ seed: 'a completely different crew', variant: pinned })

    expect(overridden.art).not.toBe(bySeed.art)
    expect(sameVariantOtherSeed.art).toBe(overridden.art)
  })

  it('treats variant 0 as a pin, not as "no variant"', () => {
    // `0` is falsy; a `variant || null` style check would silently fall through
    // to the seed. Pick a seed that does NOT hash to 0 so the two differ.
    const seed = ['spindle', 'porpoise', 'sculptor', 'triangulum', 'ursa'].find(
      s => djb2(s) % ghostVariantCount !== 0,
    )!
    const zero = paint({ seed, variant: 0 })
    zero.unmount()
    contexts = []
    const seeded = paint({ seed })

    expect(zero.art).not.toBe(seeded.art)
    expect(zero.art).toBe(paintVariantArt(0))
  })

  it('renders every variant without throwing', () => {
    expect(ghostVariantCount).toBe(8)
    const arts = new Set<string>()
    for (let v = 0; v < ghostVariantCount; v++) {
      contexts = []
      const out = paintVariant(v)
      expect(out.ctx.rects.length).toBeGreaterThan(20)
      arts.add(out.art)
    }
    // Eight looks, eight distinct drawings — no duplicated row in the table.
    expect(arts.size).toBe(ghostVariantCount)
  })

  it('keeps every variant inside the sprite frame', () => {
    // The frame exists because hats are drawn ABOVE the 24x28 body bitmap and the
    // cape to its LEFT, at negative offsets from the body origin. Too little
    // headroom clips the witch cone; too little left padding flattens the cape
    // into a stripe on the frame edge. Blush is on because it is the rightmost
    // art there is.
    for (let v = 0; v < ghostVariantCount; v++) {
      contexts = []
      const { ctx } = paintVariant(v, true)
      for (const r of ctx.rects) {
        expect(r.x).toBeGreaterThanOrEqual(0)
        expect(r.y).toBeGreaterThanOrEqual(0)
        expect(r.x + r.w).toBeLessThanOrEqual(CREW_GHOST_FRAME.width)
        expect(r.y + r.h).toBeLessThanOrEqual(CREW_GHOST_FRAME.height)
      }
    }
  })

  it('spends the frame padding it asks for', () => {
    // Guards the other direction: a frame with slack everywhere would satisfy
    // the containment test above while wasting the box. The witch hat (variant 1)
    // must reach into the top padding, and a caped variant into the left.
    const witch = paintVariant(1)
    expect(Math.min(...witch.ctx.rects.map(r => r.y))).toBeLessThan(1)

    contexts = []
    const caped = paintVariant(2)
    expect(Math.min(...caped.ctx.rects.map(r => r.x))).toBe(0)
  })

  it('draws blush only when asked', () => {
    const plain = paintVariant(0)
    contexts = []
    const rosy = paintVariant(0, true)

    expect(plain.ctx.rects.some(r => r.alpha !== 1)).toBe(false)
    // The scene draws the cheeks at 35% alpha; that is the only translucent art.
    expect(rosy.ctx.rects.filter(r => r.alpha === 0.35)).toHaveLength(2)
    expect(rosy.art).not.toBe(plain.art)
  })

  it('renders pixelated art on a device-pixel-ratio-aware canvas', () => {
    setDpr(2)
    const size = CREW_GHOST_FRAME.height // 1 sprite pixel per CSS pixel
    const { canvas, ctx } = paint({ seed: 'crown', size })

    // Backing store is in DEVICE pixels: 2 per sprite pixel here. A canvas sized
    // in CSS pixels would be upscaled by the compositor and look soft.
    expect(canvas.width).toBe(CREW_GHOST_FRAME.width * 2)
    expect(canvas.height).toBe(CREW_GHOST_FRAME.height * 2)
    // …and the CSS box divides back out to the requested size.
    expect(canvas.style.width).toBe(`${CREW_GHOST_FRAME.width}px`)
    expect(canvas.style.height).toBe(`${size}px`)
    // Sprite-pixel coordinates: the transform carries the scale, so the port's
    // numbers stay identical to GhostScene's.
    expect(ctx.transforms.at(-1)).toEqual([2, 0, 0, 2, 0, 0])
    expect(ctx.imageSmoothingEnabled).toBe(false)
    expect(canvas.style.imageRendering).toBe('pixelated')
  })

  it('scales the backing store by whole device pixels', () => {
    setDpr(1)
    const { canvas } = paint({ seed: 'crown', size: CREW_GHOST_FRAME.height * 3 })
    // 3 device pixels per sprite pixel — a fractional zoom is what makes pixel
    // art shimmer, so the box is snapped rather than honoured exactly.
    expect(canvas.width).toBe(CREW_GHOST_FRAME.width * 3)
    expect(canvas.height).toBe(CREW_GHOST_FRAME.height * 3)
  })

  it('never collapses to a sub-pixel canvas', () => {
    setDpr(1)
    const { canvas, ctx } = paint({ seed: 'crown', size: 4 })
    expect(canvas.width).toBe(CREW_GHOST_FRAME.width)
    expect(ctx.transforms.at(-1)).toEqual([1, 0, 0, 1, 0, 0])
  })

  it('is decorative — aria-hidden with no accessible name', () => {
    const { canvas } = paint({ seed: 'fireworks' })
    expect(canvas).toHaveAttribute('aria-hidden', 'true')
    expect(canvas).not.toHaveAttribute('aria-label')
    expect(canvas).not.toHaveAttribute('role')
    // The crew's name is rendered as text beside the avatar, so any name here
    // would be read out twice.
    expect(canvas.textContent).toBe('')
  })

  it('passes className through for layout', () => {
    const { canvas } = paint({ seed: 'grus' })
    expect(canvas.className).toBe('')
    contexts = []
    const styled = render(<CrewGhost seed="grus" className="rounded-md" />)
    expect(styled.container.querySelector('canvas')!.className).toBe('rounded-md')
  })

  it('degrades quietly when no 2D context is available', () => {
    // happy-dom's real behaviour, and a browser that refuses a context. The
    // avatar is decorative, so an empty canvas is the correct outcome — not a
    // thrown error that takes the roster down with it.
    vi.restoreAllMocks()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => null)
    const { container } = render(<CrewGhost seed="tadpole" />)
    expect(container.querySelector('canvas')).toBeTruthy()
  })

  it('repaints when the display resolution changes', () => {
    setDpr(1)
    const listeners: Array<() => void> = []
    // Same watch xterm's CoreBrowserService uses for this exact problem:
    // `matchMedia('… (resolution: Ndppx)')` fires when the window moves to a
    // display with a different ratio.
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: (_t: string, fn: () => void) => { listeners.push(fn) },
      removeEventListener: () => {},
    }) as unknown as typeof window.matchMedia

    const { canvas, ctx } = paint({ seed: 'cigar', size: CREW_GHOST_FRAME.height })
    expect(canvas.width).toBe(CREW_GHOST_FRAME.width)
    expect(listeners).toHaveLength(1)

    // Dragging the window to a retina display changes no React input, so without
    // this listener the avatar would stay soft until some other prop moved.
    setDpr(2)
    listeners.forEach(fn => fn())
    expect(canvas.width).toBe(CREW_GHOST_FRAME.width * 2)
    expect(ctx.clears).toBe(2)
  })
})

describe('djb2 / ghostVariantCount', () => {
  it('matches the hash the rest of the dashboard uses', () => {
    // Same algorithm as `gradientFor` (components/appstore/gradient.ts) and
    // `pickAnimal` (pages/scenes/WateringHoleScene.tsx). Exported so a third
    // copy never gets written; these values pin it.
    expect(djb2('')).toBe(5381)
    expect(djb2('a')).toBe(177670)
    expect(djb2('kirocrew')).toBe(djb2('kirocrew'))
    expect(djb2('crew-1')).not.toBe(djb2('crew-2'))
    // Unsigned 32-bit, so `% ghostVariantCount` can never be negative.
    for (const s of ['andromeda', 'bode', 'whirlpool', 'sombrero', '', 'crëw-ünïcode']) {
      expect(djb2(s)).toBeGreaterThanOrEqual(0)
      expect(djb2(s)).toBeLessThanOrEqual(0xffffffff)
    }
  })

  it('reports the size of the outfit table', () => {
    expect(ghostVariantCount).toBe(8)
    expect(CREW_GHOST_FRAME.width).toBeGreaterThan(24)
    expect(CREW_GHOST_FRAME.height).toBeGreaterThan(28)
  })
})
