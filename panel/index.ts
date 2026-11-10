// Environments panel for the Local Tools portal (kind "panel"), read-only. Exports createPanelHandler({ basePath,
// allowedHosts, theme, readOnly, lang, ask }) per the portal contract (portal-architecture.md §4); /api/health answers
// { app: "agent-env", version }. Its only data is `bin/agent-env status --json --all` (docs/status-schema.md); for
// tests and previews AGENT_ENV_STATUS_FILE names a status JSON file to show instead.
// Routes: "/" (page shell that loads the content), "/fragment" (the content), "/api/status" (the JSON as read).
import { randomBytes } from "node:crypto";
import { homedir } from "node:os";
import type { IncomingMessage, ServerResponse } from "node:http";
import { type Lang, translator } from "./i18n.ts";
import { contentHtml, type Ctx, fragmentPage, layout } from "./page.ts";
import { loadStatus, type LoadResult } from "./source.ts";

interface PanelTheme {
  colorScheme?: "auto" | "light" | "dark";
  header?: "plain" | "bar";
  light?: Record<string, string>;
  dark?: Record<string, string>;
}

export interface PanelOptions {
  basePath?: string;
  allowedHosts: string[];
  theme?: PanelTheme;
  readOnly?: boolean;
  lang?: "auto" | "en" | "zh";
  version?: string;
  /** Given by the portal when this page's ai-tasks.json entry exists: AI Tasks go to the outer page by postMessage. */
  ask?: { bridge?: string };
  /** Given by the portal: the login shell's environment (PATH), used to run the CLI. */
  runtime?: () => { env?: NodeJS.ProcessEnv };
  /** Command to run instead of `<repo>/bin/agent-env status --json --all` (tests). */
  command?: string[];
  /** How long to wait for the CLI before showing a timeout, in ms (default 120 000). */
  timeoutMs?: number;
  /** A status JSON file to show instead of running the CLI; default $AGENT_ENV_STATUS_FILE. */
  statusFile?: string;
  /** Home folder to hide from pages (default $HOME). */
  home?: string;
  /** Clock for the stale check (tests). */
  now?: () => number;
}

export const VERSION = "0.1.0";

/** Portal hook: the login shell's environment, so the CLI finds python3 and git like in a terminal. */
export function createRuntime(o: { env?: NodeJS.ProcessEnv } = {}) {
  return { env: o.env };
}

export function createPanelHandler(options: PanelOptions) {
  if (!options || !Array.isArray(options.allowedHosts)) throw new TypeError("createPanelHandler: allowedHosts must be an array of host[:port] strings");
  const base = (options.basePath ?? "").replace(/\/+$/, "");
  const lang = options.lang ?? "auto";
  if (!["auto", "en", "zh"].includes(lang)) throw new TypeError('createPanelHandler: lang must be "auto", "en" or "zh"');
  const scheme = options.theme?.colorScheme ?? "auto";
  const headerBar = options.theme?.header === "bar";
  const askBridge = options.ask?.bridge === "postMessage";
  const allowed = new Set(options.allowedHosts);
  const home = options.home ?? process.env.HOME ?? homedir();
  const now = options.now ?? Date.now;
  const statusFile = options.statusFile ?? process.env.AGENT_ENV_STATUS_FILE;
  const hide = (s: string) => (home && home !== "/" ? s.split(home + "/").join("~/") : s);

  const send = (res: ServerResponse, status: number, type: string, body: string, extra: Record<string, string> = {}, head = false) => {
    res.statusCode = status;
    res.setHeader("Content-Type", type);
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("X-Content-Type-Options", "nosniff");
    for (const [k, v] of Object.entries(extra)) res.setHeader(k, v);
    res.setHeader("Content-Length", String(Buffer.byteLength(body)));
    res.end(head ? undefined : body);
  };
  const json = (res: ServerResponse, status: number, body: unknown, head = false) =>
    send(res, status, "application/json; charset=utf-8", hide(JSON.stringify(body, null, 2)), { "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'" }, head);

  function pickLang(req: IncomingMessage): Lang {
    if (lang !== "auto") return lang;
    return /^zh|,\s*zh/i.test(String(req.headers["accept-language"] ?? "zh")) ? "zh" : "en";
  }

  function load(): Promise<LoadResult> {
    let env: NodeJS.ProcessEnv = process.env;
    try {
      const rt = options.runtime?.();
      if (rt?.env) env = { ...process.env, ...rt.env };
    } catch {
      // keep the process environment
    }
    return loadStatus({ command: options.command, statusFile, env, timeoutMs: options.timeoutMs });
  }

  return async function handle(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
    const raw = req.url || "/";
    if (base && !(raw === base || raw.startsWith(base + "/") || raw.startsWith(base + "?"))) return false;
    // Private data: answer only on the hosts the portal gave (blocks DNS rebinding).
    if (!allowed.has(String(req.headers.host ?? ""))) {
      send(res, 403, "text/plain; charset=utf-8", "host not allowed");
      return true;
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      send(res, 405, "text/plain; charset=utf-8", "read-only panel: use GET", { Allow: "GET, HEAD" });
      return true;
    }
    const head = req.method === "HEAD";
    let url: URL;
    try {
      url = new URL(raw.slice(base.length) || "/", "http://localhost");
    } catch {
      send(res, 400, "text/plain; charset=utf-8", "bad url", {}, head);
      return true;
    }
    const l = pickLang(req);
    const ctx: Ctx = { t: translator(l), lang: l, base, askBridge, now: now(), headerBar };
    const qs = url.searchParams.get("scheme");
    const schemeNow = qs && ["auto", "light", "dark"].includes(qs) ? qs : scheme;
    const htmlHeaders = (nonce?: string) => ({
      "Content-Security-Policy": `default-src 'self'; script-src ${nonce ? `'nonce-${nonce}'` : "'none'"}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'`,
      "Referrer-Policy": "same-origin",
    });

    if (url.pathname === "/api/health") {
      json(res, 200, { app: "agent-env", version: options.version ?? VERSION }, head);
      return true;
    }
    if (url.pathname === "/api/status") {
      const r = await load();
      json(res, r.ok ? 200 : 502, r, head);
      return true;
    }
    if (url.pathname === "/fragment") {
      const r = await load();
      const body = hide(contentHtml(ctx, r, r.ok && statusFile ? r.from : undefined));
      const whole = req.headers["x-env-fragment"] === "1" ? body : fragmentPage(ctx, body, schemeNow);
      send(res, 200, "text/html; charset=utf-8", whole, htmlHeaders(), head);
      return true;
    }
    if (url.pathname === "/" || url.pathname === "/index.html") {
      const nonce = randomBytes(12).toString("base64");
      send(res, 200, "text/html; charset=utf-8", layout({ ctx, nonce, scheme: schemeNow, headerBar }), htmlHeaders(nonce), head);
      return true;
    }
    send(res, 404, "text/plain; charset=utf-8", "not found", {}, head);
    return true;
  };
}
