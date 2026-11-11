// Retakes docs/assets/demo.gif (the light scene) from docs/assets/demo.html.
// It runs the real commands on the demo manifest (examples/demo) with a throwaway HOME and a PATH without any
// agent host, passes what they print to the scene in `?data=` with a fresh light panel screenshot, then
// seeks the scene frame by frame and encodes the frames.
// Needs Node 22.18+, Python 3.11+, ffmpeg, gifsicle and Chrome or Chromium (set CHROME to its binary if it is
// elsewhere). gitleaks is used when it is installed, as plugin build does.
// Usage, from the repository root:
//   node scripts/record-demo.ts                  write docs/assets/demo.gif (light)
//   node scripts/record-demo.ts --theme dark     write the dark scene to $TMPDIR/agent-env-demo-dark.gif, for a
//                                                look only (it is not shipped; its panel stays light)
//   node scripts/record-demo.ts --out <file>     write the GIF to <file> instead
//   node scripts/record-demo.ts --mp4 <file>     also write an MP4 of the same frames (not for the repository)
//   node scripts/record-demo.ts --frames 3,10,18 write only those seconds as PNGs (demo-<theme>-<t>s.png), for a look
import { execFileSync, spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  assertAnonymous,
  DEMO_MANIFEST,
  fail,
  fakeHome,
  openChrome,
  root,
  shootPanel,
  startPanel,
  workDir,
} from './chrome.ts'

const FPS = 10
const WIDTH = 1280
const HEIGHT = 760
// The README shows the GIF at most about 1010 px wide; frames are drawn at 1280 and scaled down.
const OUT_WIDTH = 1024
// GitHub proxies README images and drops large ones; stay well under its limit.
const MAX_BYTES = 4 * 1024 * 1024

function option(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  if (i === -1) return undefined
  const value = process.argv[i + 1]
  if (!value || value.startsWith('--')) fail(`${name} needs a value. 修复：see the usage at the top of scripts/record-demo.ts.`)
  return value
}
const framesArg = option('--frames')
const preview = framesArg?.split(',').filter(Boolean).map(Number) ?? null
if (preview && (preview.length === 0 || preview.some(Number.isNaN))) {
  fail('--frames takes seconds separated by commas. 修复：pass something like --frames 3,10,18')
}
const theme = option('--theme') ?? 'light'
if (theme !== 'dark' && theme !== 'light') fail(`--theme is light or dark, not ${theme}. 修复：pass --theme dark or leave it out.`)
const outArg = option('--out')
// Only the light scene goes into the repository.
const out = outArg
  ? resolve(outArg)
  : theme === 'light'
    ? join(root, 'docs/assets/demo.gif')
    : join(tmpdir(), 'agent-env-demo-dark.gif')
const shownOut = out.startsWith(`${root}/`) ? relative(root, out) : out
const mp4Arg = option('--mp4')
const mp4 = mp4Arg ? resolve(mp4Arg) : null

for (const tool of preview ? [] : ['ffmpeg', 'gifsicle']) {
  if (spawnSync(tool, ['--version']).error) {
    fail(`${tool} is not on PATH. 修复：install it (macOS: \`brew install ${tool}\`; Debian/Ubuntu: \`sudo apt install ${tool}\`) and run this again.`)
  }
}

const work = workDir('agent-env-record-')
const cliDir = join(work, 'cli')
const env = fakeHome(cliDir)
const home = env.HOME as string
const M = DEMO_MANIFEST

// What a reader sees: the throwaway HOME as ~ and the repository as ~/agent-env.
function anonymous(text: string): string {
  return text.replaceAll(home, '~').replaceAll(root, '~/agent-env')
}
function run(args: string[], ok = [0]): string[] {
  // stdout and stderr in one pipe, in the order a terminal shows them.
  const r = spawnSync('/bin/sh', ['-c', '"$0" "$@" 2>&1', join(root, 'bin/agent-env'), ...args, '--manifest', M], {
    cwd: root,
    env,
    encoding: 'utf8',
  })
  const text = anonymous(r.stdout ?? '').replace(/\n+$/, '')
  if (r.error || !ok.includes(r.status ?? -1) || !text) {
    fail(`bin/agent-env ${args.join(' ')} failed (exit ${r.status}):\n${text || r.error}\n修复：run it by hand with --manifest ${M} and fix what it reports.`)
  }
  assertAnonymous(text, `the output of bin/agent-env ${args.join(' ')}`)
  return text.split('\n')
}
const shown = (args: string[]) => `bin/agent-env ${args.join(' ')} --manifest "$M"`
const find = (lines: string[], re: RegExp, what: string) =>
  lines.find((l) => re.test(l)) ?? fail(`no line like ${re} in the ${what} output. 修复：update the pattern in scripts/record-demo.ts.`)

// 1. check: the plugins, three of the targets, the one warning and the total.
const checkArgs = ['check']
const check = run(checkArgs)
const checkPick = check.filter((l) =>
  /\[plugins\]|落点「(mac\/claude-code|vps-helper\/hermes|grok-bot\/bot)」|^WARN|^合计/.test(l),
)
// 2. plugin build --all
const buildArgs = ['plugin', 'build', '--all']
const build = run(buildArgs)
// 3. render for the laptop's Claude Code, and where that target composes to
const renderArgs = ['render', '--target', 'mac/claude-code']
const render = run(renderArgs)
const composed = find(check, /落点「mac\/claude-code」/, 'check')
const output = /本机合成到 (\S+?)，/.exec(composed)?.[1] ?? fail(`no output path in: ${composed}`)
// 4. the cloud bot's pointer rule
const pointerArgs = ['plugin', 'pointer', '--target', 'grok-bot/bot']
const pointer = run(pointerArgs)
// The server target: its pull setting as the manifest writes it, and what check says about it.
const manifestText = readFileSync(join(root, M), 'utf8')
const serverBlock = manifestText.slice(manifestText.indexOf('id = "vps-helper/hermes"'))
const pull = find(serverBlock.split('\n'), /^pull = /, 'manifest')
const remote = find(check, /落点「vps-helper\/hermes」/, 'check')
// 5. doctor on the laptop, after the steps a user would take: the composed file, the @-include, the Codex link.
//    pi and the Hermes chat bot are left undone, so doctor has findings to show.
mkdirSync(join(home, '.codex'), { recursive: true })
run([...renderArgs, '--write'])
writeFileSync(join(home, '.claude/CLAUDE.md'), '@AGENTS.md\n')
spawnSync('ln', ['-s', join(home, '.claude/AGENTS.md'), join(home, '.codex/AGENTS.md')])
const doctorArgs = ['doctor', '--env', 'mac']
const doctor = run(doctorArgs, [0, 1])

// The light panel (the one the README shows), drawn at the size the scene shows it.
const panelUrl = await startPanel(join(work, 'panel'))
const chrome = await openChrome(work)
const panelPng = join(work, 'panel.png')
writeFileSync(panelPng, await shootPanel(chrome, panelUrl, { width: 1040, height: 556 }))

const data = {
  manifest: M,
  check: { cmd: shown(checkArgs), lines: checkPick },
  build: { cmd: shown(buildArgs), lines: build },
  render: { cmd: shown(renderArgs), lines: render.slice(0, 20), output },
  pointer: { cmd: shown(pointerArgs), lines: pointer },
  pull,
  remote,
  doctor: { cmd: shown(doctorArgs), lines: doctor },
  panel: pathToFileURL(panelPng).href,
}

// The scene, one seek and one screenshot per frame.
await chrome.page('Emulation.setEmulatedMedia', { features: [] })
await chrome.page('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false })
const scene = pathToFileURL(join(root, 'docs/assets/demo.html'))
scene.search = new URLSearchParams({ record: '1', theme, data: JSON.stringify(data) }).toString()
await chrome.page('Page.navigate', { url: scene.href })
await chrome.until('window.demo !== undefined', 'the scene script')
await chrome.evaluate('window.demo.ready')
const total: number = await chrome.evaluate('window.demo.total')
const frames = join(work, 'frames')
mkdirSync(frames)
const previewDir = join(tmpdir(), 'agent-env-demo-frames')
if (preview) mkdirSync(previewDir, { recursive: true })
const times = preview ?? Array.from({ length: Math.ceil(total * FPS) }, (_, i) => i / FPS)
for (const [i, t] of times.entries()) {
  await chrome.evaluate(`window.demo.seek(${t})`)
  const name = preview ? `demo-${theme}-${t}s.png` : `${String(i).padStart(4, '0')}.png`
  writeFileSync(join(preview ? previewDir : frames, name), await chrome.screenshot())
}
await chrome.close()

if (preview) {
  console.log(`wrote ${times.map((t) => join(previewDir, `demo-${theme}-${t}s.png`)).join('\n      ')}`)
  process.exit(0)
}

// One palette for the whole clip with ordered dithering, so pixels that do not move stay identical
// between frames; gifsicle then trims what the eye does not see.
execFileSync(
  'ffmpeg',
  [
    '-y',
    '-v',
    'error',
    '-framerate',
    String(FPS),
    '-i',
    join(frames, '%04d.png'),
    '-filter_complex',
    `scale=${OUT_WIDTH}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`,
    out,
  ],
  { stdio: 'inherit' },
)
execFileSync('gifsicle', ['-O3', '--lossy=30', '--batch', out], { stdio: 'inherit' })
const bytes = statSync(out).size
console.log(
  `wrote ${shownOut} (${times.length} frames, ${(times.length / FPS).toFixed(1)} s, ${(bytes / 1024 / 1024).toFixed(2)} MB)`,
)
if (mp4) {
  const r = spawnSync(
    'ffmpeg',
    ['-y', '-v', 'error', '-framerate', String(FPS), '-i', join(frames, '%04d.png'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-movflags', '+faststart', mp4],
    { stdio: 'inherit' },
  )
  if (r.status !== 0) fail('ffmpeg could not write the MP4. 修复：use an ffmpeg built with libx264 (`ffmpeg -encoders | grep libx264`), then run this again.')
  console.log(`wrote ${mp4}`)
}
if (bytes > MAX_BYTES) {
  fail('The GIF is over 4 MB, which GitHub may not show in the README. 修复：shorten the scene in demo.html or lower FPS, then retake.')
}
process.exit(0)
