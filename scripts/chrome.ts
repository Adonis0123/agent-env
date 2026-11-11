// Shared by the scripts that retake README media (record-demo.ts, shoot-panel.ts): a throwaway work directory,
// a fake HOME, the panel dev server on a fixture and a small Chrome DevTools Protocol client over Node's built-in
// WebSocket. Every child process is stopped (by its pid) and the work directory removed when the script exits.
// Nothing here reads the real HOME: the engine and the panel run with HOME pointing into the work directory.
import { type ChildProcess, spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync } from 'node:fs'
import { createServer } from 'node:net'
import { homedir, hostname, tmpdir, userInfo } from 'node:os'
import { join, resolve } from 'node:path'

export const root = resolve(import.meta.dirname, '..')
/** The fixture the README panel shots show: every plugin, a remote target and a cloud bot. */
export const PANEL_FIXTURE = 'panel/fixtures/v8-plugins.json'
/** The demo user's manifest (examples/demo), relative to the repository root. */
export const DEMO_MANIFEST = 'examples/demo/manifest/habits.toml'

export function fail(message: string): never {
  console.error(message)
  process.exit(1)
}

const children: ChildProcess[] = []
/** Stop `child` (by its pid) when the script exits. */
export function track(child: ChildProcess): ChildProcess {
  children.push(child)
  return child
}

/** A throwaway directory under the system temp directory ($TMPDIR), removed with every tracked child on exit. */
export function workDir(prefix: string): string {
  const work = mkdtempSync(join(tmpdir(), prefix))
  process.on('exit', () => {
    for (const child of children) {
      if (child.pid && child.exitCode === null) {
        try {
          process.kill(child.pid)
        } catch {
          // already gone
        }
      }
    }
    // A child stopped just now may still be writing its files: try for up to two seconds.
    for (let i = 0; ; i++) {
      try {
        rmSync(work, { recursive: true, force: true })
        break
      } catch {
        if (i === 20) {
          console.error(`could not remove ${work}. 修复：delete that folder by hand.`)
          break
        }
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100)
      }
    }
  })
  return work
}

/** The first of `names` on PATH, or undefined. */
export function which(name: string): string | undefined {
  const found = spawnSync('sh', ['-c', `command -v "$0"`, name], { encoding: 'utf8' }).stdout.trim()
  return found || undefined
}

/**
 * An environment whose HOME is `<dir>/home` and whose PATH holds only the system directories plus links to
 * python3, git and gitleaks. Agent hosts installed on this machine (claude, codex, ...) stay out of reach, so
 * nothing about this machine shows up in what the engine prints.
 */
export function fakeHome(dir: string): NodeJS.ProcessEnv {
  const home = join(dir, 'home')
  const bin = join(dir, 'bin')
  mkdirSync(home, { recursive: true })
  mkdirSync(bin, { recursive: true })
  const python =
    process.env.AGENT_ENV_PYTHON ??
    which('python3') ??
    fail('python3 is not on PATH. 修复：install Python 3.11 or later, or set AGENT_ENV_PYTHON to its python3.')
  for (const [name, path] of [
    ['python3', python],
    ['git', which('git')],
    ['gitleaks', which('gitleaks')],
  ] as const) {
    if (path) symlinkSync(path, join(bin, name))
  }
  const env: NodeJS.ProcessEnv = {
    PATH: `${bin}:/usr/bin:/bin`,
    HOME: home,
    XDG_STATE_HOME: join(home, '.local/state'),
    XDG_CONFIG_HOME: join(home, '.config'),
    AGENT_ENV_PYTHON: join(bin, 'python3'),
    LANG: 'en_US.UTF-8',
    TMPDIR: dir,
  }
  return env
}

/**
 * Throws away a capture that still names this machine: the real home directory, the user name, the host name
 * or any path under a macOS or Linux home root. `what` names the capture in the error.
 */
export function assertAnonymous(text: string, what: string): void {
  const needles = [homedir(), userInfo().username, hostname().replace(/\.local$/, '')]
  const hit = needles.find((n) => n && text.includes(n)) ?? /\/(?:Users|home)\/[^/\s]+/.exec(text)?.[0]
  if (hit) {
    const at = text.indexOf(hit)
    fail(
      `${what} still contains "${hit}", which names this machine:\n  …${text.slice(Math.max(0, at - 80), at + 80)}…\n修复：add a replacement for it in the script that captured it and run this again.`,
    )
  }
}

/** A TCP port nobody listens on right now, picked by the system. */
export async function freePort(): Promise<number> {
  return new Promise((done, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      server.close(() => (typeof address === 'object' && address ? done(address.port) : reject(new Error('no port'))))
    })
  })
}

/**
 * Start the panel dev server (panel/dev-server.ts) on `fixture` with HOME inside `dir`, and resolve to its URL.
 * The port is a free one picked at start; it is never written into any output.
 */
export async function startPanel(dir: string, fixture = PANEL_FIXTURE): Promise<string> {
  // The fixture path is relative to the repository root, as the panel prints it under the page.
  const env = { ...fakeHome(dir), AGENT_ENV_STATUS_FILE: fixture, AGENT_ENV_MANIFEST: DEMO_MANIFEST }
  const port = await freePort()
  const panel = track(
    spawn(process.execPath, ['--disable-warning=ExperimentalWarning', join(root, 'panel/dev-server.ts'), String(port)], {
      cwd: root,
      env: { ...env, PATH: `${env.PATH}:${resolve(process.execPath, '..')}` },
    }),
  )
  return new Promise<string>((done) => {
    let log = ''
    const timer = setTimeout(
      () => fail(`the panel dev server did not start. 修复：run it by hand to see why (node panel/dev-server.ts). Its output:\n${log}`),
      15_000,
    )
    const read = (chunk: Buffer) => {
      log += chunk
      const url = /env panel: (http:\/\/127\.0\.0\.1:\d+\/)/.exec(log)?.[1]
      if (url) {
        clearTimeout(timer)
        done(url)
      }
    }
    panel.stdout?.on('data', read)
    panel.stderr?.on('data', read)
    panel.once('exit', (code) => fail(`the panel dev server exited with ${code}. 修复：run node panel/dev-server.ts by hand. Its output:\n${log}`))
  })
}

/** A page expression: true once the panel has drawn its rows (the fixture always has some). */
export const PANEL_LOADED = `!!document.querySelector('main, .wrap') && document.readyState === 'complete' && (document.body?.innerText ?? '').length > 200`

/**
 * Calls `check` until it returns something truthy. A check that throws counts as "not yet": while a page navigates,
 * its old context goes away and its new one may have no document element.
 */
export async function pollUntil(
  check: () => Promise<unknown>,
  { tries = 100, waitMs = 150 } = {},
): Promise<{ ok: true } | { ok: false; lastError?: unknown }> {
  let lastError: unknown
  for (let i = 0; i < tries; i++) {
    try {
      if (await check()) return { ok: true }
      lastError = undefined
    } catch (e) {
      lastError = e
    }
    if (i < tries - 1) await new Promise((r) => setTimeout(r, waitMs))
  }
  return { ok: false, lastError }
}

function findChrome(): string {
  const candidates = [
    process.env.CHROME,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ]
  for (const name of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
    const found = which(name)
    if (found) candidates.push(found)
  }
  const chrome = candidates.find((c) => c && existsSync(c))
  return (
    chrome ??
    fail(
      'Chrome not found. 修复：install Google Chrome (https://www.google.com/chrome/) or Chromium, or set CHROME to its binary, and run this again.',
    )
  )
}

// biome-ignore lint/suspicious/noExplicitAny: CDP replies are untyped JSON; each caller reads the fields it asked for
export type Reply = any

export interface Chrome {
  /** Send a CDP command to the page. */
  page(method: string, params?: object): Promise<Reply>
  evaluate(expression: string): Promise<Reply>
  until(expression: string, what: string): Promise<void>
  /** A PNG of the page as it is now. */
  screenshot(): Promise<Buffer>
  close(): Promise<void>
}

/**
 * Headless Chrome (`--headless=new`) with one blank page and a fresh profile under `work`, never the everyday one.
 * `lang` is the browser language, which the panel follows through Accept-Language.
 */
export async function openChrome(work: string, lang = 'en-US'): Promise<Chrome> {
  const chrome = track(
    spawn(findChrome(), [
      '--headless=new',
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      '--force-color-profile=srgb',
      `--lang=${lang}`,
      '--allow-file-access-from-files',
      `--user-data-dir=${join(work, 'chrome')}`,
      '--remote-debugging-port=0',
      'about:blank',
    ]),
  )
  const wsUrl = await new Promise<string>((done) => {
    let log = ''
    const timer = setTimeout(
      () => fail(`Chrome did not start. 修复：check that it runs, or point CHROME at another build. Its output:\n${log}`),
      20_000,
    )
    chrome.stderr?.on('data', (chunk: Buffer) => {
      log += chunk
      const url = /DevTools listening on (ws:\/\/\S+)/.exec(log)?.[1]
      if (url) {
        clearTimeout(timer)
        done(url)
      }
    })
  })
  const socket = new WebSocket(wsUrl)
  await new Promise((done) => socket.addEventListener('open', done, { once: true }))
  let nextId = 1
  const pending = new Map<number, { done: (v: Reply) => void; reject: (e: Error) => void }>()
  socket.addEventListener('message', (event) => {
    const msg = JSON.parse(String(event.data))
    const waiter = msg.id ? pending.get(msg.id) : undefined
    if (!waiter) return
    pending.delete(msg.id)
    if (msg.error) waiter.reject(new Error(msg.error.message))
    else waiter.done(msg.result)
  })
  function send(method: string, params: object = {}, sessionId?: string): Promise<Reply> {
    const id = nextId++
    socket.send(JSON.stringify({ id, method, params, sessionId }))
    return new Promise((done, reject) => pending.set(id, { done, reject }))
  }
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
  const page = (method: string, params: object = {}) => send(method, params, sessionId)
  async function tryEvaluate(expression: string) {
    const { result, exceptionDetails } = await page('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })
    if (exceptionDetails)
      throw new Error(`page script failed: ${exceptionDetails.exception?.description ?? exceptionDetails.text}`)
    return result.value
  }
  async function evaluate(expression: string) {
    return tryEvaluate(expression).catch((e: Error) => fail(`${e.message}\n修复：open the page by hand and look at its console.`))
  }
  return {
    page,
    evaluate,
    async until(expression, what) {
      const r = await pollUntil(() => tryEvaluate(expression))
      if (r.ok) return
      const last = r.lastError instanceof Error ? `; last error: ${r.lastError.message}` : ''
      fail(`timed out waiting for ${what}${last}. 修复：open the page by hand and see whether it loads.`)
    },
    async screenshot() {
      const shot = await page('Page.captureScreenshot', { format: 'png' })
      return Buffer.from(shot.data, 'base64')
    },
    // Close Chrome before the work directory goes, or it is still writing its profile there.
    async close() {
      const closed = new Promise((done) => chrome.once('exit', done))
      await send('Browser.close').catch(() => undefined)
      await closed
    },
  }
}

/**
 * Screenshot the panel at `url` in its light theme (the one the page draws on its own) at twice the pixels,
 * 1280×720 CSS px unless given. Fails if the visible page names this machine. Returns the PNG.
 */
export async function shootPanel(chrome: Chrome, url: string, { width = 1280, height = 720 } = {}): Promise<Buffer> {
  await chrome.page('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] })
  await chrome.page('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 2, mobile: false })
  await chrome.page('Page.navigate', { url })
  await chrome.until(PANEL_LOADED, 'the panel to draw')
  await new Promise((r) => setTimeout(r, 600))
  // What the picture can show is the visible text; the copy-for-your-AI payloads hold the repository folder, but
  // they stay hidden in a shot.
  assertAnonymous(await chrome.evaluate('document.body.innerText'), 'the visible text of the panel')
  return chrome.screenshot()
}
