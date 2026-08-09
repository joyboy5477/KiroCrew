/**
 * CrewGhost — a crew's identity avatar: the Kiro ghost, drawn on a canvas and
 * kept stable for the life of the crew.
 *
 * Why canvas and not SVG/emoji: `website/AUTOSDE.yaml`'s `use-lucide-icons` is
 * BLOCKING and greps ADDED lines in every `.tsx` for `<svg … viewBox`, and
 * `no-emoji-as-icons` is blocking too. lucide-react ships no mascot marks, so
 * neither an inline vector nor a glyph is available. Pixel art on a canvas is
 * the precedented third option in this repo (`MiniGhost` in
 * `hooks/useSceneInteraction.tsx`, the Worlds scenes, the companion sprite
 * renderers) and no SVG element or path data appears here.
 *
 * The art is a faithful port of `drawGhost` in `pages/scenes/GhostScene.tsx`:
 * same 24×28 bitmap (imported, not re-traced), same eight-outfit table, same
 * hat/glasses/cape/blush geometry. It is a PORT rather than a shared import
 * because the scene keeps `OUTFITS` module-local and defines `drawGhost` inside
 * a `useEffect`, so neither is reachable from outside that file. Only
 * `KIRO_GHOST_PIXELS` was ever extracted. If the scene's accessories change,
 * change them here too.
 *
 * Two deliberate departures from the scene, both because an avatar is a still
 * frame and not a simulation:
 *   • no animation — the cape's `flutter` term is evaluated at t=0 and the ghost
 *     never blinks, so one paint per prop change and no requestAnimationFrame.
 *   • the ghost always faces right (`dir = +1`), which fixes the eye offset and
 *     hangs the cape on the LEFT of the body. The frame's padding follows from
 *     that choice (see SPRITE FRAME below).
 */
import { useEffect, useRef } from 'react'
import { KIRO_GHOST_PIXELS } from '../../../hooks/sceneText'

/* ── Palette ──
 * Hard-coded rather than themed, exactly as the scene has it: every ghost is
 * classic Kiro white and the outfit does the differentiating. The art reads as
 * content (like `gradientFor`'s swatches in components/appstore/gradient.ts),
 * not as chrome, so it must not shift with the active theme. */
const GHOST_COLOR = '#e8ecf4'
const EYE_COLOR = '#14141e'

/* ── Outfits ── */
type Hat = 'witch' | 'top' | 'party' | 'beanie' | 'crown' | 'none'
type Glasses = 'round' | 'shades' | 'none'
interface Outfit { hat: Hat; glasses: Glasses; cape: boolean; capeColor: string }

/** The scene's eight looks, in the scene's order. */
const OUTFITS: Outfit[] = [
  { hat: 'none', glasses: 'round', cape: false, capeColor: '' },
  { hat: 'witch', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'none', glasses: 'none', cape: true, capeColor: '#c0392b' },
  { hat: 'top', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'none', glasses: 'shades', cape: true, capeColor: '#27408b' },
  { hat: 'beanie', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'party', glasses: 'round', cape: false, capeColor: '' },
  { hat: 'crown', glasses: 'none', cape: true, capeColor: '#5b2c6f' },
]

/** How many distinct looks exist. Exported so callers (a variant picker in the
 *  crew editor, a legend) never hard-code 8 or re-derive it from a copy. */
export const ghostVariantCount = OUTFITS.length

/**
 * Stable non-crypto string hash (djb2) — the same function as `gradientFor` in
 * `components/appstore/gradient.ts` and `pickAnimal` in
 * `pages/scenes/WateringHoleScene.tsx`. Exported so other crew views select
 * per-identity art (or anything else) from the same number instead of writing a
 * fourth copy that could disagree.
 */
export function djb2(s: string): number {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0
  return h
}

/**
 * Pick a look for an identity by HASHING the seed — not by roster position.
 *
 * GhostScene indexes the table by array position (`OUTFITS[i % OUTFITS.length]`
 * over the live agent list), which is fine for a scene where the ghosts are
 * anonymous set dressing, but wrong for an avatar: sorting the crew list,
 * pausing a crew, or adding one shifts every later index and silently repaints
 * everyone with someone else's hat. Hashing the crew's stable id instead keeps a
 * given crew's face for its whole life, which is the same intent
 * `WateringHoleScene.pickAnimal` states for species.
 */
function outfitFor(seed: string, variant?: number | null): Outfit {
  if (variant != null && Number.isFinite(variant)) {
    // Non-null `variant` pins the look (the crew editor's "pick a face" flow).
    // Wrapped, and negatives folded, so an out-of-range value cannot yield
    // `undefined` and crash the paint.
    const i = ((Math.trunc(variant) % ghostVariantCount) + ghostVariantCount) % ghostVariantCount
    return OUTFITS[i]
  }
  return OUTFITS[djb2(seed) % ghostVariantCount]
}

/* ── SPRITE FRAME ──
 * The bitmap is 24×28, but accessories are drawn OUTSIDE it, at negative
 * offsets from the body's top-left. Sizing the canvas to the bitmap would clip
 * every hat and flatten the cape into a stripe along the frame's left edge.
 * Measured from the port below, at dir=+1 and flutter=0:
 *
 *   above the body   witch cone tip   y = −7.5   (top hat −7, party −6.4)
 *   left of the body cape back panel  x = −4     (main panel −3)
 *   right of body    blush, +eyeShift x = +23.5  (bitmap itself fills to +23)
 *   below the body   nothing          y = +28    (cape ends by +20)
 *
 * So the frame is the bitmap plus 8 above (7.5 rounded up to a whole sprite
 * pixel) and 4 to the left, and 1 on the right and bottom to keep the art off
 * the frame edge — the bitmap's widest rows really do fill column 23, so
 * without that column a container ring would shave the body.
 *
 * The padding is UNIFORM across all eight variants, not per-variant: a bare-
 * headed, capeless ghost therefore sits low and slightly right of center inside
 * its box. That is the deliberate trade — a per-variant frame would pack each
 * ghost tighter but stop the eight faces from aligning with each other in a
 * roster column, which is where these are actually rendered. */
const BITMAP_W = 24
const BITMAP_H = 28
const PAD_TOP = 8
const PAD_LEFT = 4
const PAD_RIGHT = 1
const PAD_BOTTOM = 1

/** The sprite frame in sprite pixels. Exported so a caller can reserve layout
 *  space at the right aspect ratio instead of guessing (and shifting on paint). */
export const CREW_GHOST_FRAME = {
  width: PAD_LEFT + BITMAP_W + PAD_RIGHT,   // 29
  height: PAD_TOP + BITMAP_H + PAD_BOTTOM,  // 37
} as const

/** Ghosts face right, so the eyes sit half a pixel toward that side and the
 *  cape trails on the left. Same constant the scene derives from `dir > 0`. */
const EYE_SHIFT = 0.5

/**
 * Paint one ghost, body top-left at (gx, gy) in sprite pixels. The caller has
 * already scaled the context, so every coordinate here is a sprite pixel and
 * matches GhostScene's numbers one-for-one.
 */
function drawCrewGhost(X: CanvasRenderingContext2D, gx: number, gy: number, o: Outfit, blush: boolean) {
  const d = (x: number, y: number, w: number, h: number, c: string) => {
    X.fillStyle = c
    X.fillRect(x, y, w, h)
  }

  // Cape — behind the body. The scene's `flutter` is Math.sin(t·0.08 + phase)·1.5
  // and is 0 for a still frame, so the panels take their rest heights.
  if (o.cape) {
    const back = gx - 3
    d(back, gy + 6, 4, 14, o.capeColor)
    d(back - 1, gy + 9, 2, 9, o.capeColor)
    // Collar over the shoulders
    d(gx + 4, gy + 6, 17, 1.8, o.capeColor)
  }

  // Body — run-length fill of the shared bitmap, one fillRect per horizontal run
  X.fillStyle = GHOST_COLOR
  KIRO_GHOST_PIXELS.forEach((row, ry) => {
    let run = -1
    for (let cx = 0; cx <= row.length; cx++) {
      const on = cx < row.length && row[cx] === '#'
      if (on && run < 0) run = cx
      else if (!on && run >= 0) {
        X.fillRect(gx + run, gy + ry, cx - run, 1)
        run = -1
      }
    }
  })

  // Eyes — tall rounded ovals right of center: 1px-narrower top and bottom caps.
  // The scene's blink state is never entered here (a still avatar that happened
  // to be captured mid-blink would look broken, not alive).
  d(gx + 11.5 + EYE_SHIFT, gy + 7, 2, 1, EYE_COLOR)
  d(gx + 11 + EYE_SHIFT, gy + 8, 3, 3, EYE_COLOR)
  d(gx + 11.5 + EYE_SHIFT, gy + 11, 2, 1, EYE_COLOR)
  d(gx + 17.5 + EYE_SHIFT, gy + 7, 2, 1, EYE_COLOR)
  d(gx + 17 + EYE_SHIFT, gy + 8, 3, 3, EYE_COLOR)
  d(gx + 17.5 + EYE_SHIFT, gy + 11, 2, 1, EYE_COLOR)

  // Glasses
  if (o.glasses === 'round') {
    d(gx + 10 + EYE_SHIFT, gy + 6.2, 5, 0.9, '#3a3a4a')
    d(gx + 16 + EYE_SHIFT, gy + 6.2, 5, 0.9, '#3a3a4a')
    d(gx + 10 + EYE_SHIFT, gy + 6.2, 0.9, 6.5, '#3a3a4a')
    d(gx + 14.1 + EYE_SHIFT, gy + 6.2, 0.9, 6.5, '#3a3a4a')
    d(gx + 20.1 + EYE_SHIFT, gy + 6.2, 0.9, 6.5, '#3a3a4a')
    d(gx + 10 + EYE_SHIFT, gy + 11.8, 5, 0.9, '#3a3a4a')
    d(gx + 16 + EYE_SHIFT, gy + 11.8, 5, 0.9, '#3a3a4a')
    d(gx + 14.9 + EYE_SHIFT, gy + 7.5, 1.2, 0.9, '#3a3a4a')
  } else if (o.glasses === 'shades') {
    d(gx + 10 + EYE_SHIFT, gy + 6.8, 4.6, 4.4, '#111')
    d(gx + 15.6 + EYE_SHIFT, gy + 6.8, 4.6, 4.4, '#111')
    d(gx + 14.4 + EYE_SHIFT, gy + 7.4, 1.4, 1, '#111')
    // Lens glint
    d(gx + 10.8 + EYE_SHIFT, gy + 7.5, 1.2, 0.9, '#8fa8ff')
    d(gx + 16.4 + EYE_SHIFT, gy + 7.5, 1.2, 0.9, '#8fa8ff')
  }

  // Hats — perched on the dome (apex ~cols 10-16). These are the rects that
  // consume the frame's headroom; the tallest is the witch cone at gy − 7.5.
  if (o.hat === 'witch') {
    d(gx + 6, gy - 1, 16, 1.8, '#2d1b4e')
    d(gx + 10, gy - 4.5, 7, 3.5, '#2d1b4e')
    d(gx + 11.8, gy - 7.5, 3.4, 3.4, '#2d1b4e')
    d(gx + 10, gy - 2, 7, 1.1, '#8e44ad')
  } else if (o.hat === 'top') {
    d(gx + 7, gy - 1, 13, 1.4, '#181820')
    d(gx + 9, gy - 7, 9, 6, '#181820')
    d(gx + 9, gy - 2.2, 9, 1.2, '#b03a2e')
  } else if (o.hat === 'party') {
    d(gx + 11, gy - 2.4, 4.6, 2.4, '#f39c12')
    d(gx + 12, gy - 4.8, 2.7, 2.4, '#e74c3c')
    d(gx + 12.7, gy - 6.4, 1.2, 1.6, '#f1c40f')
  } else if (o.hat === 'beanie') {
    d(gx + 8, gy - 1.2, 11, 2.8, '#16a085')
    d(gx + 8, gy + 0.6, 11, 1, '#0e6655')
    d(gx + 12.7, gy - 2.8, 1.8, 1.8, '#f4d03f')
  } else if (o.hat === 'crown') {
    d(gx + 9.5, gy - 2.4, 7.5, 2.4, '#f1c40f')
    d(gx + 9.5, gy - 4, 1.7, 1.9, '#f1c40f')
    d(gx + 12.4, gy - 4, 1.7, 1.9, '#f1c40f')
    d(gx + 15.3, gy - 4, 1.7, 1.9, '#f1c40f')
    d(gx + 12.8, gy - 1.6, 1.1, 1.1, '#e74c3c')
  }

  // Blush — in the scene this marks a running agent; here the caller decides
  // what "alive" means for a crew, so it is a prop.
  if (blush) {
    X.globalAlpha = 0.35
    d(gx + 8 + EYE_SHIFT, gy + 12.5, 2, 1.1, '#ff8899')
    d(gx + 21 + EYE_SHIFT, gy + 12.5, 2, 1.1, '#ff8899')
    X.globalAlpha = 1
  }
}

/** Cap the backing store so a 4×-DPR display cannot balloon a roster of avatars. */
const MAX_DPR = 4

export interface CrewGhostProps {
  /** Stable per-crew identity (the crew id — NOT its display name, which can be
   *  renamed). Chooses the outfit when `variant` is null. */
  seed: string
  /** Rendered height in CSS pixels; the width follows the frame's aspect ratio.
   *  Snapped up to a whole number of device pixels per sprite pixel (see below). */
  size?: number
  /** Pins one of the `ghostVariantCount` looks, ignoring the seed. `null` (the
   *  default) means "derive from the seed". */
  variant?: number | null
  /** Draw the cheeks — the caller's signal that this crew is working. */
  blush?: boolean
  className?: string
}

export default function CrewGhost({ seed, size = 34, variant = null, blush = false, className }: CrewGhostProps) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    // happy-dom (the test DOM) and any browser that refuses a 2D context return
    // null here. An avatar is decorative, so the empty canvas is the right
    // degradation — the crew's name is always rendered beside it as text.
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const paint = () => {
      const dpr = Math.min(Math.max(window.devicePixelRatio || 1, 1), MAX_DPR)
      // Pixel art shimmers at a fractional zoom, so resolve DEVICE pixels per
      // sprite pixel to a whole number and derive the CSS box back from it. The
      // rendered height can therefore land up to one sprite pixel above `size`;
      // crisp art is worth more than honouring the request to the pixel. The
      // backing store is sized in device pixels (not CSS pixels), which is what
      // keeps a retina display from upscaling a 1× bitmap.
      const px = Math.max(1, Math.round((size / CREW_GHOST_FRAME.height) * dpr))
      canvas.width = CREW_GHOST_FRAME.width * px
      canvas.height = CREW_GHOST_FRAME.height * px
      canvas.style.width = `${canvas.width / dpr}px`
      canvas.style.height = `${canvas.height / dpr}px`

      // Setting .width resets the context, so state and transform go after it.
      ctx.imageSmoothingEnabled = false
      ctx.setTransform(px, 0, 0, px, 0, 0)
      ctx.clearRect(0, 0, CREW_GHOST_FRAME.width, CREW_GHOST_FRAME.height)
      drawCrewGhost(ctx, PAD_LEFT, PAD_TOP, outfitFor(seed, variant), blush)
    }

    paint()

    // Dragging the window to a display with a different DPR changes nothing
    // observable to React, so repaint on the resolution query instead of
    // leaving the avatar soft until the next prop change.
    const mq = window.matchMedia?.(`(resolution: ${window.devicePixelRatio || 1}dppx)`)
    mq?.addEventListener?.('change', paint)
    return () => mq?.removeEventListener?.('change', paint)
  }, [seed, size, variant, blush])

  return (
    <canvas
      ref={ref}
      className={className}
      // `pixelated` is the other half of the crispness contract: whenever the
      // backing store still has to be scaled to the CSS box (fractional DPR, or
      // a parent that constrains the element), nearest-neighbour keeps the
      // pixels square instead of blurring them. Matches PIXEL_CANVAS_STYLE in
      // hooks/sceneText.ts, minus that constant's scene-only border and cursor.
      style={{ imageRendering: 'pixelated', display: 'block', flexShrink: 0 }}
      // Decorative: the crew's name is always rendered as text next to this, so
      // an accessible name here would only make a screen reader say it twice.
      aria-hidden
    />
  )
}
