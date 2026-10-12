/**
 * Leaving the guided New crewmate flow through the nav rail — evidence frames.
 *
 *   01-draft-<theme>           the guided flow on its name step with a typed
 *                              goal and name: an edited draft, in place of
 *                              Radar's chat
 *   02-rail-crewmates-<theme>  after one click on the rail's Crewmates row: no
 *                              confirm was raised, the flow stepped aside and
 *                              Radar's chat is back under the "Your crewmate
 *                              draft was kept. Resume" note
 *   03-draft-kept-<theme>      the note's Resume clicked: the same draft, goal
 *                              and name as typed, on the same step
 *   04-rail-sessions-<theme>   after one click on the rail's Sessions row (a
 *                              different page): the leave confirm WAS raised
 *                              (recorded, answered "stay"), so the page and the
 *                              draft are still here
 *   06-empty-hero-<theme>      an EMPTY roster: the flow was opened from the
 *                              hero at a bare /members, a goal typed, then the
 *                              rail's Crewmates row clicked (same address, the
 *                              guard is never asked): the flow still steps
 *                              aside and the hero's button reads "Resume
 *                              crewmate draft"
 *   06b-empty-hero-mobile-<theme>  390x844: the roster-column copy of the hero
 *                              (the md:hidden placement) after the flow stepped
 *                              aside with a typed goal -- by "Not now" there,
 *                              since the phone's rail lives in a drawer that
 *                              exists only over an open chat
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): the real rail rows ask the real leave channel.
 * `window.confirm` is replaced before the app boots: it records each message
 * and answers "stay", so the frames prove which clicks asked.
 *
 * Usage:
 *   cd website && npm run build && node scripts/capture-crewmate-wizard-rail-exit.mjs [outDir] [distDir]
 *
 * `distDir` defaults to this checkout's `website/dist`. Point it at a build of
 * the base branch to reproduce the defect: frame 02 then records the confirm
 * and the flow still on screen, and the run exits 1.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crewmate-wizard-rail-exit')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Triage new GitHub issues every morning', source: 'kirocrew',
  last_active_ts: now - 30, last_message: 'Quiet morning: three new issues, one needs a decision.',
}
const SLOTS = [{ key: 'member-radar', title: 'Radar', mode: 'member', created: new Date((now - 86400) * 1000).toISOString(), last_ts: new Date((now - 30) * 1000).toISOString(), running: false, agent: 'Radar' }]
// The guided flow has been seen, so the page never opens it on its own; the
// Crewmates rail row is preview-gated (`mc-preview-crew`), and the rail is
// expanded so its labels read in the frames.
const STORAGE = { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '0', 'mc-members-panel-open': '0', 'mc-preview-crew': '1', 'mc-privacy-notice-v1': '1' }
const LEAVE_MESSAGE = 'Leave this page? Your unsaved crewmate draft will be lost.'
const GOAL = 'Keep release notes up to date'
const NAME = 'Scribe'

// Mate's greeting reads the crew board on this page; the shipped empty fixture.
const CREW_BOARD = JSON.parse(readFileSync(new URL('./fixtures/crew-board-empty.json', import.meta.url), 'utf8'))

const extraFor = (members) => async (path, route) => {
  if (path === '/api/members') { await json(route, { members, default_agent: 'kirocrew' }); return true }
  if (path === '/api/crew-board') { await json(route, CREW_BOARD); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  if (path === '/api/agents/installed') { await json(route, [{ name: 'kirocrew', kiro_agent: 'kirocrew', scope: 'global' }]); return true }
  if (path === '/api/slack/config') { await json(route, { configured: false, connected: false }); return true }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) { await json(route, { slot_key: 'member-radar', slug: 'radar', member: 'Radar', created: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: 'radar', member: 'Radar', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  return false
}

const DIST = process.argv[3]
const { srv, base } = await serveDist(DIST || undefined)
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function open(theme, { scene = 'radar', viewport = { width: 1440, height: 900 } } = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  const extra = extraFor(scene === 'empty' ? [] : [RADAR])
  // Before the app boots: every confirm the page raises is recorded and
  // answered "stay", the answer that keeps a draft.
  await page.addInitScript(() => {
    window.__confirms = []
    window.confirm = (message) => { window.__confirms.push(String(message ?? '')); return false }
  })
  await stubDashboardApi(page, { theme, extra, slots: scene === 'empty' ? [] : SLOTS, localStorageEntries: STORAGE })
  await page.goto(scene === 'empty' ? `${base}/members` : `${base}/members?member=Radar`, { waitUntil: 'domcontentloaded' })
  // The hero renders twice (chat column at md+, roster column below); wait for whichever is on screen.
  await page.getByTestId(scene === 'empty' ? 'crewmate-empty-cta' : 'crewmate-switcher').locator('visible=true').first().waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(400)
  return { context, page }
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme ${theme}`, theme === 'light' ? lum > 180 : lum < 90, `body bg=${bg}`)
}

const confirms = (page) => page.evaluate(() => window.__confirms.slice())
const hasHidden = (el) => el.className.split(/\s+/).includes('hidden')
const flowVisible = (page) => page.getByTestId('member-create-guided').evaluate(el => !el.className.split(/\s+/).includes('hidden'))
const createPaneVisible = (page) => page.getByTestId('member-create-pane').evaluate(el => !el.className.split(/\s+/).includes('hidden'))
/** The rail row by its onboarding id — the real `NavItem` the shell renders. */
const railRow = (page, navId) => page.locator(`[data-onboarding-nav="${navId}"]`)

/** framer-motion slides each step in and fades the pane: a frame shot mid-slide
 *  reads as greyed out and its Next as disabled. Wait for the step and every
 *  ancestor up to the create pane to be fully opaque and untransformed. */
async function settled(page) {
  await page.waitForFunction(() => {
    const step = document.querySelector('[data-testid^="meet-crewmates-step-"]')
    const pane = document.querySelector('[data-testid=member-create-pane]')
    if (!step || !pane) return false
    for (let el = step; el; el = el.parentElement) {
      const cs = getComputedStyle(el)
      if (cs.opacity !== '1' || (cs.transform !== 'none' && cs.transform !== 'matrix(1, 0, 0, 1, 0, 0)')) return false
      if (el === pane) break
    }
    return true
  })
  await page.waitForTimeout(200)
}

/** The chat header's crewmate switcher holds the New crewmate door, the same
 *  door the GUI tester took. */
async function clickAddCrewmate(page) {
  await page.getByTestId('crewmate-switcher').click()
  await page.getByTestId('crewmate-switcher-create').click()
}

for (const theme of ['dark', 'light']) {
  const { context, page } = await open(theme)
  const pathname = () => page.evaluate(() => location.pathname)

  // 01 — an edited draft on the name step
  await clickAddCrewmate(page)
  const goal = page.getByTestId('meet-crewmates-goal')
  await goal.waitFor()
  await goal.fill(GOAL)
  await page.getByTestId('meet-crewmates-next').click()
  const name = page.getByTestId('meet-crewmates-name')
  await name.waitFor()
  await name.fill(NAME)
  await page.mouse.move(5, 5)
  await settled(page)
  check(`01-draft-${theme} flow shown`, await flowVisible(page), 'guided flow in place of the chat')
  check(`01-draft-${theme} name step`, (await page.getByTestId('meet-crewmates-step-2').count()) === 1, 'step 2 of 4')
  await assertTheme(page, theme, `01-draft-${theme}`)
  await page.screenshot({ path: join(OUT, `01-draft-${theme}.png`) })

  // 02 — the rail's Crewmates row: nothing asked, the flow steps aside
  await railRow(page, 'members').click()
  // Bounded: on a build with the defect the flow never hides, and the checks
  // below record that instead of the run dying here.
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid=member-create-guided]')
    return !!el && el.className.split(/\s+/).includes('hidden')
  }, null, { timeout: 5000 }).catch(() => {})
  await page.mouse.move(5, 5)
  await page.waitForTimeout(400)
  const asked02 = await confirms(page)
  check(`02-rail-crewmates-${theme} no confirm`, asked02.length === 0, `confirms=${JSON.stringify(asked02)}`)
  check(`02-rail-crewmates-${theme} flow aside`, !(await flowVisible(page)), 'guided flow hidden')
  check(`02-rail-crewmates-${theme} chat back`, !(await createPaneVisible(page)), 'create pane hidden, chat column shows')
  check(`02-rail-crewmates-${theme} still on page`, (await pathname()) === '/members', `pathname=${await pathname()}`)
  const note = page.getByTestId('member-draft-waiting')
  check(`02-rail-crewmates-${theme} kept note`, (await note.count()) === 1 && /Your crewmate draft was kept\./.test(await note.textContent()) && (await page.getByTestId('member-draft-waiting-resume').textContent()).trim() === 'Resume', `note=${(await note.textContent().catch(() => '')).trim()}`)
  await assertTheme(page, theme, `02-rail-crewmates-${theme}`)
  await page.screenshot({ path: join(OUT, `02-rail-crewmates-${theme}.png`) })

  // The dismiss is words, never a glyph that could read as "delete the draft".
  check(`02-rail-crewmates-${theme} dismiss is words`, (await page.getByTestId('member-draft-waiting-dismiss').textContent()).trim() === 'Hide this note' && (await page.getByTestId('member-draft-waiting-dismiss').locator('svg').count()) === 0, `dismiss=${(await page.getByTestId('member-draft-waiting-dismiss').textContent()).trim()}`)
  // The doors beside the note keep their plain label.
  await page.getByTestId('crewmate-switcher').click()
  check(`02-rail-crewmates-${theme} switcher row plain`, (await page.getByTestId('crewmate-switcher-create').textContent()).trim() === 'New crewmate', `label=${(await page.getByTestId('crewmate-switcher-create').textContent()).trim()}`)
  await page.keyboard.press('Escape')
  await page.getByTestId('crewmate-switcher-create').waitFor({ state: 'hidden' })

  // 03 — the note's Resume: the same draft resumes
  await page.getByTestId('member-draft-waiting-resume').click()
  await page.getByTestId('meet-crewmates-name').waitFor()
  check(`03-draft-kept-${theme} name kept`, (await page.getByTestId('meet-crewmates-name').inputValue()) === NAME, `name=${await page.getByTestId('meet-crewmates-name').inputValue()}`)
  check(`03-draft-kept-${theme} same step`, (await page.getByTestId('meet-crewmates-step-2').count()) === 1, 'step 2 of 4')
  await page.getByTestId('meet-crewmates-back').click()
  await page.getByTestId('meet-crewmates-goal').waitFor()
  check(`03-draft-kept-${theme} goal kept`, (await page.getByTestId('meet-crewmates-goal').inputValue()) === GOAL, `goal=${await page.getByTestId('meet-crewmates-goal').inputValue()}`)
  await page.getByTestId('meet-crewmates-next').click()
  await page.getByTestId('meet-crewmates-name').waitFor()
  await page.mouse.move(5, 5)
  await settled(page)
  await assertTheme(page, theme, `03-draft-kept-${theme}`)
  await page.screenshot({ path: join(OUT, `03-draft-kept-${theme}.png`) })

  // 04 — the rail's Sessions row: a real leave, asked and refused
  await railRow(page, 'chat').click()
  await settled(page)
  const asked04 = await confirms(page)
  check(`04-rail-sessions-${theme} asked once`, asked04.length === 1 && asked04[0] === LEAVE_MESSAGE, `confirms=${JSON.stringify(asked04)}`)
  check(`04-rail-sessions-${theme} stayed`, (await pathname()) === '/members' && (await flowVisible(page)), `pathname=${await pathname()}`)
  await assertTheme(page, theme, `04-rail-sessions-${theme}`)
  await page.screenshot({ path: join(OUT, `04-rail-sessions-${theme}.png`) })

  await context.close()

  // 06 — an empty roster: the flow opened at a bare /members from the hero
  for (const [tag, viewport] of [[`06-empty-hero-${theme}`, { width: 1440, height: 900 }], [`06b-empty-hero-mobile-${theme}`, { width: 390, height: 844 }]]) {
    const { context: ctx, page: pg } = await open(theme, { scene: 'empty', viewport })
    const cta = () => pg.getByTestId('crewmate-empty-cta').locator('visible=true').first()
    check(`${tag} hero reads New crewmate`, (await cta().textContent()).trim() === 'New crewmate', `label=${(await cta().textContent()).trim()}`)
    await cta().click()
    const g = pg.getByTestId('meet-crewmates-goal')
    await g.waitFor()
    await g.fill(GOAL)
    check(`${tag} bare address`, (await pg.evaluate(() => location.pathname + location.search)) === '/members', `address=${await pg.evaluate(() => location.pathname + location.search)}`)
    // Typing dirtied the draft, which arms the Back guard's duplicate entry over
    // the same address; that push is not a click on the page and must not have
    // stepped the flow aside by itself.
    await pg.waitForTimeout(400)
    check(`${tag} flow still shown after typing`, await flowVisible(pg), 'guided flow on screen')
    // Desktop: the address is already /members, so the rail skips the guard and
    // navigates anyway; the page reads the landing and steps the flow aside.
    // Phone: the flow covers the screen and the rail lives in a drawer that
    // only exists over an open chat, so the step-aside there is "Not now".
    if (viewport.width < 640) await pg.getByTestId('meet-crewmates-not-now').click()
    else await pg.locator('[data-onboarding-nav="members"]').locator('visible=true').first().click()
    await pg.waitForFunction(() => {
      const el = document.querySelector('[data-testid=member-create-guided]')
      return !!el && el.className.split(/\s+/).includes('hidden')
    }, null, { timeout: 5000 }).catch(() => {})
    check(`${tag} no confirm`, (await confirms(pg)).length === 0, `confirms=${JSON.stringify(await confirms(pg))}`)
    check(`${tag} flow aside`, !(await flowVisible(pg)), 'guided flow hidden')
    await cta().waitFor({ state: 'visible' })
    check(`${tag} hero reads Resume`, (await cta().textContent()).trim() === 'Resume crewmate draft', `label=${(await cta().textContent()).trim()}`)
    check(`${tag} pen glyph`, (await cta().locator('svg.lucide-pencil-line').count()) === 1, 'PencilLine on the CTA')
    await pg.mouse.move(5, 5)
    await pg.waitForTimeout(400)
    await assertTheme(pg, theme, tag)
    await pg.screenshot({ path: join(OUT, `${tag}.png`) })
    await ctx.close()
  }
}

await browser.close()
srv.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
