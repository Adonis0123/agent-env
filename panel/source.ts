// Where the panel's data comes from: `bin/agent-env status --json --all` from this repository, or, for tests and
// previews only, the JSON file named by AGENT_ENV_STATUS_FILE. Never throws: a failure comes back as a LoadFailure
// with the stderr head and a next step, so the page can show it instead of going blank.
import { execFile } from "node:child_process";
import { statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join, resolve, sep } from "node:path";
import { type AllStatus, parseAllStatus, StatusShapeError } from "./model.ts";

export const REPO_ROOT = resolve(import.meta.dirname, "..");
export const DEFAULT_COMMAND = [resolve(REPO_ROOT, "bin", "agent-env"), "status", "--json", "--all"];
const STDERR_LINES = 8;

/** Where an AI works for this panel, found the way the engine finds its manifest (engine/agent_env.py manifest_arg,
 *  engine/overlay.py overlay_root): $AGENT_ENV_MANIFEST when set, as any path (a relative one from the engine's root,
 *  where the panel runs the CLI); else manifest/habits.toml in the engine's root or at most three folders up.
 *  No path is written in this file. */
export interface WorkFolders {
  /** the folder `bin/agent-env` commands run in: the personal folder when it has its own bin/agent-env, else the
   *  engine's root */
  run: string;
  /** the personal folder, the one that holds the manifest; "" when no manifest was found */
  overlay: string;
  /** the manifest file the engine reads; "" when none was found */
  manifest: string;
  /** a manifest file exists. False: the engine's root is not anyone's personal folder, and the page says so */
  found: boolean;
  /** `AGENT_ENV_MANIFEST=<path>` when a command run in `run` would not find the manifest by itself, else "" */
  env: string;
}
const MANIFEST = join("manifest", "habits.toml");
const OVERLAY_MAX_UP = 3;
const isFile = (path: string) => {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
};
const shellWord = (s: string) => (/^[\w@%+=:,./~-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`);

export function workFolders(o: { start?: string; env?: NodeJS.ProcessEnv; exists?: (path: string) => boolean } = {}): WorkFolders {
  const start = resolve(o.start ?? REPO_ROOT);
  const exists = o.exists ?? isFile;
  // overlay.py overlay_root: the engine's root, then its parents, MAX_UP levels at most
  let up = "";
  for (let d = start, i = 0; i <= OVERLAY_MAX_UP; i++, d = dirname(d)) {
    if (exists(join(d, MANIFEST))) {
      up = d;
      break;
    }
    if (dirname(d) === d) break;
  }
  const own = (dir: string) => dir !== start && exists(join(dir, "bin", "agent-env"));
  const named = o.env?.AGENT_ENV_MANIFEST;
  if (named) {
    const manifest = resolve(start, named);
    if (!exists(manifest)) return { run: start, overlay: "", manifest: "", found: false, env: "" };
    const standard = manifest.endsWith(join(sep, MANIFEST));
    const overlay = standard ? dirname(dirname(manifest)) : dirname(manifest);
    // the same file the engine finds by walking up: nothing to carry
    if (up && join(up, MANIFEST) === manifest) return { run: own(up) ? up : start, overlay: up, manifest, found: true, env: "" };
    if (standard && own(overlay)) return { run: overlay, overlay, manifest, found: true, env: "" };
    return { run: start, overlay, manifest, found: true, env: `AGENT_ENV_MANIFEST=${shellWord(manifest)}` };
  }
  if (!up) return { run: start, overlay: "", manifest: "", found: false, env: "" };
  return { run: own(up) ? up : start, overlay: up, manifest: join(up, MANIFEST), found: true, env: "" };
}
/** `agent-env status` runs its probes in parallel; the slowest one, `sideby doctor`, is capped at 120 s. Leave headroom above that. */
export const DEFAULT_TIMEOUT_MS = 180_000;

export type LoadFailureKind = "missing" | "exit" | "timeout" | "not-json" | "shape" | "file";

export interface LoadFailure {
  ok: false;
  kind: LoadFailureKind;
  message: string;
  /** First lines of stderr (or stdout when stderr is empty), for the error panel. */
  output: string[];
  /** What the user can run or do next. */
  fix: string;
  command: string;
}

export interface LoadOk {
  ok: true;
  status: AllStatus;
  from: string;
}

export type LoadResult = LoadOk | LoadFailure;

export interface LoadOptions {
  command?: string[];
  statusFile?: string;
  env?: NodeJS.ProcessEnv;
  timeoutMs?: number;
  cwd?: string;
}

const head = (s: string) =>
  s
    .split(/\r?\n/)
    .map((l) => l.trimEnd())
    .filter(Boolean)
    .slice(0, STDERR_LINES);

const shown = (argv: string[]) => argv.map((a) => (a.startsWith(REPO_ROOT + "/") ? a.slice(REPO_ROOT.length + 1) : a)).join(" ");

function parse(text: string, from: string, command: string, stderr: string): LoadResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    return {
      ok: false,
      kind: "not-json",
      message: `output is not valid JSON (${(e as Error).message})`,
      output: head(stderr).length ? head(stderr) : head(text),
      fix: `${command} | head`,
      command,
    };
  }
  try {
    return { ok: true, status: parseAllStatus(raw), from };
  } catch (e) {
    if (!(e instanceof StatusShapeError)) throw e;
    return { ok: false, kind: "shape", message: e.message, output: head(stderr), fix: "docs/status-schema.md", command };
  }
}

export async function loadStatus(o: LoadOptions = {}): Promise<LoadResult> {
  const file = o.statusFile ?? o.env?.AGENT_ENV_STATUS_FILE;
  if (file) {
    let text: string;
    try {
      text = await readFile(file, "utf8");
    } catch (e) {
      return { ok: false, kind: "file", message: (e as Error).message, output: [], fix: "unset AGENT_ENV_STATUS_FILE", command: `AGENT_ENV_STATUS_FILE=${file}` };
    }
    return parse(text, file, `AGENT_ENV_STATUS_FILE=${file}`, "");
  }
  const argv = o.command ?? DEFAULT_COMMAND;
  const command = shown(argv);
  const timeoutMs = o.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  return new Promise((done) => {
    execFile(
      argv[0],
      argv.slice(1),
      { cwd: o.cwd ?? REPO_ROOT, env: o.env ?? process.env, timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024, encoding: "utf8" },
      (error, stdout, stderr) => {
        if (error) {
          const err = error as NodeJS.ErrnoException & { killed?: boolean; signal?: string | null; code?: string | number | null };
          if (err.code === "ENOENT" || err.code === "EACCES") {
            done({ ok: false, kind: "missing", message: `${command.split(" ")[0]} ${err.code === "ENOENT" ? "does not exist" : "is not executable"}`, output: head(stderr), fix: command, command });
            return;
          }
          if (err.killed || err.signal === "SIGTERM") {
            done({ ok: false, kind: "timeout", message: `no answer within ${Math.round(timeoutMs / 1000)} s`, output: head(stderr), fix: command, command });
            return;
          }
          // A non-zero exit that still printed the status is shown (status may exit 1 when something drifted).
          if (stdout.trim().startsWith("{")) {
            const r = parse(stdout, command, command, stderr);
            if (r.ok) {
              done(r);
              return;
            }
          }
          done({ ok: false, kind: "exit", message: `exited with code ${err.code ?? "?"}`, output: head(stderr).length ? head(stderr) : head(stdout), fix: command, command });
          return;
        }
        done(parse(stdout, command, command, stderr));
      },
    );
  });
}
