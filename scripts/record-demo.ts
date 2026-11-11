// Retakes the README demo from the scene in docs/assets/demo.html: docs/assets/demo.gif (English) and
// docs/assets/demo.zh-CN.gif (Chinese). It runs the real commands on the demo manifest (examples/demo) with a
// throwaway HOME and a PATH without any agent host, builds the scenes from what they print, adds a fresh panel
// screenshot, passes it all to the scene in `?data=`, then seeks the scene frame by frame and encodes the frames.
// The English clip only shows commands whose output is English; the script stops if any Chinese gets into it.
// Needs Node 22.18+, Python 3.11+, jq, ffmpeg, gifsicle and Chrome or Chromium (set CHROME to its binary if it is
// elsewhere). gitleaks is used when it is installed, as plugin build does.
// Usage, from the repository root:
//   node scripts/record-demo.ts                     write docs/assets/demo.gif (English)
//   node scripts/record-demo.ts --lang zh           write docs/assets/demo.zh-CN.gif (Chinese)
//   node scripts/record-demo.ts --mp4 <file>        also write an MP4 (1280 wide, 30 fps) of the same scene
//   node scripts/record-demo.ts --out <file>        write the GIF to <file> instead
//   node scripts/record-demo.ts --frames 0,9,19     write only those seconds as PNGs (demo-<lang>-<t>s.png)
import { spawnSync } from 'node:child_process'
import { mkdirSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  assertAnonymous,
  CJK,
  DEMO_MANIFEST,
  fail,
  fakeHome,
  openChrome,
  PANEL_SHOTS,
  root,
  shootPanel,
  startPanel,
  which,
  workDir,
} from './chrome.ts'

const GIF_FPS = 10
const MP4_FPS = 30
const WIDTH = 1280
const HEIGHT = 760
// The README shows the GIF at most about 1010 px wide; frames are drawn at 1280 and scaled down.
const OUT_WIDTH = 1024
// GitHub proxies README images and drops large ones; stay well under its limit.
const MAX_BYTES = 4 * 1024 * 1024
// The address bar shows the dev server's default port (panel/dev-server.ts), not the one this run picked.
const PANEL_ADDRESS = '127.0.0.1:17392'

function option(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  if (i === -1) return undefined
  const value = process.argv[i + 1]
  if (!value || value.startsWith('--')) fail(`${name} needs a value. 修复：see the usage at the top of scripts/record-demo.ts.`)
  return value
}
const lang = option('--lang') ?? 'en'
if (lang !== 'en' && lang !== 'zh') fail(`--lang is en or zh, not ${lang}. 修复：pass --lang zh or leave it out.`)
const framesArg = option('--frames')
const preview = framesArg?.split(',').filter(Boolean).map(Number) ?? null
if (preview && (preview.length === 0 || preview.some(Number.isNaN))) {
  fail('--frames takes seconds separated by commas. 修复：pass something like --frames 0,9,19')
}
const mp4Arg = option('--mp4')
const mp4 = mp4Arg ? resolve(mp4Arg) : null
const outArg = option('--out')
const out = outArg ? resolve(outArg) : join(root, 'docs/assets', lang === 'en' ? 'demo.gif' : 'demo.zh-CN.gif')
const shownOut = out.startsWith(`${root}/`) ? relative(root, out) : out

for (const tool of ['jq', ...(preview ? [] : ['ffmpeg', 'gifsicle'])]) {
  if (!which(tool)) {
    fail(`${tool} is not on PATH. 修复：install it (macOS: \`brew install ${tool}\`; Debian/Ubuntu: \`sudo apt install ${tool}\`) and run this again.`)
  }
}

const work = workDir('agent-env-record-')
const env = { ...fakeHome(join(work, 'cli')), AGENT_ENV_MANIFEST: DEMO_MANIFEST }
const home = env.HOME as string
const plugins = join(home, '.local/state/agent-env/plugins')

// What a reader sees: the throwaway HOME as ~ and the repository as ~/agent-env.
const anonymous = (text: string) => text.replaceAll(home, '~').replaceAll(root, '~/agent-env')
/** Run `cmd` in sh from `cwd` (the repository unless given), stdout and stderr in one stream, as a terminal shows it. */
function sh(cmd: string, { cwd = root, ok = [0], quiet = false } = {}): string[] {
  const r = spawnSync('/bin/sh', ['-c', `{ ${cmd} ; } 2>&1`], { cwd, env, encoding: 'utf8' })
  const text = anonymous(r.stdout ?? '').replace(/\n+$/, '')
  if (r.error || !ok.includes(r.status ?? -1) || (!text && !quiet)) {
    fail(`\`${cmd}\` failed (exit ${r.status}):\n${text || r.error}\n修复：run it by hand from the repository root with AGENT_ENV_MANIFEST=${DEMO_MANIFEST} and fix what it reports.`)
  }
  assertAnonymous(text, `the output of \`${cmd}\``)
  return text.split('\n')
}
const step = (cmd: string, mode: string, opts: { cwd?: string; ok?: number[] } = {}) => ({ cmd, mode, lines: sh(cmd, opts) })

// The plugins first: both clips show them. Then what a user does on the laptop, so status and doctor have
// something to compare: the composed rules, the @-include and the Codex link (pi and the Hermes chat bot stay undone).
const build = step('bin/agent-env plugin build --all', 'cli')
sh('bin/agent-env render --target mac/claude-code --write')
writeFileSync(join(home, '.claude/CLAUDE.md'), '@AGENTS.md\n')
mkdirSync(join(home, '.codex'), { recursive: true })
sh(`ln -s "$HOME/.claude/AGENTS.md" "$HOME/.codex/AGENTS.md"`, { quiet: true })

// Two targets from one rules source: the CLI on the laptop and the chat bot next to it.
const render = (target: string) => step(`bin/agent-env render --target ${target} | grep -Ev '^(<!--|$)'`, 'md')
const cli = render('mac/claude-code')
const bot = render('mac/hermes')
const only = (a: string[], b: string[]) => a.filter((l) => l.trim() && !b.includes(l))
const renderScene = (title: string) => ({
  chip: 'render',
  title,
  layout: 'two',
  terms: [
    { env: 'mac/claude-code', path: '~/agent-env', accent: '#43721d', steps: [cli], markLines: only(cli.lines, bot.lines) },
    { env: 'mac/hermes', path: '~/agent-env', accent: '#8250df', steps: [bot], markLines: only(bot.lines, cli.lines) },
  ],
})

const scenes =
  lang === 'en'
    ? [
        {
          chip: 'habits.toml',
          title: 'One manifest lists every plugin and who may see it',
          terms: [
            {
              env: 'mac',
              path: '~/agent-env/examples/demo',
              steps: [step(`grep -A2 '^\\[plugins' manifest/habits.toml`, 'toml', { cwd: join(root, 'examples/demo') })],
              mark: '^visibility',
            },
          ],
        },
        {
          chip: 'plugin build',
          title: 'Each plugin builds into its own installable repository',
          terms: [
            {
              env: 'mac',
              path: '~/.local/state/agent-env/plugins',
              steps: [
                step(`jq -r '[.name, .visibility, .description] | @tsv' */agent-env-plugin.json`, 'tsv', { cwd: plugins }),
                step('ls -1A demo-public', 'plain', { cwd: plugins }),
              ],
              mark: '^\\.(claude|cursor)-plugin$',
            },
          ],
        },
        renderScene('One rules source, composed for each target by its role'),
        {
          chip: 'status --json',
          title: 'status checks every habit on every target',
          terms: [
            {
              env: 'mac',
              path: '~/agent-env',
              steps: [
                step(`bin/agent-env status --json | jq -r '.targets[] | [.id, (.habits[] | .state)] | @tsv'`, 'tsv', { ok: [0, 1] }),
                step(`bin/agent-env status --json | jq -c .summary`, 'json', { ok: [0, 1] }),
              ],
              mark: '\tmissing',
            },
          ],
        },
      ]
    : [
        {
          chip: 'check',
          title: '一份清单，写明每个插件给谁看、每个落点用哪些',
          size: 14,
          terms: [
            {
              env: 'mac',
              path: '~/agent-env',
              steps: [step(`bin/agent-env check | grep -E '\\[(plugins|targets)\\]'`, 'cli')],
              mark: '落点「mac/claude-code」',
            },
          ],
        },
        {
          chip: 'plugin build',
          title: '每个插件构建成一个能直接安装的仓库',
          size: 15,
          terms: [
            {
              env: 'mac',
              path: '~/agent-env',
              steps: [build, step('ls -1A ~/.local/state/agent-env/plugins/demo-public', 'plain')],
              mark: '^\\.(claude|cursor)-plugin$',
            },
          ],
        },
        renderScene('同一份规则源，按落点的角色各自合成'),
        {
          chip: 'doctor',
          title: '逐个落点体检，每条发现都带修复命令',
          size: 13,
          terms: [{ env: 'mac', path: '~/agent-env', steps: [step('bin/agent-env doctor --env mac', 'cli', { ok: [0, 1] })], mark: '修复：' }],
        },
      ]

// The panel, in the clip's language, at the width the browser window shows it; then where its key rows sit.
const { fixture, tab } = PANEL_SHOTS[lang]
const panelUrl = await startPanel(join(work, 'panel'), fixture)
const chrome = await openChrome(work, lang === 'en' ? 'en-US' : 'zh-CN')
const panelPng = join(work, 'panel.png')
const PANEL_H = 900
writeFileSync(panelPng, await shootPanel(chrome, panelUrl, { lang, tab, width: 1100, height: PANEL_H }))
const boxes = await chrome.evaluate(`(() => {
  // Rows run the page's full width; the part that matters ends before the last column, so the camera can push in.
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, Math.min(r.width, 840), r.height] }
  const union = (a, b) => { const x = Math.min(a[0], b[0]), y = Math.min(a[1], b[1]); return [x, y, Math.max(a[0] + a[2], b[0] + b[2]) - x, Math.max(a[1] + a[3], b[1] + b[3]) - y] }
  const seen = (el) => el && el.getClientRects().length > 0
  if (${JSON.stringify(tab)} === 'matrix') {
    const rows = [...document.querySelectorAll('tr')].filter(seen)
    const row = (name) => rows.find((r) => (r.cells[0]?.innerText ?? '').trim().startsWith(name))
    const first = row('Claude Code'), ring = row('Hermes'), ring2 = row('Codex')
    if (!first || !ring || !ring2) return null
    return { focus: union(box(first), box(ring)), ring: box(ring), ring2: box(ring2) }
  }
  const todos = [...document.querySelectorAll('.todo')].filter(seen)
  if (todos.length < 3) return null
  return { focus: union(box(todos[0]), box(todos[2])), ring: box(todos[0]), ring2: box(todos[2]) }
})()`)
if (!boxes) fail(`the panel on ${fixture} has not the rows the scene zooms into. 修复：update the row lookup in scripts/record-demo.ts.`)
scenes.push({
  chip: lang === 'en' ? 'panel' : '面板',
  title: lang === 'en' ? 'The panel shows every machine and agent on one page' : '一页看全部机器和 agent，待办排在最前',
  layout: 'panel',
  // @ts-expect-error the panel scene carries a panel instead of terminals
  panel: { src: pathToFileURL(panelPng).href, url: PANEL_ADDRESS, height: PANEL_H, ...boxes },
})

const cover =
  lang === 'en'
    ? {
        tagline: 'One manifest for the rules and skills of every AI agent you run',
        tiles: [
          { icon: 'laptop', name: 'mac', sub: 'laptop · Claude Code, Codex, Hermes' },
          { icon: 'terminal', name: 'wsl', sub: 'Windows WSL · Claude Code, Codex' },
          { icon: 'server', name: 'vps-helper', sub: 'team server · Hermes bot, pulls on a timer' },
          { icon: 'cloud', name: 'grok-bot', sub: 'cloud chat bot · pulls by itself' },
        ],
      }
    : {
        tagline: '一份清单，管好你每个 AI agent 的规则和技能',
        tiles: [
          { icon: 'laptop', name: 'mac', sub: '笔记本 · Claude Code、Codex、Hermes' },
          { icon: 'terminal', name: 'wsl', sub: 'Windows WSL · Claude Code、Codex' },
          { icon: 'server', name: 'vps-helper', sub: '团队服务器 · Hermes bot，定时自己拉' },
          { icon: 'cloud', name: 'grok-bot', sub: '云端 bot · 按指路规则自己拉' },
        ],
      }
const data = { lang, cover: { ...cover, url: 'github.com/Adonis0123/agent-env' }, scenes }
if (lang === 'en') {
  const text = JSON.stringify({ ...data, scenes: scenes.map((s) => ({ ...s, panel: undefined })) })
  const hit = CJK.exec(text)
  if (hit) {
    const at = hit.index
    fail(`the English clip would show Chinese: …${text.slice(Math.max(0, at - 60), at + 20)}… 修复：drop that command or line from the English scenes.`)
  }
}

// The scene, one seek and one screenshot per frame.
await chrome.page('Network.setExtraHTTPHeaders', { headers: {} })
await chrome.page('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false })
const scene = pathToFileURL(join(root, 'docs/assets/demo.html'))
scene.search = new URLSearchParams({ record: '1', data: JSON.stringify(data) }).toString()
await chrome.page('Page.navigate', { url: scene.href })
await chrome.until('window.demo !== undefined', 'the scene script')
await chrome.evaluate('window.demo.ready')
const total: number = await chrome.evaluate('window.demo.total')
const starts: number[] = await chrome.evaluate('window.demo.starts')
const fps = mp4 ? MP4_FPS : GIF_FPS
const frames = join(work, 'frames')
mkdirSync(frames)
const previewDir = join(tmpdir(), 'agent-env-demo-frames')
if (preview) mkdirSync(previewDir, { recursive: true })
const times = preview ?? Array.from({ length: Math.ceil(total * fps) }, (_, i) => i / fps)
for (const [i, t] of times.entries()) {
  await chrome.evaluate(`window.demo.seek(${t})`)
  const name = preview ? `demo-${lang}-${t}s.png` : `${String(i).padStart(4, '0')}.png`
  writeFileSync(join(preview ? previewDir : frames, name), await chrome.screenshot())
}
await chrome.close()

if (preview) {
  console.log(`total ${total.toFixed(1)} s, scenes start at ${starts.join(', ')} s; wrote\n  ${times.map((t) => join(previewDir, `demo-${lang}-${t}s.png`)).join('\n  ')}`)
  process.exit(0)
}

function ffmpeg(args: string[], what: string) {
  const r = spawnSync('ffmpeg', ['-y', '-v', 'error', '-framerate', String(fps), '-i', join(frames, '%04d.png'), ...args], {
    stdio: 'inherit',
  })
  if (r.status !== 0) fail(`ffmpeg could not write ${what}. 修复：check the ffmpeg error above (an MP4 needs an ffmpeg built with libx264).`)
}
// One palette for the whole clip with ordered dithering, so pixels that do not move stay identical
// between frames; gifsicle then trims what the eye does not see.
ffmpeg(
  [
    '-filter_complex',
    `fps=${GIF_FPS},scale=${OUT_WIDTH}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`,
    out,
  ],
  shownOut,
)
const lossy = spawnSync('gifsicle', ['-O3', '--lossy=80', '--batch', out], { stdio: 'inherit' })
if (lossy.status !== 0) fail('gifsicle failed. 修复：check its error above, or reinstall gifsicle.')
const bytes = statSync(out).size
console.log(`wrote ${shownOut} (${total.toFixed(1)} s, ${Math.round(total * GIF_FPS)} frames at ${GIF_FPS} fps, ${(bytes / 1024 / 1024).toFixed(2)} MB)`)
if (mp4) {
  ffmpeg(['-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', mp4], mp4)
  console.log(`wrote ${mp4} (${(statSync(mp4).size / 1024 / 1024).toFixed(2)} MB)`)
}
if (bytes > MAX_BYTES) {
  fail('The GIF is over 4 MB, which GitHub may not show in the README. 修复：shorten the scene in demo.html or lower GIF_FPS, then retake.')
}
process.exit(0)
