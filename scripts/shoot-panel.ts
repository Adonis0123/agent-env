// Retakes the README panel screenshot docs/assets/panel.png: the light panel, 1280×720 CSS px at twice the pixels,
// of the panel dev server on panel/fixtures/v8-plugins.json.
// Needs Node 22.18+ and Chrome or Chromium (set CHROME to its binary if it is elsewhere).
// Usage, from the repository root:
//   node scripts/shoot-panel.ts            write docs/assets/panel.png
//   node scripts/shoot-panel.ts --out <d>  write it to <d> instead, for a look before replacing
// The panel runs with a throwaway HOME, so nothing of this machine shows up in the shot.
import { mkdirSync, writeFileSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { openChrome, root, shootPanel, startPanel, workDir } from './chrome.ts'

const outArg = process.argv.indexOf('--out')
const out = outArg === -1 ? join(root, 'docs/assets') : resolve(process.argv[outArg + 1] ?? '.')
mkdirSync(out, { recursive: true })

const work = workDir('agent-env-shoot-')
const url = await startPanel(join(work, 'panel'))
const chrome = await openChrome(work)
const file = join(out, 'panel.png')
writeFileSync(file, await shootPanel(chrome, url))
console.log(`wrote ${file.startsWith(`${root}/`) ? relative(root, file) : file}`)
await chrome.close()
process.exit(0)
