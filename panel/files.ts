// Looking at a listed file from the page: the short id the page carries for each path it offers to show, the
// read-only preview the server gives for one id, and the buttons next to a path.
// One set of paths (fileSet): a path enters it when the page draws its 「查看内容」 button, and the route reads only
// what is in the set of that render. The page sends an id, never a path (index.ts "/api/file").
// Opening a file in an app is the portal's job (its shared /_shared/open.js control); this module only says whether
// that control is there (openTargets).
import { createHash } from "node:crypto";
import { constants, statSync } from "node:fs";
import { open, opendir, realpath, stat } from "node:fs/promises";
import { basename, join, sep } from "node:path";
import type { T } from "./i18n.ts";

/** At most this much of a file is shown. */
export const PREVIEW_BYTES = 256 * 1024;
/** A NUL byte in this much of the start means the file is not text. */
export const SNIFF_BYTES = 8 * 1024;
/** At most this many entries of a folder are shown. */
export const FOLDER_ENTRIES = 200;
/** At most this many entries of a folder are read; the shown ones are the first of these, sorted. */
export const FOLDER_READ = 2000;
/** At most this many paths get a view button in one render, each at most this long. */
export const LISTED_PATHS = 500;
export const PATH_CHARS = 1024;

/** The path as the page writes it, `~` or `~/…`; null for a path outside the home folder or with a `..` step in it
 *  (never previewed). */
export function tilde(path: string, home = ""): string | null {
  if (path.split("/").includes("..")) return null;
  if (path === "~" || path.startsWith("~/")) return path;
  if (home && home !== "/" && path.startsWith(home + "/")) return "~/" + path.slice(home.length + 1);
  return null;
}

/** Folders whose content is never shown, wherever they are in the path (also a file of that very name). */
const SECRET_FOLDERS = new Set([".ssh", ".gnupg", ".aws", ".kube", ".docker", "keychains", "secrets", "secret", "credentials", "tokens", "keys", "private"]);
/** Key files, whatever their extension: *.env, .env*, *.pem, *.key, *.p12, *.pfx, id_rsa*, id_ed25519*, .netrc,
 *  .npmrc, .pypirc, auth.json. */
const KEY_FILE = /^(?:.*\.env|\.env.*|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_rsa.*|id_ed25519.*|\.netrc|\.npmrc|\.pypirc|auth\.json)$/i;
/** Names that say what is inside: *credentials*, *secret*, *token*. Not for documents (.md, .mdx, .txt): a page of
 *  rules about tokens is something to read. */
const SECRET_WORD = /credentials|secret|token/i;
const DOCUMENT = /\.(?:md|mdx|txt)$/i;

/** A path that looks like it holds keys or passwords: it gets no view button and the route never reads it. Checked
 *  on the path as listed and again on where it really leads. Letter case does not matter (some disks ignore it). */
export function isSecretPath(path: string): boolean {
  const parts = path.split("/").filter(Boolean).map((p) => p.toLowerCase());
  if (parts.some((p) => SECRET_FOLDERS.has(p) || p.startsWith(".secret"))) return true;
  const name = parts.at(-1) ?? "";
  return KEY_FILE.test(name) || (SECRET_WORD.test(name) && !DOCUMENT.test(name));
}

/** The id the page carries for a path: the same path gives the same id, and the id does not say the path. */
export const fileId = (tildePath: string) => createHash("sha256").update(tildePath).digest("hex").slice(0, 16);
const ID = /^[0-9a-f]{16}$/;
export const isFileId = (id: string) => ID.test(id);

/** The paths one render offers to show. `issue` is called by fileRow when it draws a view button and answers the
 *  button's id; `path` is what the route looks an id up with. So the route can read exactly what the page showed a
 *  button for: nothing a status merely mentions, nothing of another machine. */
export interface FileSet {
  /** the id for this path's view button; null when it gets none (outside home, a secret, too long, the set is full) */
  issue(path: string): string | null;
  path(id: string): string | undefined;
  /** whether a `~/…` path is a folder now, for the button's words (查看文件夹); false when that cannot be told */
  isFolder(tildePath: string): boolean;
  readonly size: number;
}
export function fileSet(home = ""): FileSet {
  const byId = new Map<string, string>();
  return {
    issue(path) {
      const p = tilde(path, home);
      if (!p || p.length > PATH_CHARS || isSecretPath(p)) return null;
      const id = fileId(p);
      if (!byId.has(id)) {
        if (byId.size >= LISTED_PATHS) return null;
        byId.set(id, p);
      }
      return id;
    },
    path: (id) => byId.get(id),
    isFolder(tildePath) {
      if (!home || home === "/") return false;
      try {
        return statSync(join(home, tildePath.slice(2))).isDirectory();
      } catch {
        return false;
      }
    },
    get size() {
      return byId.size;
    },
  };
}

export type Preview =
  /** the text of a file; `cut`: only the first PREVIEW_BYTES are here */
  | { state: "text"; path: string; text: string; cut: boolean }
  /** names in a folder, folders first (with a trailing "/"). `total`: how many were read; `cut`: not all of those
   *  are in `entries`; `more`: reading stopped at FOLDER_READ, the folder holds more than `total` */
  | { state: "folder"; path: string; entries: string[]; total: number; cut: boolean; more: boolean }
  | { state: "gone"; path: string }
  | { state: "binary"; path: string }
  | { state: "error"; path: string }
  /** not under the home folder once links are followed, or a secret: answered like an id nobody issued */
  | { state: "outside" };

interface Identity {
  dev: bigint;
  ino: bigint;
  isFile(): boolean;
  isDirectory(): boolean;
}
interface Entry {
  name: string;
  isDirectory(): boolean;
}
/** The file system calls readPreview makes; tests hand in their own to stand in for a race or a huge folder. */
export interface PreviewIo {
  realpath(path: string): Promise<string>;
  stat(path: string): Promise<Identity>;
  open(path: string, flags: number): Promise<{ fd: number; stat(): Promise<Identity & { size: bigint }>; read(buf: Buffer, offset: number, length: number, position: number): Promise<{ bytesRead: number }>; close(): Promise<void> }>;
  opendir(path: string): Promise<AsyncIterable<Entry>>;
}
export const nodeIo: PreviewIo = {
  realpath: (path) => realpath(path),
  stat: (path) => stat(path, { bigint: true }),
  open: async (path, flags) => {
    const h = await open(path, flags);
    return { fd: h.fd, stat: () => h.stat({ bigint: true }), read: (buf, offset, length, position) => h.read(buf, offset, length, position), close: () => h.close() };
  },
  opendir: (path) => opendir(path),
};

const byName = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
const inside = (root: string, real: string) => real === root || real.startsWith(root + sep);
const missing = (e: unknown) => ["ENOENT", "ENOTDIR"].includes(String((e as NodeJS.ErrnoException).code));

/** Reads a listed path for the page. Regular files and folders only; links are followed and the result must still be
 *  inside the home folder and not a secret (isSecretPath). Never throws, never waits on a pipe or a device.
 *
 *  How it opens: resolve the path, open what it resolved to (no link at the last step, non-blocking so a pipe does
 *  not hang the read), look at the open descriptor (a regular file or a folder, nothing else), then resolve the
 *  listed path once more and require that it still leads, inside the home folder, to the very file the descriptor
 *  holds (same device and inode). A file is then read through that descriptor only.
 *  This keeps a path that is swapped between the check and the open (a folder on the way replaced by a link out of
 *  the home folder, a file replaced by a pipe) from being read: what is read is what the checks saw.
 *  It does not defend against someone who swaps a folder away and back within the read: that takes the right to
 *  rename folders inside the home folder, which is already everything this user may do, so nothing is gained by it.
 *  A folder's names cannot be read through a descriptor here (Node has no such call; /dev/fd works on Linux only),
 *  so where that fails the folder is read by its resolved path and the same identity check is made again after. */
export async function readPreview(home: string, tildePath: string, io: PreviewIo = nodeIo): Promise<Preview> {
  if (!home || home === "/" || !(tildePath === "~" || tildePath.startsWith("~/"))) return { state: "outside" };
  if (isSecretPath(tildePath)) return { state: "outside" };
  const path = tildePath;
  const listed = join(home, tildePath.slice(2));
  /** Where the listed path leads now: null when that is outside the home folder or a secret. */
  const resolve = async (): Promise<string | null> => {
    const root = await io.realpath(home);
    const real = await io.realpath(listed);
    return inside(root, real) && !isSecretPath(real.slice(root.length)) ? real : null;
  };
  try {
    await io.realpath(home);
  } catch {
    return { state: "error", path };
  }
  try {
    const real = await resolve();
    if (!real) return { state: "outside" };
    const file = await io.open(real, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const held = await file.stat();
      if (!held.isFile() && !held.isDirectory()) return { state: "error", path };
      /** The listed path still leads to what the descriptor holds. */
      const same = async (): Promise<"same" | "outside" | "other"> => {
        const again = await resolve();
        if (!again) return "outside";
        const now = await io.stat(again);
        return now.dev === held.dev && now.ino === held.ino ? "same" : "other";
      };
      const first = await same();
      if (first !== "same") return first === "outside" ? { state: "outside" } : { state: "error", path };

      if (held.isDirectory()) {
        const names: Array<{ name: string; dir: boolean }> = [];
        let more = false;
        const take = async (from: string) => {
          for await (const d of await io.opendir(from)) {
            if (names.length >= FOLDER_READ) {
              more = true;
              break; // leaving the loop closes the folder
            }
            names.push({ name: d.name, dir: d.isDirectory() });
          }
        };
        try {
          await take(`/dev/fd/${file.fd}`);
        } catch {
          names.length = 0;
          more = false;
          await take(real);
          const after = await same();
          if (after !== "same") return after === "outside" ? { state: "outside" } : { state: "error", path };
        }
        const sorted = [
          ...names.filter((d) => d.dir).map((d) => d.name + "/").sort(byName),
          ...names.filter((d) => !d.dir).map((d) => d.name).sort(byName),
        ];
        return { state: "folder", path, entries: sorted.slice(0, FOLDER_ENTRIES), total: sorted.length, cut: sorted.length > FOLDER_ENTRIES, more };
      }

      const buf = Buffer.alloc(Number(held.size < BigInt(PREVIEW_BYTES + 1) ? held.size : BigInt(PREVIEW_BYTES + 1)));
      let got = 0;
      while (got < buf.length) {
        const { bytesRead } = await file.read(buf, got, buf.length - got, got);
        if (!bytesRead) break;
        got += bytesRead;
      }
      const cut = got > PREVIEW_BYTES;
      const shown = buf.subarray(0, Math.min(got, PREVIEW_BYTES));
      if (shown.subarray(0, SNIFF_BYTES).includes(0)) return { state: "binary", path };
      let text: string;
      try {
        // strict: bytes that are not UTF-8 mean this is not text. Stream mode at a cut leaves out the character
        // the cut went through, instead of calling it invalid
        text = new TextDecoder("utf-8", { fatal: true }).decode(shown, { stream: cut });
      } catch {
        return { state: "binary", path };
      }
      return { state: "text", path, text, cut };
    } finally {
      await file.close();
    }
  } catch (e) {
    return { state: missing(e) ? "gone" : "error", path };
  }
}

/** Tells the newest request from older ones, so a slow answer to an earlier click never replaces a later one's
 *  (the page keeps one per preview). `begin` starts a request and gives its number; `current` says whether that
 *  number is still the newest; `cancel` (the preview was closed) makes every answer still on its way stale.
 *  Embedded into the page as it is (page.ts layout): it uses nothing from this module. */
export function requestGuard() {
  let newest = 0;
  return {
    begin: () => ++newest,
    current: (n: number) => n === newest,
    cancel: () => {
      newest++;
    },
  };
}

/** Whether the portal's "Open in" control can be used from this page: its API on the page's own origin answers with
 *  at least one app. Anything else (the panel run on its own, an older portal, a network error) is a quiet no.
 *  Embedded into the page as it is (page.ts layout): it uses nothing from this module. */
export async function openTargets(get: (url: string, init: { headers: Record<string, string>; cache: "no-store" }) => Promise<{ ok: boolean; json(): Promise<unknown> }>): Promise<boolean> {
  try {
    const res = await get("/_shared/api/open", { headers: { accept: "application/json" }, cache: "no-store" });
    if (!res.ok) return false;
    const body = (await res.json()) as { targets?: unknown } | null;
    const targets = body && typeof body === "object" ? body.targets : null;
    if (Array.isArray(targets)) return targets.length > 0;
    return !!targets && typeof targets === "object" && Object.keys(targets).length > 0;
  } catch {
    return false;
  }
}

// ---- the buttons next to a path ----

export interface FileKit {
  t: T;
  esc: (s: unknown) => string;
  /** the home folder the server hides, so a path written in full is still known to be under it */
  home?: string;
  /** the set of this render: a view button is drawn only for a path it takes (no set: no view buttons) */
  files?: FileSet;
}

/** One path with what the reader can do with it: copy it; on this machine also look at it in the page (unless it
 *  looks like a secret), and (when the portal offers it, filled in by the page script) open it in an app.
 *  `local`: the path is this machine's. `label`: what the path is, when several rows stand together. */
export function fileRow(k: FileKit, path: string, local: boolean, label = ""): string {
  if (!path) return "";
  const { t, esc } = k;
  const name = label ? `<span class="fv-label">${esc(label)}</span>` : "";
  const copy = `<button type="button" class="linkish" data-copy="${esc(path)}" data-tip="${esc(path)}">${esc(t("Copy path"))}</button>`;
  const shown = local ? tilde(path, k.home) : null;
  if (!shown) return `<div class="fv"><p class="fv-acts">${name}${copy}${local ? "" : `<span class="fv-far">${esc(t("This file is on another computer"))}</span>`}</p></div>`;
  const id = k.files?.issue(shown) ?? null;
  const folder = !!id && !!k.files?.isFolder(shown);
  // the page script turns the words into 收起 while the block is open, and back
  const view = id ? `<button type="button" class="linkish" data-view="${id}"${folder ? " data-folder" : ""} aria-expanded="false">${esc(t(folder ? "View folder" : "View"))}</button>` : "";
  const slot = `<span class="fv-open" data-open-path="${esc(shown)}" data-open-name="${esc(basename(shown))}" hidden></span>`;
  const body = id ? `<div class="fv-body" hidden><p class="fv-cap">${esc(shown)}</p><p class="fv-state" role="status"></p><p class="fv-intro" hidden>${esc(t("In this folder:"))}</p><pre class="fv-text" tabindex="0" hidden></pre></div>` : "";
  return `<div class="fv"${id ? " data-fv" : ""}><p class="fv-acts">${name}${copy}${view}${slot}</p>${body}</div>`;
}

/** Styles of the row; page.ts adds them to its one CSS string. No animation: the block opens on the click's frame. */
export const FILES_CSS = `
  /* a path's buttons (copy, view, open) and the read-only preview under them */
  .fv { display: grid; gap: 4px; min-width: 0; }
  .fv-acts { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; min-width: 0; font-size: 12px; color: var(--kit-muted, #666); }
  .fv-label { color: var(--kit-text, inherit); }
  .fv-open { display: inline-flex; }
  /* the preview is set apart from the command boxes around it by room and by its caption (which file this is), not
     by a colour of its own */
  .fv-body { display: grid; gap: 4px; min-width: 0; margin: 8px 0 16px; padding-top: 8px; border-top: 1px solid var(--kit-border, #ddd); }
  .fv-cap { font: 500 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--kit-text, inherit); overflow-wrap: anywhere; }
  .fv-state, .fv-intro { font-size: 12px; color: var(--kit-muted, #666); }
  .fv-text { max-width: 100%; max-height: 320px; margin: 0; padding: 8px 12px; overflow: auto; border: 1px solid var(--kit-border, #ddd); border-radius: 6px; background: var(--kit-surface-muted, #f4f4f4); color: var(--kit-text, inherit); font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre; tab-size: 2; }
`;
