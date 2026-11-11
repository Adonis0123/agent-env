// Retakes the README panel screenshots: docs/assets/panel.png (English) and docs/assets/panel.zh-CN.png (Chinese),
// the light panel at 1280×860 CSS px and twice the pixels, of the panel dev server on the fixtures in PANEL_SHOTS
// (scripts/chrome.ts).
// Needs Node 22.18+ and Chrome or Chromium (set CHROME to its binary if it is elsewhere).
// Usage, from the repository root:
//   node scripts/shoot-panel.ts            write both PNGs
//   node scripts/shoot-panel.ts --out <d>  write them to <d> instead, for a look before replacing
// The panel runs with a throwaway HOME, so nothing of this machine shows up in the shots.
import { mkdirSync, writeFileSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { openChrome, PANEL_SHOTS, root, shootPanel, startPanel, workDir } from './chrome.ts'

const outArg = process.argv.indexOf('--out')
const out = outArg === -1 ? join(root, 'docs/assets') : resolve(process.argv[outArg + 1] ?? '.')
mkdirSync(out, { recursive: true })

const work = workDir('agent-env-shoot-')
const chrome = await openChrome(work)
for (const [lang, file] of [
  ['en', 'panel.png'],
  ['zh', 'panel.zh-CN.png'],
] as const) {
  const { fixture, tab } = PANEL_SHOTS[lang]
  const url = await startPanel(join(work, `panel-${lang}`), fixture)
  const path = join(out, file)
  writeFileSync(path, await shootPanel(chrome, url, { lang, tab, height: 860 }))
  console.log(`wrote ${path.startsWith(`${root}/`) ? relative(root, path) : path}`)
}
await chrome.close()
process.exit(0)
