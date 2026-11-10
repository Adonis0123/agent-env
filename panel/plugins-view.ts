// The 插件 tab: which plugin repositories are public or private, how a habit gets from its source repositories to
// each environment, and what each environment gets. Its only data is the status's optional `plugins_view`
// (engine/plugins_view.py, docs/status-schema.md「插件视图」); the engine writes the Chinese texts, the tab adds no
// judgement of its own. No import from page.ts but types: page.ts imports this module, and hands in the few shared
// pieces (state marks, the AI button) as a kit.
import { readsOnly } from "./ai-copy.ts";
import type { T } from "./i18n.ts";
import { type FactWhere, factWhere } from "./model.ts";

export type Visibility = "public" | "personal" | "company" | "local";
export const VIS_ORDER: Visibility[] = ["public", "personal", "company", "local"];
export type RepoVisibility = "public" | "private" | "unknown";

export interface PvNote { code: string; state: string; text_zh: string; fix: string }
export interface PvRepo { key: string; name: string; url: string; host: string; visibility: RepoVisibility; empty: boolean | null; checked_at: string | null; detail: string; fix: string }
export interface PvSource { name: string; repo: string | null; path: string; provides: string[] }
export interface PvGenerator extends PvSource { output: string }
export interface PvPluginRepo { visibility: Visibility; visibility_zh: string; repo: string | null; plugins: string[]; notes: PvNote[] }
export interface PvPlugin {
  name: string;
  visibility: Visibility;
  visibility_zh: string;
  description: string;
  repo_key: string | null;
  build: { state: string; detail: string; fix: string };
  publish: { state: string; detail: string; fix: string; pushed_at: string | null };
}
export interface PvGets { state: "gets" | "none" | "denied" | "legacy" | "own"; plugins: string[] }
export interface PvEnv {
  env: string | null;
  name: string;
  transport: string | null;
  mode: "plugins" | "legacy" | "others";
  phase: number | null;
  now_zh: string | null;
  deny: string[];
  targets: Array<{ id: string; name: string; plugins: string[] }>;
  gets: Record<Visibility, PvGets>;
  credential: { state: string; name: string | null; expires: string | null; detail: string };
  notes: PvNote[];
}
export interface PluginsView {
  refresh_command: string;
  checked_at: string | null;
  repos: PvRepo[];
  sources: PvSource[];
  generator: PvGenerator | null;
  plugin_repos: PvPluginRepo[];
  plugins: PvPlugin[];
  environments: PvEnv[];
}
export type PluginsData = { kind: "absent" } | { kind: "error"; message: string; fix: string; refresh: string } | { kind: "ok"; view: PluginsView };

export const PLUGINS_PRESET = "adjust-plugins";
const REFRESH = "bin/agent-env plugin repos --refresh";

const obj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const strOrNull = (v: unknown) => (typeof v === "string" && v ? v : null);
const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
const list = (v: unknown) => (Array.isArray(v) ? v.filter(obj) : []);
const isVis = (v: unknown): v is Visibility => typeof v === "string" && (VIS_ORDER as string[]).includes(v);
const note = (n: Record<string, unknown>): PvNote => ({ code: str(n.code), state: str(n.state) || "unknown", text_zh: str(n.text_zh), fix: str(n.fix) });
const source = (s: Record<string, unknown>): PvSource => ({ name: str(s.name), repo: strOrNull(s.repo), path: str(s.path), provides: strs(s.provides) });

/** The field as the tab reads it: absent (an older engine), the engine's own error, or the view with every part
 *  checked, so one odd entry never breaks the page. */
export function readPluginsView(raw: unknown): PluginsData {
  if (!obj(raw)) return { kind: "absent" };
  const refresh = str(raw.refresh_command) || REFRESH;
  if (obj(raw.error)) return { kind: "error", message: str(raw.error.message), fix: str(raw.error.fix), refresh };
  const gets = (g: unknown): Record<Visibility, PvGets> => {
    const o = obj(g) ? g : {};
    const out = {} as Record<Visibility, PvGets>;
    for (const v of VIS_ORDER) {
      const c = obj(o[v]) ? o[v] : {};
      const state = ["gets", "none", "denied", "legacy", "own"].includes(str(c.state)) ? (c.state as PvGets["state"]) : "none";
      out[v] = { state, plugins: strs(c.plugins) };
    }
    return out;
  };
  const view: PluginsView = {
    refresh_command: refresh,
    checked_at: strOrNull(raw.checked_at),
    repos: list(raw.repos).map((r) => ({
      key: str(r.key), name: str(r.name) || str(r.key), url: str(r.url), host: str(r.host),
      visibility: r.visibility === "public" || r.visibility === "private" ? r.visibility : "unknown",
      empty: typeof r.empty === "boolean" ? r.empty : null, checked_at: strOrNull(r.checked_at), detail: str(r.detail), fix: str(r.fix),
    })),
    sources: list(raw.sources).map(source),
    generator: obj(raw.generator) ? { ...source(raw.generator), output: str(raw.generator.output) } : null,
    plugin_repos: list(raw.plugin_repos).filter((c) => isVis(c.visibility)).map((c) => ({
      visibility: c.visibility as Visibility, visibility_zh: str(c.visibility_zh), repo: strOrNull(c.repo), plugins: strs(c.plugins), notes: list(c.notes).map(note),
    })),
    plugins: list(raw.plugins).map((p) => {
      const b = obj(p.build) ? p.build : {};
      const u = obj(p.publish) ? p.publish : {};
      return {
        name: str(p.name), visibility: isVis(p.visibility) ? p.visibility : "local", visibility_zh: str(p.visibility_zh), description: str(p.description),
        repo_key: strOrNull(p.repo_key),
        build: { state: str(b.state) || "unknown", detail: str(b.detail), fix: str(b.fix) },
        publish: { state: str(u.state) || "unknown", detail: str(u.detail), fix: str(u.fix), pushed_at: strOrNull(u.pushed_at) },
      };
    }),
    environments: list(raw.environments).map((e) => {
      const c = obj(e.credential) ? e.credential : {};
      return {
        env: strOrNull(e.env), name: str(e.name) || str(e.env), transport: strOrNull(e.transport),
        mode: e.mode === "plugins" || e.mode === "legacy" || e.mode === "others" ? e.mode : "legacy",
        phase: typeof e.phase === "number" ? e.phase : null, now_zh: strOrNull(e.now_zh), deny: strs(e.deny),
        targets: list(e.targets).map((t) => ({ id: str(t.id), name: str(t.name) || str(t.id), plugins: strs(t.plugins) })),
        gets: gets(e.gets),
        credential: { state: str(c.state) || "none", name: strOrNull(c.name), expires: strOrNull(c.expires), detail: str(c.detail) },
        notes: list(e.notes).map(note),
      };
    }),
  };
  return { kind: "ok", view };
}

/** The repository behind a key, or an unknown one that says how to find out. */
export function repoOf(v: PluginsView, key: string | null): PvRepo | null {
  if (!key) return null;
  return v.repos.find((r) => r.key === key) ?? { key, name: key.split("/").pop() ?? key, url: `https://${key}`, host: key.split("/")[0], visibility: "unknown", empty: null, checked_at: null, detail: "", fix: v.refresh_command };
}

/** Notes that frame their node: what is broken or missing. Drift (not pushed yet, an empty repository) shows only as its mark. */
export const isAlertNote = (n: PvNote) => n.state === "fail" || n.state === "missing";

/** The fact the 「让 AI 调整」 task gets: what each environment may and does pull, and which plugins exist. At most
 *  `limit` characters: target lists go first when it is too long, the per-environment rules stay. */
export function pluginsFact(v: PluginsView, limit: number, where: FactWhere = {}): string {
  const envs = v.environments.filter((e) => e.env);
  const full = {
    ...factWhere(where),
    plugins: v.plugins.map((p) => ({ name: p.name, visibility: p.visibility })),
    environments: envs.map((e) => ({ env: e.env, deny_visibility: e.deny, mode: e.mode, targets: e.targets.map((t) => ({ id: t.id, plugins: t.plugins })) })),
  };
  let text = JSON.stringify(full);
  if (text.length <= limit) return text;
  const lean = { ...full, environments: envs.map((e) => ({ env: e.env, deny_visibility: e.deny, mode: e.mode, plugins: [...new Set(e.targets.flatMap((t) => t.plugins))] })) };
  text = JSON.stringify(lean);
  return text.length <= limit ? text : text.slice(0, limit);
}

// ---- rendering ----

export interface PluginsKit {
  t: T;
  lang: "en" | "zh";
  esc: (s: unknown) => string;
  /** a state mark: icon and words, coloured by state */
  mark: (state: string, label: string, tip?: string) => string;
  /** the time a check ran, in the page's words ("today 14:43") */
  when: (iso: string) => string;
  /** the 「让 AI 调整」 button, or its copy-notes stand-in outside the portal */
  aiButton: string;
  /** the 「复制给 AI」 button: copies a sentence that says `what` (after 帮我 / Please) with the commands inside it */
  aiCopy: (what: string, commands?: string[], o?: { careful?: boolean; quiet?: boolean }) => string;
  /** the same button for a fix written as a sentence: copies the problem with the whole fix quoted (ai-copy.ts aiFixText) */
  aiFix: (problem: string, fix: string) => string;
}

const VIS_LABEL: Record<Visibility, string> = { public: "Public", personal: "Personal", company: "Company", local: "This machine" };
const VIS_TIP: Record<Visibility, string> = {
  public: "Anyone can read it, so it holds only what is fine to share",
  personal: "Your private repository: only your own machines and the cloud agents you trust read it",
  company: "A private repository for company work: company machines and colleagues read it",
  local: "Extras for this machine only: they stay here and are never uploaded",
};
const REPO_VIS: Record<RepoVisibility, string> = { public: "public", private: "private", unknown: "not checked" };
const UNKNOWN_MEANS = "Nobody could tell whether this repository is public or private, so the page cannot confirm that private content stays private.";
const PROVIDES: Record<string, string> = { rules: "rules", docs: "docs", hooks: "hooks", skills: "skills", company_skills: "company skills" };
const TRANSPORT: Record<string, string> = { local: "reads on this machine", "remote-exec": "sent over from this machine", "cloud-hosted": "cloud-hosted" };
const BUILD: Record<string, [string, string]> = { synced: ["synced", "built"], drift: ["drift", "built from old sources"], missing: ["missing", "not built"] };
const PUBLISH: Record<string, [string, string]> = { pushed: ["synced", "pushed"], stale: ["drift", "repository has an old version"], not_pushed: ["drift", "not pushed"], local: ["none", "not pushed (stays here)"] };

const arrow = `<svg class="ic pv-arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9.5M9 4.5 12.5 8 9 11.5"/></svg>`;
const lock = `<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7"/></svg>`;

const STATUS_COMMAND = "bin/agent-env status --json --all";
const isCommand = (fix: string) => /^bin\/agent-env\s/.test(fix) && !/[，。；]/.test(fix);

/** A command the AI runs: the 「复制给 AI」 button first, the command itself as quiet code after it. `what` says what
 *  the command is for, in the page's language. */
function copyLine(k: PluginsKit, what: string, cmd: string, quiet = true, careful = !readsOnly(cmd)): string {
  return `<span class="pv-cmd">${k.aiCopy(what, [cmd], { careful, quiet })}<code>${k.esc(cmd)}</code></span>`;
}

/** How to fix `problem` (what is wrong, in the page's language): a command, or a one-line remedy in words. Either way
 *  the button copies a sentence that names the problem and carries the fix. */
function fixText(k: PluginsKit, fix: string, problem: string): string {
  if (!fix) return "";
  if (isCommand(fix)) return copyLine(k, k.t("handle this problem: {what}", { what: problem }), fix);
  return `<span class="pv-fix">${k.esc(fix)}</span><span class="pv-cmd">${k.aiFix(problem, fix)}</span>`;
}

function badge(k: PluginsKit, r: PvRepo | null): string {
  if (!r) return "";
  const word = k.t(REPO_VIS[r.visibility]);
  const tip = r.visibility === "unknown"
    ? `${k.t(UNKNOWN_MEANS)}\n${r.detail || k.t("Not checked yet")}`
    : `${k.t("Checked {when}", { when: r.checked_at ? k.when(r.checked_at) : k.t("time unknown") })}${r.empty ? `\n${k.t("The repository is empty")}` : ""}`;
  return `<span class="pv-vis v-${r.visibility}" data-tip="${k.esc(tip)}">${r.visibility === "private" ? lock : ""}${k.esc(word)}${r.empty ? `<span class="pv-empty">${k.esc(k.t("empty"))}</span>` : ""}</span>`;
}

/** The repository line of a node: its name as a link, its visibility; an unknown one says why and how to fix it. */
function repoLine(k: PluginsKit, r: PvRepo | null, fallback: string): string {
  const name = r ? `<a class="pv-name" href="${k.esc(r.url)}" target="_blank" rel="noopener noreferrer">${k.esc(r.name)}</a>` : `<span class="pv-name">${k.esc(fallback)}</span>`;
  const detail = r?.detail || k.t("Not checked yet");
  const why = r && r.visibility === "unknown"
    ? `<p class="pv-why">${k.esc(k.t(UNKNOWN_MEANS))}</p><p class="pv-why">${k.esc(detail)}</p>${fixText(k, r.fix, k.t("nobody could tell whether the repository {name} is public or private ({detail})", { name: r.name, detail }))}`
    : "";
  return `<div class="pv-node-h">${name}${badge(k, r)}</div>${why}`;
}

/** `about`: the repository or environment the notes sit on, so a copied sentence says whose problem it is. */
function notesHtml(k: PluginsKit, notes: PvNote[], about: string): string {
  if (!notes.length) return "";
  return `<ul class="pv-notes">${notes.map((n) => `<li>${k.mark(n.state, n.text_zh)}${n.fix ? fixText(k, n.fix, `${about}: ${n.text_zh}`) : ""}</li>`).join("")}</ul>`;
}

function sourceNode(k: PluginsKit, v: PluginsView, s: PvSource): string {
  const what = s.provides.map((p) => k.t(PROVIDES[p] ?? p)).join(k.lang === "zh" ? "、" : ", ");
  return `<li class="pv-node">${repoLine(k, repoOf(v, s.repo), s.name)}<p class="pv-sub">${k.esc(s.path)}</p>${what ? `<p class="pv-what">${k.esc(what)}</p>` : ""}</li>`;
}

function generatorNode(k: PluginsKit, v: PluginsView): string {
  const g = v.generator;
  if (!g) return "";
  const own = g.provides.length ? `<p class="pv-what">${k.esc(k.t("Also keeps: {what}", { what: g.provides.map((p) => k.t(PROVIDES[p] ?? p)).join(k.lang === "zh" ? "、" : ", ") }))}</p>` : "";
  const plugins = v.plugins.map((p) => {
    const [bs, bw] = BUILD[p.build.state] ?? ["unknown", "build state unknown"];
    const [ps, pw] = PUBLISH[p.publish.state] ?? ["unknown", "publish state unknown"];
    return `<li class="pv-plugin"><span class="pv-pl-h"><span class="pv-pl">${k.esc(p.name)}</span><span class="pv-tag" data-tip="${k.esc(k.t(VIS_TIP[p.visibility]))}">${k.esc(k.t(VIS_LABEL[p.visibility]))}</span></span><span class="pv-pl-s">${k.mark(bs, k.t(bw), p.build.detail)}${k.mark(ps, k.t(pw), p.publish.detail)}</span>${p.build.fix ? fixText(k, p.build.fix, `${p.name}: ${k.t(bw)}`) : ""}</li>`;
  }).join("");
  return `<li class="pv-node">${repoLine(k, repoOf(v, g.repo), g.name)}<p class="pv-sub">${k.esc(k.t("Builds into {path}", { path: g.output }))}</p>${own}${plugins ? `<ul class="pv-plugins">${plugins}</ul>` : `<p class="pv-sub">${k.esc(k.t("No plugins defined"))}</p>`}</li>`;
}

function repoNode(k: PluginsKit, v: PluginsView, c: PvPluginRepo): string {
  const r = repoOf(v, c.repo);
  const holds = c.plugins.length ? c.plugins.map((p) => `<span class="pv-pl">${k.esc(p)}</span>`).join("") : "";
  const head = `<p class="pv-for" data-tip="${k.esc(k.t(VIS_TIP[c.visibility]))}">${k.esc(k.t("For {vis} plugins", { vis: k.t(VIS_LABEL[c.visibility]) }))}</p>`;
  const alert = c.notes.some(isAlertNote) ? " is-alert" : "";
  return `<li class="pv-node${alert}">${head}${repoLine(k, r, k.t("No repository yet"))}${holds ? `<p class="pv-holds">${holds}</p>` : ""}${notesHtml(k, c.notes, r?.name ?? k.t("For {vis} plugins", { vis: k.t(VIS_LABEL[c.visibility]) }))}</li>`;
}

function envNode(k: PluginsKit, e: PvEnv): string {
  let what: string;
  if (e.mode === "legacy") what = k.t("Still set up the old way, not on plugins yet (phase {n})", { n: e.phase ?? "?" });
  else if (e.mode === "others") what = k.t("Only what is public");
  else {
    const names = VIS_ORDER.flatMap((x) => e.gets[x].plugins);
    what = names.length ? names.join(k.lang === "zh" ? "、" : ", ") : k.t("No plugins");
  }
  // a remote host on plugins pulls them itself (stage 3); one of the user's machines composes them from the sources
  // (stage 4); before that the rules were delivered to the one and read straight from the source by the other
  const how = e.mode === "plugins" && e.transport === "remote-exec" ? "pulls on its own timer"
    : e.mode === "plugins" && e.transport === "local" ? "composed on this machine"
    : e.transport ? TRANSPORT[e.transport] ?? e.transport : "";
  const sub = how ? k.t(how) : "";
  return `<li class="pv-node pv-env${e.mode === "others" ? " is-others" : ""}"><div class="pv-node-h"><span class="pv-name">${k.esc(e.name)}</span></div>${sub ? `<p class="pv-sub">${k.esc(sub)}</p>` : ""}<p class="pv-what">${k.esc(what)}</p></li>`;
}

function flowHtml(k: PluginsKit, v: PluginsView): string {
  const col = (title: string, tip: string, items: string, first = false) =>
    `<div class="pv-col"><h3 class="pv-ch"${tip ? ` data-tip="${k.esc(k.t(tip))}"` : ""}>${first ? "" : arrow}${k.esc(k.t(title))}</h3><ul class="pv-nodes">${items || `<li class="pv-none">${k.esc(k.t("Nothing here yet"))}</li>`}</ul></div>`;
  return `<div class="pv-flow">${[
    col("Sources", "The repositories the rules, docs, hooks and skills are written in", v.sources.map((s) => sourceNode(k, v, s)).join(""), true),
    col("Built by", "This repository turns the sources into one plugin per visibility", generatorNode(k, v)),
    col("Plugin repositories", "Where each plugin is pushed; its visibility decides who can read it", v.plugin_repos.map((c) => repoNode(k, v, c)).join("")),
    col("Environments", "Who pulls which plugins", v.environments.map((e) => envNode(k, e)).join("")),
  ].join("")}</div>`;
}

function getsCell(k: PluginsKit, vis: Visibility, g: PvGets): string {
  const label = k.t(VIS_LABEL[vis]);
  if (g.state === "gets") return `<td class="c" data-label="${k.esc(label)}">${g.plugins.map((p) => `<span class="pv-pl">${k.esc(p)}</span>`).join("")}</td>`;
  if (g.state === "denied") {
    const bad = g.plugins.length ? k.mark("fail", k.t("{names} attached", { names: g.plugins.join("、") })) : "";
    return `<td class="c is-denied" data-label="${k.esc(label)}"><span class="pv-deny" data-tip="${k.esc(k.t("By its settings, this environment must not get {vis} plugins", { vis: label }))}">${lock}${k.esc(k.t("Not allowed"))}</span>${bad}</td>`;
  }
  if (g.state === "own") return `<td class="c is-quiet" data-label="${k.esc(label)}">${k.esc(k.t("its own"))}</td>`;
  return `<td class="c is-quiet" data-label="${k.esc(label)}"><span aria-hidden="true">—</span><span class="visually-hidden">${k.esc(k.t("none"))}</span></td>`;
}

function tableHtml(k: PluginsKit, v: PluginsView): string {
  const heads = VIS_ORDER.map((x) => `<th scope="col"><span data-tip="${k.esc(k.t(VIS_TIP[x]))}">${k.esc(k.t(VIS_LABEL[x]))}</span></th>`).join("");
  const rows = v.environments.map((e) => {
    const who = e.targets.length ? `<p class="pv-sub">${k.esc(e.targets.map((t) => t.name).join(k.lang === "zh" ? "、" : ", "))}</p>` : "";
    const cred = e.credential.state === "token" || e.credential.state === "expiring"
      ? `<p class="pv-sub">${k.esc(k.t("access key {name}, expires {date}", { name: e.credential.name ?? "", date: e.credential.expires ?? k.t("time unknown") }))}</p>` : "";
    const name = `<th scope="row"><span class="pv-name">${k.esc(e.name)}</span>${who}${cred}${notesHtml(k, e.notes.filter((n) => n.code !== "legacy"), e.name)}</th>`;
    const cells = e.mode === "legacy"
      ? `<td class="c is-legacy" colspan="4"><span class="pv-legacy">${k.esc(k.t("Still set up the old way, not on plugins yet (phase {n})", { n: e.phase ?? "?" }))}</span>${e.now_zh ? `<span class="pv-sub">${k.esc(k.t("For now: {how}", { how: e.now_zh }))}</span>` : ""}</td>`
      : VIS_ORDER.map((x) => getsCell(k, x, e.gets[x])).join("");
    const alert = e.notes.some(isAlertNote) || VIS_ORDER.some((x) => e.gets[x].state === "denied" && e.gets[x].plugins.length) ? " is-alert" : "";
    return `<tr class="${e.mode === "others" ? "is-others" : ""}${alert}">${name}${cells}</tr>`;
  }).join("");
  return `<table class="pv-table"><thead><tr><th scope="col">${k.esc(k.t("Environment"))}</th>${heads}</tr></thead><tbody>${rows}</tbody></table>`;
}

/** The whole tab. `data` from readPluginsView. */
export function pluginsHtml(k: PluginsKit, data: PluginsData): string {
  if (data.kind === "absent") {
    return `<div class="pv"><div class="empty" role="status" data-pv="absent"><p><strong>${k.esc(k.t("No plugin information yet"))}</strong></p><p>${k.esc(k.t("The tool that made this status is an older version that does not know about plugins, so this tab is empty. The other tabs are not affected. Copy the sentence below to your AI; it updates the tool and checks again."))}</p><div class="fix-line">${copyLine(k, k.t("update agent-env to the newest version (first tell me how you are going to update it, and wait for my yes), then check every machine again so the Plugins tab has something to show"), STATUS_COMMAND, false, true)}</div></div></div>`;
  }
  if (data.kind === "error") {
    const problem = k.t("the Plugins tab of the Environments panel could not be worked out ({detail})", { detail: data.message });
    return `<div class="pv"><div class="empty error" role="alert" data-pv="error"><p><strong>${k.esc(k.t("The plugin information could not be read"))}</strong></p><p>${k.esc(k.t("Something went wrong while working out which plugins go where, so this tab is empty. The other tabs are not affected. Copy the sentence below to your AI."))}</p><p class="pv-why">${k.esc(data.message)}</p><div class="fix-line">${fixText(k, data.fix, problem) || k.aiCopy(k.t("handle this problem: {what}", { what: problem }), [], { careful: true })}</div></div></div>`;
  }
  const v = data.view;
  const checked = v.checked_at
    ? k.t("Public or private: checked {when}", { when: k.when(v.checked_at) })
    : k.t("Public or private: never checked");
  const unknown = v.repos.filter((r) => r.visibility === "unknown").length;
  const bar = `<div class="viewbar pv-bar"><p class="pv-checked">${k.esc(checked)}${unknown ? `<span class="pv-unknown-n">${k.esc(k.t("{n} could not be checked", { n: unknown }))}</span>` : ""}<span class="pv-refresh">${k.esc(k.t("Check again:"))}</span>${copyLine(k, k.t("check again whether each repository is public or private"), v.refresh_command)}</p><span class="pv-acts">${k.aiButton}</span></div>`;
  return `<div class="pv" data-pv="ok">${bar}<section class="pv-sec" aria-labelledby="pv-flow-h"><h2 class="pv-h" id="pv-flow-h">${k.esc(k.t("From source to environment"))}</h2>${flowHtml(k, v)}</section><section class="pv-sec" aria-labelledby="pv-gets-h"><h2 class="pv-h" id="pv-gets-h">${k.esc(k.t("What each environment gets"))}</h2>${tableHtml(k, v)}</section></div>`;
}

/** Styles of the tab; page.ts adds them to its one CSS string. Same tokens and rhythm as the other views; colour
 *  only for states (DESIGN.md). Under 761px the flow stacks and the table turns into one block per environment. */
export const PLUGINS_CSS = `
  /* "plugins" tab: a flow from the sources to the environments, then a table of what each environment gets */
  .pv-bar { justify-content: space-between; }
  .pv-checked { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 13px; color: var(--kit-muted, #666); }
  .pv-unknown-n { color: var(--kit-warn, #a44d00); }
  .pv-refresh { margin-left: 8px; }
  .pv-acts { display: flex; gap: 8px; margin-left: auto; }
  .pv-cmd { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 0; max-width: 100%; }
  .pv-cmd code { padding: 0 4px; border-radius: 4px; background: var(--kit-surface-muted, #f4f4f4); overflow-wrap: anywhere; }
  .pv-sec + .pv-sec { margin-top: 40px; }
  .pv-h { margin-bottom: 12px; font-size: 15px; font-weight: 600; line-height: 20px; }
  .pv-flow { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 24px; align-items: start; }
  .pv-col { display: grid; gap: 8px; min-width: 0; }
  .pv-ch { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 500; line-height: 16px; color: var(--kit-muted, #666); }
  .pv-arrow { width: 14px; height: 14px; color: var(--kit-faint, #999); }
  .pv-nodes { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
  .pv-node { display: grid; gap: 4px; min-width: 0; padding: 12px; border: 1px solid var(--kit-border, #ddd); border-radius: 8px; background: var(--kit-surface, #fff); font-size: 13px; }
  .pv-node.is-alert { border-color: var(--kit-fail-bar, #eb5757); }
  .pv-env.is-others { border-style: dashed; background: transparent; }
  .pv-none { font-size: 12px; color: var(--kit-faint, #999); }
  .pv-node-h { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; min-width: 0; }
  .pv-name { min-width: 0; font-weight: 600; color: var(--kit-text, inherit); overflow-wrap: anywhere; }
  a.pv-name { text-decoration: none; }
  a.pv-name:hover { text-decoration: underline; text-underline-offset: 3px; }
  .pv-for { font-size: 12px; color: var(--kit-muted, #666); }
  .pv-sub { font-size: 12px; color: var(--kit-muted, #666); overflow-wrap: anywhere; }
  .pv-what { font-size: 12px; color: var(--kit-text, inherit); overflow-wrap: anywhere; }
  .pv-why, .pv-fix { font-size: 12px; color: var(--kit-muted, #666); overflow-wrap: anywhere; }
  .pv-node .pv-cmd, .pv-notes .pv-cmd { font-size: 12px; }
  /* a repository's visibility: quiet words in a hairline frame; unknown is dashed and faint, the fix sits under it */
  .pv-vis { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 6px; border: 1px solid var(--kit-border-strong, #ccc); border-radius: 4px; font-size: 12px; font-weight: 500; line-height: 1; color: var(--kit-text, inherit); white-space: nowrap; }
  .pv-vis .ic { width: 12px; height: 12px; }
  .pv-vis.v-unknown { border-style: dashed; color: var(--kit-muted, #666); }
  .pv-empty { margin-left: 2px; font-weight: 400; color: var(--kit-muted, #666); }
  .pv-empty::before { content: "·"; margin-right: 4px; }
  .pv-plugins { display: grid; gap: 8px; margin: 4px 0 0; padding: 8px 0 0; border-top: 1px solid var(--kit-border, #ddd); list-style: none; }
  .pv-plugin { display: grid; gap: 2px; min-width: 0; }
  .pv-pl-h { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
  .pv-pl-s { display: flex; flex-wrap: wrap; gap: 0 12px; font-size: 12px; }
  .pv-pl-s .ic { width: 14px; height: 14px; }
  .pv-pl { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }
  .pv-holds, td.c .pv-pl { display: flex; flex-wrap: wrap; gap: 2px 8px; }
  .pv-tag { font-size: 12px; color: var(--kit-muted, #666); }
  .pv-notes { display: grid; gap: 4px; margin: 4px 0 0; padding: 0; list-style: none; font-size: 12px; }
  .pv-notes > li { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; min-width: 0; }
  .pv-notes .ic { width: 14px; height: 14px; }
  .m-planned { color: var(--kit-faint, #8b8d98); }
  .m-planned > span { color: var(--kit-muted, #666); }
  /* the table: one row per environment, one column per visibility */
  .pv-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 13px; }
  .pv-table th, .pv-table td { padding: 10px 12px 10px 0; border-bottom: 1px solid var(--kit-border, #ddd); text-align: left; vertical-align: top; }
  .pv-table thead th { padding-top: 0; font-size: 12px; font-weight: 500; color: var(--kit-muted, #666); white-space: nowrap; }
  .pv-table thead th:first-child { width: 32%; }
  .pv-table thead th:not(:first-child) { width: 17%; }
  .pv-table tbody th { font-weight: 400; }
  .pv-table tbody th .pv-name { display: block; }
  .pv-table td.is-quiet { color: var(--kit-faint, #999); }
  .pv-table td.is-legacy { color: var(--kit-muted, #666); }
  .pv-table td.is-legacy .pv-sub { display: block; }
  .pv-legacy { display: block; color: var(--kit-text, inherit); }
  .pv-deny { display: inline-flex; align-items: center; gap: 4px; color: var(--kit-muted, #666); }
  .pv-deny .ic { width: 14px; height: 14px; }
  .pv-table td.is-denied .mark { display: flex; margin-top: 4px; font-size: 12px; }
  .pv-table tr.is-others th .pv-name { color: var(--kit-muted, #666); font-weight: 500; }
  .pv .empty { margin-top: 0; }
  @media (max-width: 1151px) {
    .pv-flow { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 760px) {
    .pv-bar { align-items: flex-start; }
    .pv-acts { margin-left: 0; }
    .pv-refresh { margin-left: 0; }
    .pv-flow { grid-template-columns: minmax(0, 1fr); gap: 16px; }
    .pv-table thead { display: none; }
    .pv-table, .pv-table tbody, .pv-table tr, .pv-table th, .pv-table td { display: block; width: auto; }
    .pv-table tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; padding: 12px 0; border-bottom: 1px solid var(--kit-border, #ddd); }
    .pv-table tr > * { padding: 0; border: 0; }
    .pv-table tbody th, .pv-table td.is-legacy { grid-column: 1 / -1; }
    .pv-table td.c::before { content: attr(data-label); display: block; font-size: 11px; color: var(--kit-faint, #999); }
    .pv-table td.is-denied { display: block; }
  }
`;
