// All presentation of the Environments panel: one CSS string and render functions returning HTML strings.
// Data logic lives in model.ts; restyle here. Colors and sizes come from the portal's /_shared/local-tools.css tokens
// (--kit-* / --lt-*), with plain fallbacks when the page runs on its own.
// Layout (v6): one headline ("5 things to handle") with the batch button on its right, three tabs over the same list
// of things to do (to do / by machine / overview), and a footer with each environment's freshness and a link to the
// glossary drawer. Terms are explained where they show, on hover or focus (data-tip). Every tab draws a thing to do
// with the same component (todoHtml): one or two lines, one main button picked by who handles it, the rest folded.
import type { Lang, T } from "./i18n.ts";
import {
  type Actor,
  ACTORS,
  type AgentRow,
  type AiTask,
  type AllStatus,
  type EvalRow,
  evalRows,
  type Freshness,
  freshness,
  type Glossary,
  gloss,
  type HabitCell,
  hostLabel,
  inboxGroups,
  isProblem,
  KINDS,
  kindSummary,
  LOCAL_SOURCE,
  type MachineCard,
  type MatrixCol,
  type MatrixGroup,
  MATRIX_COLS,
  overview,
  readRelFilter,
  readTab,
  type RelCol,
  type RelLayout,
  type RelNode,
  type RelState,
  type RelationGraph,
  REL_COLS,
  relAlert,
  type RelHabit,
  type AgentKind,
  agentKinds,
  sourcePath,
  habitFix,
  relationGraph,
  relationLayout,
  problemGraph,
  rowActions,
  cellTodos,
  type TabCounts,
  type Todo,
  todoFact,
  batchPayload,
  batchTodos,
  views,
  scrollHold,
  writeRelFilter,
  writeTab,
} from "./model.ts";
import { aiFixText, aiLead, aiSentence, type AiStep, aiStepsText, aiStepText, readsOnly, type SentenceOptions } from "./ai-copy.ts";
import { PLUGINS_CSS, PLUGINS_PRESET, pluginsFact, pluginsHtml, readPluginsView } from "./plugins-view.ts";
import type { LoadFailure, LoadResult } from "./source.ts";

export interface Ctx {
  t: T;
  lang: Lang;
  base: string;
  /** The portal handles AI Tasks for this page (ask: { bridge: "postMessage" }). */
  askBridge: boolean;
  now: number;
  /** The portal draws the page title in its own header bar; the page keeps the title for screen readers only. */
  headerBar?: boolean;
  /** The folder `bin/agent-env` commands run in on this machine (source.ts workFolders().run); goes into every
   *  sentence and fact handed to an AI. The server shows the home folder as `~`. */
  workdir?: string;
  /** The manifest file the engine reads (workFolders().manifest), for tasks that edit it; "" when none was found. */
  manifest?: string;
  /** No personal manifest was found: `workdir` is only the engine's own folder; sentences, facts and the footer say so. */
  noManifest?: boolean;
  /** `AGENT_ENV_MANIFEST=<path>` to put before every bin/agent-env command, or "" (workFolders().env). */
  envPrefix?: string;
}

type Place = Pick<Ctx, "workdir" | "noManifest" | "envPrefix">;
/** Where commands run on this machine, for ai-copy.ts. */
const whereOf = (ctx: Place) => ({ dir: ctx.workdir, env: ctx.envPrefix, noManifest: ctx.noManifest });
/** The same, for the facts handed to an AI: `command_prefix` and `manifest: null` only when they apply. */
const factWhere = (ctx: Place) => ({ workdir: ctx.workdir ?? "", ...(ctx.envPrefix ? { command_prefix: ctx.envPrefix } : {}), ...(ctx.noManifest ? { manifest: null } : {}) });

const copyButton = (ctx: Pick<Ctx, "t">, text: string, o: { quiet?: boolean; label?: string } = {}) =>
  `<button type="button" class="lt-btn lt-btn-sm${o.quiet === false ? "" : " lt-btn-ghost"}" data-copy="${esc(text)}" data-ai-copy data-tip="${esc(ctx.t("Copies a sentence to paste to your AI; the command is inside it"))}">${esc(o.label ?? ctx.t("Copy for AI"))}</button>`;

/** The one copy button for anything an AI should do: it copies a plain sentence (ai-copy.ts) with the commands inside
 *  it. `what` is already in the page's language and continues 帮我… / Please …. */
export function aiCopyButton(ctx: Pick<Ctx, "t" | "lang"> & Place, what: string, commands: string[] = [], o: SentenceOptions & { quiet?: boolean; label?: string } = {}): string {
  return copyButton(ctx, aiSentence(ctx.lang, what, commands, { ...whereOf(ctx), ...o }), o);
}

/** ai-tasks.json preset per `issue.ai_task`. */
export const AI_PRESETS: Record<AiTask, string> = { fix: "fix-drift", prepare: "prepare-step", logs: "check-logs" };
const AI_LABEL: Record<AiTask, string> = { fix: "Let AI fix it", prepare: "Let AI prepare it", logs: "Let AI read the logs" };
const AI_TIP: Record<AiTask, string> = {
  fix: "Opens the Local Tools AI task with this problem only; AI shows you each step before running it and stops where only you can act",
  prepare: "AI does a dry run first and shows it to you; nothing is written until you confirm",
  logs: "AI reads the logs of this problem and tells you what it found",
};
/** docs/ai-tasks.md §5: a fact value is at most 2,000 characters. */
export const FACT_LIMIT = 2000;

export function esc(s: unknown): string {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export const CSS = `
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; color: var(--kit-text, #1d1d1f); background: var(--kit-bg, var(--lt-page, #f7f7f5)); font-family: var(--kit-font, system-ui, sans-serif); font-size: var(--kit-font-size, 14px); line-height: 1.5; -webkit-font-smoothing: antialiased; }
  :focus-visible { outline: var(--kit-focus-outline, 2px solid #43721d); outline-offset: 2px; border-radius: 4px; }
  ::selection { background: var(--kit-accent-soft, #edf5e5); color: var(--kit-text, inherit); }
  /* one column for all four views (DESIGN.md): wide enough for the relation graph and its pane, so the title, the
     tabs, the view bar, the content and the footer share their left and right edges in every view */
  .wrap { max-width: 1240px; margin: 0 auto; padding: 16px 24px 64px; }
  a:not([class]) { color: var(--kit-accent, #43721d); text-underline-offset: 3px; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  pre { margin: 8px 0; padding: 8px 12px; overflow-x: auto; background: var(--lt-code-bg, var(--kit-surface-muted, #f4f4f4)); color: var(--lt-code-fg, inherit); border-radius: 8px; white-space: pre-wrap; word-break: break-word; }
  h1, h2, h3, h4, p { margin: 0; }
  .muted { color: var(--kit-muted, #666); }
  .faint { color: var(--kit-faint, #999); }
  .num { font-variant-numeric: tabular-nums; }
  .visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

  /* marks: colour only for states; who handles it is a grey icon explained on hover */
  .ic { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
  .mark { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
  .m-synced, .m-pass { color: var(--kit-ok, #2f7d32); }
  .m-customized { color: var(--kit-muted, #666); }
  .m-drift, .m-changed { color: var(--kit-warn, #a44d00); }
  .m-missing, .m-fail, .m-unreadable { color: var(--kit-fail, #c0392b); }
  .m-unknown, .m-err, .m-none, .m-skip { color: var(--kit-faint, #8b8d98); }
  .mark > span { color: var(--kit-text, inherit); }
  .m-none > span, .m-skip > span, .m-unknown > span { color: var(--kit-muted, #666); }
  .who { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; flex: none; color: var(--kit-muted, #666); border-radius: 4px; cursor: help; }
  .who:hover { color: var(--kit-text, inherit); }
  [data-tip] { cursor: help; }
  button[data-tip] { cursor: pointer; }

  /* header: the headline, refresh, and the one batch button on the far right */
  .top { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-height: 40px; }
  .top h1 { flex: 1 0 100%; font-size: 13px; font-weight: 500; color: var(--kit-muted, #666); }
  .top.bar h1 { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  .headline { display: flex; align-items: center; gap: 8px; margin-right: auto; font-size: var(--kit-title-size, 20px); font-weight: 600; letter-spacing: -0.01em; line-height: 1.3; }
  .headline .ic { width: 20px; height: 20px; color: var(--kit-ok, #2f7d32); }
  .top-acts { display: flex; align-items: center; gap: 8px; margin-left: auto; }

  /* switching controls (panel/DESIGN.md). Three levels, all shared components: .lt-tabs for the four views, one
     .lt-segmented in a view bar for a view's filter, one .lt-segmented in the side pane for an agent's four kinds.
     The page places them and does not restyle them. One 16px step from the title to the tabs, to the view bar, to
     the content, in every view; counts are quiet 12px numbers. */
  .views { --step: 16px; margin-top: var(--step); }
  .inbox-count { color: var(--kit-muted, #666); font-size: 12px; font-weight: 400; }
  .tabpanel { padding-top: var(--step); }
  /* switching views moves nothing but the underline: the views swap on the same frame, with no fade (a fade, or a
     cross-fade, leaves the content pale or gone for a few frames, which reads as a flash). The underline is the
     page's own bar (the shared one is drawn per tab and can only jump): a 1px piece the script places and stretches
     with a transform, so it slides on the compositor. The shared per-tab underline is switched off through the
     tab's role, the one place the page changes a shared control (DESIGN.md rule 1).
     html: where scrollbars take width, the room for one is always kept, so a long and a short view sit in the same
     place (the shared scrollbar-gutter asks for the same; this holds where that does not). */
  html { overflow-y: scroll; }
  .views > nav { position: relative; }
  /* five tabs in one row on a phone: the English names need the room the shared 4px gap takes (DESIGN.md) */
  @media (max-width: 420px) { .views > nav { gap: 0; } }
  .view-ink { position: absolute; left: 0; bottom: -1px; width: 1px; height: 2px; background: var(--lt-brand-strong, var(--kit-accent, #43721d)); transform-origin: 0 50%; visibility: hidden; pointer-events: none; will-change: transform; }
  .views > nav.has-ink > .view-ink { visibility: visible; transition: transform var(--duration-slow, 200ms) var(--ease-out, cubic-bezier(0.2, 0, 0, 1)); }
  .views > nav.has-ink > [role="tab"]::after { content: none; }
  .viewbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 24px; min-height: 32px; margin-bottom: var(--step); }
  /* a shared segment is 30px inside its 32px track: an unseen 1px above and below (5px on a phone) lets the whole
     32px (40px) take the click, and the look stays the shared one; the inset counts from inside the 1px border */
  .viewbar button, .rp-chips button { position: relative; }
  .viewbar button::before, .rp-chips button::before { content: ""; position: absolute; inset: -2px 0; }

  /* "to do" tab: a small label per group, then the rows */
  .group + .group { margin-top: 24px; }
  .group-h { padding-bottom: 6px; border-bottom: 1px solid var(--kit-border, #ddd); font-size: 12px; font-weight: 500; color: var(--kit-muted, #666); }
  .group-h .num { color: var(--kit-faint, #999); }
  .rest-ok { display: flex; gap: 8px; align-items: flex-start; margin-top: 24px; font-size: 13px; color: var(--kit-muted, #666); }
  .rest-ok .ic { color: var(--kit-ok, #2f7d32); margin-top: 2px; }

  /* a thing to do: the one row all three tabs use */
  .todo { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "main acts" "more more"; gap: 0 16px; padding: 12px 0; border-bottom: 1px solid var(--kit-border, #ddd); }
  .todo-main { grid-area: main; min-width: 0; }
  .todo-line { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; }
  .todo-title { font-size: 14px; font-weight: 600; line-height: 1.45; overflow-wrap: anywhere; }
  .todo-where { font-size: 12px; color: var(--kit-faint, #999); white-space: nowrap; }
  .todo-where .sep { margin: 0 4px; }
  .todo-impact { margin-top: 2px; max-width: 72ch; font-size: 13px; color: var(--kit-muted, #666); }
  .todo-acts { grid-area: acts; display: flex; align-items: center; gap: 4px; align-self: start; }
  .todo-more { grid-area: more; display: grid; gap: 8px; padding: 8px 0 4px; }
  .raw-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; font-size: 12px; color: var(--kit-faint, #999); }
  .raw-line code { color: var(--kit-muted, #666); overflow-wrap: anywhere; }
  .more-acts { display: flex; flex-wrap: wrap; gap: 8px; }
  .steps { display: grid; margin: 0; padding: 0; list-style: none; counter-reset: step; }
  .step { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 4px 8px; padding: 8px 0; counter-increment: step; scroll-margin: 80px; border-radius: 8px; }
  .step::before { content: counter(step); display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 50%; border: 1px solid var(--kit-border-strong, #ccc); font-size: 11px; color: var(--kit-muted, #666); font-variant-numeric: tabular-nums; }
  .step-body { display: grid; gap: 6px; min-width: 0; }
  .step-text { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; }
  .step-who { font-size: 12px; font-weight: 600; color: var(--kit-muted, #666); }
  .confirm-note { font-size: 12px; color: var(--kit-warn, #a44d00); }
  .cmd { display: flex; align-items: center; gap: 8px; padding: 4px 4px 4px 10px; border-radius: 8px; background: var(--lt-code-bg, var(--kit-surface-muted, #f4f4f4)); }
  .cmd code { flex: 1; min-width: 0; color: var(--lt-code-fg, inherit); word-break: break-all; }

  /* "by machine" tab: a heading per machine, then its agents' rows */
  .machine + .machine { margin-top: 32px; }
  .machine-h { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; padding-bottom: 8px; border-bottom: 1px solid var(--kit-border, #ddd); }
  .machine-h h2 { font-size: 16px; font-weight: 600; }
  .machine-state { font-size: 13px; color: var(--kit-muted, #666); }
  .machine-state.is-bad { color: var(--kit-fail, #c0392b); }
  .machine-state .muted { color: var(--kit-muted, #666); }
  .machine-meta { flex-basis: 100%; display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: 12px; color: var(--kit-faint, #999); }
  .machine-meta .is-stale { color: var(--kit-warn, #a44d00); font-weight: 600; }
  .m-agent-h { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 8px; padding: 14px 0 2px; font-size: 13px; font-weight: 600; }
  .agent-role { color: var(--kit-faint, #999); font-size: 12px; font-weight: 400; }
  .m-agent > .cells-line { padding: 4px 0 12px; border-bottom: 1px solid var(--kit-border, #ddd); }
  .disclose { display: flex; width: 100%; align-items: center; gap: 8px; min-height: 44px; padding: 8px 0; border: 0; border-bottom: 1px solid var(--kit-border, #ddd); background: none; color: var(--kit-text, inherit); font: inherit; text-align: left; cursor: pointer; }
  .disclose:hover .ok-names { color: var(--kit-text, inherit); }
  .disclose .ok-names { color: var(--kit-muted, #666); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
  .chev { display: inline-block; width: 0; height: 0; margin-left: 2px; border-left: 4px solid currentColor; border-top: 4px solid transparent; border-bottom: 4px solid transparent; transition: var(--lt-transition, transform 150ms ease-out); }
  [aria-expanded="true"] > .chev { transform: rotate(90deg); }
  .ok-list { margin: 0; padding: 0; list-style: none; }
  .ok-list li { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; padding: 8px 0; border-bottom: 1px solid var(--kit-border, #ddd); font-size: 13px; }
  .ok-list .ok-name { min-width: 120px; font-weight: 500; }
  .cells-line { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; }
  .cells-line .k { color: var(--kit-faint, #999); margin-right: 4px; }
  .machine-note { padding: 8px 0; font-size: 12px; color: var(--kit-faint, #999); }

  /* "overview" tab: one state icon per cell, its words on hover */
  .matrix-scroll { overflow-x: auto; }
  table.matrix { width: 100%; border-collapse: collapse; font-size: 13px; }
  .matrix th, .matrix td { padding: 6px 8px; border-top: 1px solid var(--kit-border, #ddd); text-align: left; vertical-align: middle; }
  .matrix thead th { padding-top: 0; border-top: 0; font-weight: 500; font-size: 12px; color: var(--kit-muted, #666); }
  .matrix thead th.c, .matrix td.c { text-align: center; }
  .matrix .grp th { padding-top: 20px; border-top: 0; font-weight: 600; font-size: 13px; }
  .matrix .grp .muted { margin-left: 8px; font-weight: 400; font-size: 12px; }
  .matrix tr.agent-h th { font-weight: 500; }
  .matrix .agent-h .agent-role { display: block; }
  .cell { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border: 0; border-radius: 8px; background: none; cursor: pointer; }
  .cell:hover { background: var(--kit-hover, #f4f4f4); }
  .cell[aria-expanded="true"] { background: var(--kit-surface-muted, #f0f0ee); }
  .who-cell { white-space: nowrap; }
  .who-cell .who + .who { margin-left: 4px; }
  tr.cell-detail > td { padding: 0 0 12px; border-top: 0; }
  .detail { padding: 0 16px; border-radius: 8px; background: var(--kit-surface-muted, #f6f6f4); }
  .detail .todo:last-child { border-bottom: 0; }
  .cell-explain { display: grid; gap: 8px; padding: 12px 0; }
  .cell-explain ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
  .cell-explain li { display: grid; grid-template-columns: minmax(0, 200px) auto minmax(0, 1fr); gap: 4px 12px; align-items: baseline; }
  .cell-explain li .d { color: var(--kit-muted, #666); overflow-wrap: anywhere; }
  .cell-explain .raw-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--kit-faint, #999); }
  table.matrix.only-problems tbody tr.agent-h:not(.has-todo), table.matrix.only-problems tbody.grp-ok { display: none; }

  /* "relations" tab: sources on the left, lines to one box per machine holding its agents, each agent's four habit
     cells right after its name; a pinned-node pane on the right; a tree by machine on narrow screens.
     4px grid; normal lines pale, problem chains in colour; counts are quiet text. The graph (672px) and the pane
     fill the page's one column; under 1152px the pane goes below the graph, under 761px the tree replaces both. */
  .rel-legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 0; font-size: 12px; color: var(--kit-muted, #666); }
  .lg { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
  .lg-line { width: 16px; height: 0; border-top: 1px solid var(--kit-border-strong, #ccc); }
  .lg-line.s-drift { border-top: 2px dashed var(--kit-warn-bar, #f0bf00); }
  .lg-line.s-missing { border-top: 2px solid var(--kit-fail-bar, #eb5757); }
  .lg-box { width: 16px; height: 12px; border: 1px dashed var(--kit-border-strong, #bbb); border-radius: 4px; background: var(--kit-surface-muted, #f4f4f4); }
  .rel-body { display: grid; grid-template-columns: 672px minmax(400px, 1fr); gap: 32px; align-items: start; }
  .rel-heads { position: relative; width: 672px; height: 16px; margin-bottom: 8px; font-size: 12px; line-height: 16px; color: var(--kit-faint, #999); }
  .rel-heads > span { position: absolute; top: 0; padding-left: 12px; }
  .rel-graph { position: relative; }
  .rel-graph[data-graph="problems"], .rel.only-problems .rel-graph[data-graph="all"] { display: none; }
  .rel.only-problems .rel-graph[data-graph="problems"] { display: block; }
  .rel-lines { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none; }
  .rl { fill: none; stroke: var(--kit-border-strong, #ccc); stroke-width: 1; opacity: 0.8; transition: opacity 150ms ease-out; }
  .rl.is-problem { stroke-width: 2; opacity: 1; }
  .rl.s-drift.is-problem { stroke: var(--kit-warn-bar, #f0bf00); stroke-dasharray: 6 4; }
  .rl.s-missing.is-problem { stroke: var(--kit-fail-bar, #eb5757); }
  .rbox { position: absolute; border: 1px solid var(--kit-border, #ddd); border-radius: 12px; background: var(--kit-surface, #fff); }
  .rbox.is-unreported { border-style: dashed; border-color: var(--kit-border-strong, #bbb); background: var(--kit-surface-muted, #f4f4f4); }
  .rn { position: absolute; display: flex; align-items: center; gap: 8px; margin: 0; padding: 0 8px 0 12px; border: 1px solid var(--kit-border, #ddd); border-radius: 8px; background: var(--kit-surface, #fff); color: var(--kit-text, inherit); font: inherit; font-size: 13px; line-height: 16px; text-align: left; transition: opacity 150ms ease-out, border-color 150ms ease-out, background-color 150ms ease-out; }
  button.rn, .rn-name { cursor: pointer; }
  button.rn.n-source:hover { border-color: var(--kit-border-strong, #bbb); }
  .rn-label { min-width: 0; font-weight: 500; white-space: nowrap; }
  .rn-sub { margin-left: auto; font-size: 12px; color: var(--kit-faint, #999); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .rn-bad { margin-left: auto; font-size: 12px; color: var(--kit-fail, #c0392b); font-variant-numeric: tabular-nums; white-space: nowrap; }
  /* a box header and the agent rows under it share one grid: name column, then four 40px cells */
  .rn.n-env, .rn.n-agent { display: grid; grid-template-columns: minmax(0, 1fr) repeat(4, 40px); gap: 0; }
  .rn.n-env { padding: 0 8px 0 12px; border: 0; border-bottom: 1px solid var(--kit-border, #ddd); border-radius: 12px 12px 0 0; background: transparent; }
  .rn.n-env:hover { background: var(--kit-hover, #f4f4f4); }
  .rn.n-env .rn-label { font-size: 14px; font-weight: 600; }
  .rn-two { display: grid; gap: 0; min-width: 0; }
  .rn-two .rn-sub { margin-left: 0; }
  .rn.n-env.is-unreported { border-bottom-style: dashed; border-bottom-color: var(--kit-border-strong, #bbb); }
  .rn.n-env.is-unreported .rn-sub { color: var(--kit-warn, #a44d00); }
  .rc-h { font-size: 12px; color: var(--kit-faint, #999); text-align: center; white-space: nowrap; overflow: hidden; }
  .rn.n-agent { padding: 0 4px 0 8px; border: 0; border-radius: 8px; background: transparent; }
  .rn.n-agent:hover { background: var(--kit-hover, #f4f4f4); }
  .rn-name { min-width: 0; height: 100%; padding: 0; border: 0; background: none; color: inherit; font: inherit; font-weight: 500; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rn.is-unreported .rn-label, .rn.is-unreported .rn-name, .rn.is-aux .rn-name { color: var(--kit-muted, #666); font-weight: 400; }
  .rc-row { display: inline-flex; flex: none; }
  .rc { display: inline-flex; align-items: center; justify-content: center; gap: 2px; width: 40px; height: 28px; padding: 0; border: 0; border-radius: 6px; background: none; font: inherit; font-size: 12px; font-variant-numeric: tabular-nums; cursor: pointer; }
  span.rc { cursor: default; }
  .rc .ic { width: 16px; height: 16px; }
  button.rc:hover { background: var(--kit-surface-muted, #ececea); }
  .rc.m-synced, .rc.m-customized { color: var(--kit-faint, #999); }
  .rn-divider { position: absolute; height: 0; border-top: 1px dashed var(--kit-border, #ddd); }
  .rn-idle { position: absolute; margin: 0; padding: 0 12px; font-size: 12px; line-height: 16px; color: var(--kit-faint, #999); }
  .rel-graph.is-focus .rl:not(.is-hot) { opacity: 0.25; }
  .rel-graph.is-focus .rl.is-hot:not(.is-problem) { stroke: var(--kit-muted, #666); stroke-width: 1.5; opacity: 1; }
  .rel-graph.is-focus .rn:not(.is-hot), .rel-graph.is-focus .rn-idle, .rel-graph.is-focus .rn-divider { opacity: 0.4; }
  .rel-graph.is-focus .rn.is-hot .rc:not(.is-hot) { opacity: 0.4; }
  .rel-graph .rn.is-pinned { outline: 1.5px solid var(--kit-text, #333); outline-offset: -1.5px; }
  .rc.is-pinned { outline: 1.5px solid var(--kit-text, #333); outline-offset: -2px; }
  /* the side pane */
  /* 8px on the right: the rows' hover background (.rp-rows, -8px) ends at the column's edge and nothing in the pane
     reaches past it, so the pane never scrolls sideways and no button is cut */
  .rel-aside { position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow: auto; overscroll-behavior: contain; padding: 0 8px 16px 24px; border-left: 1px solid var(--kit-border, #ddd); font-size: 13px; }
  .rel-pane { display: grid; gap: 12px; }
  .rel-pane h3 { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; line-height: 20px; }
  .rp-head { display: grid; gap: 4px; }
  .rp-meta { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; font-size: 12px; color: var(--kit-muted, #666); }
  .rp-sub { font-size: 12px; font-weight: 500; color: var(--kit-muted, #666); }
  .rp-rows { display: grid; gap: 0; margin: 0 -8px; padding: 0; list-style: none; }
  .rp-row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 32px; padding: 0 8px; border: 0; border-radius: 8px; background: none; color: var(--kit-text, inherit); font: inherit; text-align: left; cursor: pointer; }
  .rp-row:hover { background: var(--kit-hover, #f4f4f4); }
  .rp-row-l { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rp-row-r { font-size: 12px; color: var(--kit-muted, #666); }
  /* a row's buttons, one or several (their labels then name what each is for), go on a line of their own under
     the row and wrap there: every row reads the same, the label keeps its width and no button is cut off at the
     pane's edge */
  .rp-li { display: flex; flex-wrap: wrap; align-items: center; }
  .rp-li > .rp-row { flex: 1 0 100%; min-width: 0; }
  .rp-li-acts { display: flex; flex-wrap: wrap; gap: 4px 8px; flex: 1 0 100%; min-width: 0; padding: 0 8px 8px; }
  .rp-li-acts > * { max-width: 100%; }
  .rp-acts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  /* the four kinds of one agent: the shared segmented control, laid out as four equal columns */
  .rp-chips { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .rp-chip { justify-content: center; min-width: 0; padding: 0 6px; }
  .rp-chip > span:first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .rp-chip.is-empty { display: flex; align-items: center; gap: 4px; min-height: 30px; border: 1px solid transparent; font-size: 13px; font-weight: 500; line-height: 1.2; white-space: nowrap; color: var(--kit-faint, #999); }
  .rp-chip .mark { gap: 2px; }
  .rp-chip .num { color: var(--kit-muted, #666); font-variant-numeric: tabular-nums; }
  .rp-kind { display: grid; gap: 8px; }
  .rp-kh { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
  .rp-src { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 4px; font-size: 12px; color: var(--kit-muted, #666); }
  .rp-src-name { color: var(--kit-text, inherit); text-decoration: underline dotted var(--kit-border-strong, #bbb); text-underline-offset: 3px; }
  .rp-copy { margin-left: 8px; font-size: 12px; }
  .rp-bad { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
  .rp-bad > li { display: grid; gap: 4px; padding: 8px 12px; border-radius: 8px; background: var(--kit-surface-muted, #f4f4f4); }
  .rp-line { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
  .rp-name { font-weight: 500; overflow-wrap: anywhere; }
  .rp-why, .rp-fix { font-size: 12px; color: var(--kit-muted, #666); overflow-wrap: anywhere; }
  .rp-rest summary { display: flex; align-items: center; gap: 8px; min-height: 32px; font-size: 12px; color: var(--kit-muted, #666); cursor: pointer; list-style: none; }
  .rp-rest summary::-webkit-details-marker { display: none; }
  .rp-rest[open] > summary > .chev { transform: rotate(90deg); }
  .rp-names { display: flex; flex-wrap: wrap; gap: 0; padding-left: 16px; font-size: 12px; line-height: 20px; color: var(--kit-text, inherit); }
  .rp-names-k { margin-right: 8px; color: var(--kit-faint, #999); }
  .rp-names .sep { color: var(--kit-faint, #999); }
  .rp-note, .rp-hint { font-size: 12px; color: var(--kit-faint, #999); }
  .rel-tree { display: none; }
  .rt-head { display: flex; align-items: center; padding: 0 0 4px; font-size: 12px; color: var(--kit-faint, #999); }
  .rt-head > :first-child { flex: 1; }
  .rt-head .rc-h { width: 40px; }
  .rt-h { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; padding-bottom: 8px; border-bottom: 1px solid var(--kit-border, #ddd); font-size: 15px; font-weight: 600; }
  .rt-sub { font-size: 12px; font-weight: 400; color: var(--kit-faint, #999); }
  .rt-env.is-unreported .rt-h { border-bottom-style: dashed; }
  .rt-env.is-unreported .rt-sub { color: var(--kit-warn, #a44d00); }
  .rt-agents { margin: 0; padding: 0; list-style: none; }
  .rt-agent { border-bottom: 1px solid var(--kit-border, #ddd); }
  .rt-agent.is-aux { border-top: 1px dashed var(--kit-border, #ddd); }
  .rt-name { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 44px; padding: 0; border: 0; background: none; color: var(--kit-text, inherit); font: inherit; font-weight: 500; text-align: left; cursor: pointer; }
  .rt-label { flex: 1; min-width: 0; }
  .rt-agent.is-aux .rt-label { color: var(--kit-muted, #666); font-weight: 400; }
  .rt-detail { display: grid; gap: 12px; padding: 0 0 16px; font-size: 13px; }
  .rel.only-problems .rt-env:not(.is-alert), .rel.only-problems .rt-agent:not(.is-alert) { display: none; }
  [data-agent].is-flash, .machine.is-flash { background: var(--kit-accent-soft, #edf5e5); border-radius: 8px; transition: background 600ms ease-out; }
  @media (max-width: 1151px) {
    .rel-body { grid-template-columns: 672px; }
    .rel-aside { position: static; max-height: none; padding: 24px 0 0; border-left: 0; border-top: 1px solid var(--kit-border, #ddd); }
  }

  /* footer: freshness of each environment, the glossary link */
  .foot { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; margin-top: 40px; padding-top: 12px; border-top: 1px solid var(--kit-border, #ddd); font-size: 12px; color: var(--kit-muted, #666); }
  .fresh { margin-right: auto; }
  .fresh-label { margin-right: 4px; }
  html[lang|="zh"] .fresh-label { margin-right: 0; }
  .fresh .is-stale { color: var(--kit-warn, #a44d00); font-weight: 600; }
  .fresh .sep { margin: 0 4px; color: var(--kit-faint, #999); }
  .linkish { padding: 0; border: 0; background: none; color: var(--kit-accent, #43721d); font: inherit; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
  .preview-note { font-size: 12px; color: var(--kit-faint, #999); }

  /* glossary drawer: the one copy of the glossary, opened from the footer */
  dialog.drawer { position: fixed; inset: 0 0 0 auto; width: min(380px, 100vw); height: 100%; max-height: 100%; margin: 0; padding: 0; border: 0; border-left: 1px solid var(--kit-border, #ddd); background: var(--kit-surface, #fff); color: var(--kit-text, inherit); overflow: auto; }
  dialog.drawer::backdrop { background: rgb(0 0 0 / 0.28); }
  .drawer-h { position: sticky; top: 0; display: flex; align-items: center; justify-content: space-between; padding: 16px 20px 12px; background: var(--kit-surface, #fff); border-bottom: 1px solid var(--kit-border, #ddd); }
  .drawer-h h2 { font-size: 15px; font-weight: 600; }
  .drawer-body { padding: 4px 20px 24px; font-size: 13px; }
  .glossary { display: grid; gap: 10px; margin: 0; }
  .glossary dt { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 6px; font-weight: 600; }
  .glossary dt .raw { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; font-weight: 400; color: var(--kit-faint, #999); }
  .glossary dd { margin: 2px 0 0; color: var(--kit-muted, #666); }
  .gloss-group { padding-top: 16px; }
  .gloss-group h3 { margin-bottom: 8px; font-size: 12px; font-weight: 500; color: var(--kit-faint, #999); }

  /* states of the page */
  .empty { margin-top: 24px; padding: 20px 24px; color: var(--kit-muted, #666); background: var(--kit-surface, #fff); border: 1px dashed var(--kit-border-strong, #ccc); border-radius: var(--lt-radius-panel, 12px); }
  .empty p { margin: 4px 0; }
  .empty strong { color: var(--kit-text, inherit); font-size: 14px; }
  .error { border-style: solid; border-color: var(--kit-fail-bar, #eb5757); background: var(--kit-fail-soft, #fdf0ee); color: var(--kit-text, inherit); }
  .error h3 { margin-top: 12px; }
  .fix-line { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
  .fail-detail > span { margin-right: 8px; color: var(--kit-faint, #999); }
  .fail-detail code { overflow-wrap: anywhere; }
  .fix-line code { padding: 0 4px; border-radius: 4px; background: var(--kit-surface-muted, #f4f4f4); word-break: break-all; }
  .loading { display: flex; align-items: center; gap: 12px; }
  .spinner { width: 16px; height: 16px; border-radius: 50%; border: 2px solid var(--kit-border, #ddd); border-top-color: var(--kit-accent, #43721d); animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* narrow: the buttons drop under the title; the overview's rows become blocks with labelled cells */
  @media (max-width: 760px) {
    .wrap { padding: 12px 16px 48px; }
    .headline { font-size: 18px; }
    .todo { grid-template-columns: minmax(0, 1fr); grid-template-areas: "main" "acts" "more"; }
    .todo-acts { margin-top: 8px; }
    .cmd { flex-wrap: wrap; }
    .matrix thead { display: none; }
    .matrix, .matrix tbody, .matrix tr, .matrix th, .matrix td { display: block; width: auto; }
    .matrix tr.agent-h { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px 8px; padding: 10px 0; border-top: 1px solid var(--kit-border, #ddd); }
    .matrix tr.agent-h > * { padding: 0; border: 0; }
    .matrix tr.agent-h > th, .matrix tr.env-row > td, .matrix tr.agent-h > td.who-cell { grid-column: 1 / -1; width: auto; }
    .matrix tr.agent-h > td.c { text-align: left; }
    .matrix tr.agent-h > td.c::before { content: attr(data-label); display: block; font-size: 11px; color: var(--kit-faint, #999); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .matrix tr.agent-h > td.who-cell:not(:empty)::before { content: attr(data-label); display: block; font-size: 11px; color: var(--kit-faint, #999); }
    .matrix tr.agent-h > td.who-cell:empty { display: none; }
    .matrix .grp th { padding-top: 0; }
    .matrix tbody { padding-bottom: 20px; }
    .matrix tbody:last-child { padding-bottom: 0; }
    .cell { margin-left: -6px; }
    .cell-explain li { grid-template-columns: minmax(0, 1fr) auto; }
    .cell-explain li .d { grid-column: 1 / -1; }
    .rel-heads, .rel-main, .rel-aside { display: none; }
    .rel-body { display: block; }
    .rel-tree { display: grid; gap: 24px; }
    .rel-legend { display: none; }
    .rel-legend { gap: 4px 12px; }
    .viewbar button::before { inset: -6px 0; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
` + PLUGINS_CSS;

// ---- icons: one drawn set, 16 grid, 1.5 stroke ----

const ICONS: Record<string, string> = {
  ok: `<circle cx="8" cy="8" r="6"/><path d="M5.3 8.2l1.9 1.9 3.6-4"/>`,
  customized: `<circle cx="8" cy="8" r="6"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor" stroke="none"/>`,
  drift: `<path d="M8 2.5l6 10.5H2z"/><path d="M8 6.8v2.6M8 11.3v.01"/>`,
  missing: `<circle cx="8" cy="8" r="6"/><path d="M5.2 8h5.6"/>`,
  unknown: `<circle cx="8" cy="8" r="6"/><path d="M6.4 6.5a1.7 1.7 0 1 1 2.3 1.6c-.5.2-.7.5-.7 1v.2M8 11.3v.01"/>`,
  fail: `<circle cx="8" cy="8" r="6"/><path d="M6 6l4 4M10 6l-4 4"/>`,
  err: `<circle cx="8" cy="8" r="6" stroke-dasharray="2.2 2.2"/><path d="M8 5.6v3M8 10.6v.01"/>`,
  none: `<path d="M5 8h6"/>`,
  ai: `<rect x="3" y="5.5" width="10" height="8" rx="2"/><path d="M8 5.5V3M6 9.2v.6M10 9.2v.6M1.5 9.5v1.5M14.5 9.5v1.5"/>`,
  user: `<circle cx="8" cy="5.5" r="2.5"/><path d="M3 14c.6-2.6 2.6-4 5-4s4.4 1.4 5 4"/>`,
  other_env: `<rect x="2" y="3" width="12" height="8" rx="1.5"/><path d="M6 14h4M8 11v3"/>`,
  notice: `<circle cx="8" cy="8" r="6"/><path d="M8 7.4v3.6M8 5.1v.01"/>`,
  more: `<circle cx="3.5" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="8" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="12.5" cy="8" r="1.1" fill="currentColor" stroke="none"/>`,
};
const icon = (name: string) => `<svg class="ic" viewBox="0 0 16 16" aria-hidden="true">${ICONS[name] ?? ""}</svg>`;

/** Cell and step states to icons; one place, used by every tab. */
const STATE_ICON: Record<string, string> = {
  synced: "ok",
  pass: "ok",
  customized: "customized",
  drift: "drift",
  changed: "drift",
  missing: "missing",
  unknown: "unknown",
  fail: "fail",
  err: "err",
  unreadable: "fail",
  stale: "drift",
  skip: "none",
  none: "none",
};

// ---- small helpers ----

function fmtTime(ctx: Ctx, iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || ctx.t("time unknown");
  return new Intl.DateTimeFormat(ctx.lang === "zh" ? "zh-CN" : "en-GB", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
}

/** "today 14:43" for a time today; null for any other day or an unreadable time. */
function todayText(ctx: Ctx, iso: string): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const locale = ctx.lang === "zh" ? "zh-CN" : "en-GB";
  const day = (x: Date) => new Intl.DateTimeFormat(locale, { year: "numeric", month: "2-digit", day: "2-digit" }).format(x);
  const hm = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  return day(d) === day(new Date(ctx.now)) ? `${ctx.t("today")} ${hm}` : null;
}

/** "today 14:43" for a time today, else the date and time. */
function whenText(ctx: Ctx, iso: string): string {
  if (Number.isNaN(new Date(iso).getTime())) return ctx.t("time unknown");
  return todayText(ctx, iso) ?? fmtTime(ctx, iso);
}

function ago(ctx: Ctx, days: number | null): string {
  if (days === null) return ctx.t("time unknown");
  if (days === 0) return ctx.t("today");
  return days === 1 ? ctx.t("1 day ago") : ctx.t("{n} days ago", { n: days });
}

/** Role ids to translatable labels; an unlisted role shows as reported. */
const ROLE_LABELS: Record<string, string> = { cli: "coding assistant", "coding-cli": "coding assistant", bot: "chat bot", cron: "scheduled jobs", remote: "cloud-hosted" };

function roleText(ctx: Ctx, r: AgentRow): string {
  if (!r.roles.length) return r.short !== r.host ? r.short : "";
  return r.roles.map((x) => (ROLE_LABELS[x] ? ctx.t(ROLE_LABELS[x]) : x)).join(" · ");
}

/** Everything a render needs besides ctx: the glossary, and which hidden fact belongs to which thing to do. */
interface View {
  g: Glossary;
  facts: Map<string, string>;
  /** every thing to do, for the AI buttons outside the To do tab */
  todos: Todo[];
  agents: Map<string, AgentRow>;
  multiEnv: boolean;
}

/** A glossary word in the page's language; the key itself when the glossary has nothing for it. */
function word(ctx: Ctx, v: View, key: string): string {
  const e = gloss(v.g, key);
  if (!e) return key;
  return ctx.lang === "en" ? (e.label_en ?? e.label_zh) : e.label_zh;
}
function explain(ctx: Ctx, v: View, key: string): string {
  const e = gloss(v.g, key);
  if (!e) return "";
  return ctx.lang === "en" ? (e.explain_en ?? e.explain_zh) : e.explain_zh;
}
/** "Word\nwhat it means": the text of a hover/focus tip (.lt-tip keeps the line break). */
const tipText = (head: string, body: string) => (body ? `${head}\n${body}` : head);

const mark = (state: string, label: string, tip = "") => `<span class="mark m-${esc(state)}"${tip ? ` data-tip="${esc(tip)}"` : ""}>${icon(STATE_ICON[state] ?? "none")}<span>${esc(label)}</span></span>`;
/** Who handles it: a grey icon; the words show on hover or focus and are read by screen readers. */
const whoMark = (ctx: Ctx, v: View, a: Actor) => {
  const text = tipText(word(ctx, v, a), explain(ctx, v, a));
  return `<span class="who a-${a}" role="img" tabindex="0" aria-label="${esc(text.replace("\n", ": "))}" data-tip="${esc(text)}">${icon(a)}</span>`;
};

const pick = (ctx: Ctx, zh: string, en?: string) => (ctx.lang === "en" ? en || zh : zh);

/** "mac · Codex" for an agent, "wsl" for a whole machine. */
function whereText(v: View, t: Todo): string {
  const agent = v.agents.get(`${t.env}|${t.target}`);
  if (agent) return `${v.multiEnv ? `${esc(t.env)}<span class="sep">·</span>` : ""}${esc(hostLabel(agent.host))}`;
  if (t.target.includes("/")) return `${esc(t.env)}<span class="sep">·</span>${esc(hostLabel(t.target.split("/").pop()!))}`;
  return esc(t.target || t.env);
}

// ---- the one component: a thing to do ----

/** `name`: what this button is for, when several buttons with the same label sit side by side (one per thing to do
 *  behind the same cell); the label then reads "Copy for AI: <name>". */
function aiButton(ctx: Ctx, v: View, t: Todo, task: AiTask, quiet: boolean, name = ""): string {
  const { t: tr } = ctx;
  const fact = v.facts.get(t.id) ?? "";
  const cls = `lt-btn lt-btn-sm${quiet ? " lt-btn-ghost" : ""}`;
  const label = (text: string) => esc(name ? tr("{label}: {name}", { label: text, name }) : text);
  return ctx.askBridge
    ? `<button type="button" class="${cls}" data-ask="${AI_PRESETS[task]}" data-fact="${esc(fact)}" data-tip="${esc(tr(AI_TIP[task]))}">${label(tr(AI_LABEL[task]))}</button>`
    : `<button type="button" class="${cls}" data-copy-from="${esc(fact)}" data-copy-lead="${esc(aiLead(ctx.lang, AI_PRESETS[task], whereOf(ctx)))}" data-ai-copy data-tip="${esc(tr("Copies this problem, written for an AI. Paste it to your AI and it takes it from there"))}">${label(tr("Copy for AI"))}</button>`;
}

/** A thing to do, one or two lines: [who] title · where, then its impact in grey. One main button (model.ts
 *  rowActions: the AI button, or "copy commands" for another machine); "more" opens the rest: the steps with their
 *  commands (each with its copy-for-AI button), the portal's AI button and the internal names. `key` must be unique on the page; the same thing to do shows in all three tabs under different keys.
 *  `who: false` leaves out the actor icon where a group label already says it; `where: false` leaves out the place. */
export function todoHtml(ctx: Ctx, v: View, t: Todo, key: string, opts: { where?: boolean; who?: boolean } = {}): string {
  const { t: tr } = ctx;
  const acts = rowActions(t);
  const where = opts.where === false ? "" : `<span class="todo-where">${whereText(v, t)}</span>`;
  const who = opts.who === false ? "" : whoMark(ctx, v, t.actor);
  const impact = t.impact_zh ? `<p class="todo-impact">${esc(pick(ctx, t.impact_zh, t.impact_en))}</p>` : "";
  const main = `<div class="todo-main"><div class="todo-line">${who}<h3 class="todo-title">${esc(pick(ctx, t.title_zh, t.title_en))}</h3>${where}</div>${impact}</div>`;
  const title = pick(ctx, t.title_zh, t.title_en);
  // every step says where it runs from its own `who`: the other machine, or this machine's folder
  const aiSteps = t.steps.map((s): AiStep => ({ who: s.who, text: pick(ctx, s.text_zh, s.text_en), command: s.command, confirm: s.confirm }));
  const place = { ...whereOf(ctx), machine: t.env };
  const primary = acts.primary === "copy"
    ? copyButton(ctx, aiStepsText(ctx.lang, title, aiSteps, place), { quiet: false })
    : aiButton(ctx, v, t, acts.ai, false);
  const moreLabel = tr("More: the steps, with the commands for your AI");
  const more = `<button type="button" class="lt-btn lt-btn-sm lt-btn-icon" aria-expanded="false" aria-controls="${key}" data-toggle="${key}" aria-label="${esc(moreLabel)}" data-tip="${esc(moreLabel)}">${icon("more")}</button>`;
  const steps = t.steps.length
    ? `<ol class="steps">${t.steps
        .map((s, i) => {
          const whoText = s.who === "ai" ? "AI" : s.who === "other_env" ? tr("You, on {env}", { env: t.env }) : tr("You");
          const text = pick(ctx, s.text_zh, s.text_en);
          // the dry-run wording only when an earlier step is a dry run; local delivery has none
          const afterDryRun = t.steps.slice(0, i).some((x) => x.command?.includes("--dry-run"));
          const confirm = s.confirm ? `<span class="confirm-note">${esc(afterDryRun ? tr("look at the dry run, then confirm") : tr("Run it only after you confirm"))}</span>` : "";
          const cmd = s.command ? `<div class="cmd"><code>${esc(s.command)}</code>${copyButton(ctx, aiStepText(ctx.lang, title, aiSteps, i, place), { quiet: false, label: tr("Copy this step for AI") })}</div>` : "";
          return `<li class="step${s.confirm ? " is-confirm" : ""}" id="${key}-s${i}"><div class="step-body"><p class="step-text"><span class="step-who">${esc(whoText)}</span><span>${esc(text)}</span>${confirm}</p>${cmd}</div></li>`;
        })
        .join("")}</ol>`
    : "";
  // the portal's AI task stays one click away when the main button copies; without the portal both would copy
  const rest = [acts.aiInMore && ctx.askBridge ? aiButton(ctx, v, t, acts.ai, true) : ""].filter(Boolean);
  const raw = [t.ref.kind, t.ref.name || t.ref.case, t.state].filter(Boolean).join(" · ");
  const rawLine = raw ? `<p class="raw-line"><span>${esc(tr("Internal name, for your AI"))}</span><code>${esc(raw)}</code></p>` : "";
  const folded = `<div class="todo-more" id="${key}" hidden>${steps}${rest.length ? `<div class="more-acts">${rest.join("")}</div>` : ""}${rawLine}</div>`;
  return `<article class="todo" data-todo="${esc(t.id)}" data-actor="${t.actor}">${main}<div class="todo-acts">${primary}${more}</div>${folded}</article>`;
}

// ---- header and footer ----

function freshItem(ctx: Ctx, f: Freshness, iso: string): string {
  const { t } = ctx;
  const text = f.kind === "unreported" ? t("not reported") : f.kind === "unreadable" ? t("Could not be read") : f.stale && f.ageDays !== null ? t("{n} days without a report", { n: f.ageDays }) : (todayText(ctx, iso) ?? ago(ctx, f.ageDays));
  const tip = f.kind === "report" ? `${t("Last report")} ${fmtTime(ctx, iso)}` : "";
  return `<span class="${f.stale ? "is-stale" : ""}"${tip ? ` title="${esc(tip)}"` : ""}>${esc(f.env)} ${esc(text)}</span>`;
}

/** "Last report: laptop today 21:31 · vps-helper 3 days ago": when each environment last reported. An environment that is already a thing to do
 *  (not reported, stale, unreadable) is left out here: its row in the list says it. */
function freshHtml(ctx: Ctx, all: AllStatus, todos: Todo[] = []): string {
  const listed = new Set(todos.filter((x) => x.ref.kind === "env" || x.category.startsWith("env") || x.target === x.env).map((x) => x.env));
  const items = freshness(all, ctx.now)
    .filter((f) => !listed.has(f.env))
    .map((f) => freshItem(ctx, f, all.environments.find((e) => e.env === f.env)?.generated_at ?? ""))
    .join(`<span class="sep" aria-hidden="true">·</span>`);
  return items ? `<p class="fresh"><span class="fresh-label">${esc(ctx.t("Last report:"))}</span>${items}</p>` : "";
}

/** The header: the page title (for screen readers under the portal's bar), the headline, refresh, and the batch
 *  button on the far right. While loading or on an error only the title and refresh show. */
export function topHtml(ctx: Ctx, head?: { line: string; action: string }): string {
  const { t } = ctx;
  return `<header class="top${ctx.headerBar ? " bar" : ""}"><h1>${esc(t("Environments"))}</h1>${head?.line ?? ""}<div class="top-acts"><button type="button" class="lt-btn lt-btn-sm lt-btn-ghost" data-refresh>${esc(t("Refresh"))}</button>${head?.action ?? ""}</div></header>`;
}

/** "5 things to handle" with "let AI fix all (n)" beside it, or "Everything is fine". */
function headline(ctx: Ctx, todos: Todo[]): { line: string; action: string } {
  const { t } = ctx;
  if (!todos.length) return { line: `<p class="headline is-ok" role="status" data-count-total="0">${icon("ok")}<span>${esc(t("Everything is fine"))}</span></p>`, action: "" };
  const batch = batchTodos(todos).length;
  // one item would only repeat its row's button, with the strongest look on the page
  const action = batch < 2
    ? ""
    : ctx.askBridge
      ? `<button type="button" class="lt-btn lt-btn-primary" data-ask-all="fix-drift" data-batch="${batch}" data-tip="${esc(t("Sends every problem AI can fix by itself, each with its own context; AI shows you each step before running it"))}">${esc(t("Let AI fix all ({n})", { n: batch }))}</button>`
      : `<button type="button" class="lt-btn" data-copy-from="batch-notes" data-copy-lead="${esc(aiLead(ctx.lang, "fix-drift", whereOf(ctx)))}" data-ai-copy data-batch="${batch}" data-tip="${esc(t("Copies every problem AI can fix by itself, written for an AI. Paste it to your AI"))}">${esc(t("Copy all {n} for AI", { n: batch }))}</button>`;
  return { line: `<p class="headline" data-count-total="${todos.length}">${esc(todos.length === 1 ? t("1 thing to handle") : t("{n} things to handle", { n: todos.length }))}</p>`, action };
}

/** Shown when no personal manifest was found: what is wrong, what it means, and the sentence to give the AI. */
function noManifestHtml(ctx: Ctx, tag: "span" | "p"): string {
  if (!ctx.noManifest) return "";
  const { t } = ctx;
  const dir = ctx.workdir ? (ctx.lang === "zh" ? `（${ctx.workdir}）` : ` (${ctx.workdir})`) : "";
  const ask = t("find the folder that holds my personal agent-env manifest (manifest/habits.toml) and tell me where it is. The Environments panel cannot find it: it looks in the folder it is installed in{dir} and up to three folders above, or reads AGENT_ENV_MANIFEST. Do not change anything yet", { dir });
  return `<${tag} class="no-manifest" data-no-manifest>${esc(t("No personal manifest (manifest/habits.toml) was found, so the page cannot tell which folder is yours. The sentences you copy ask your AI to find it first; or give your AI this one:"))} ${copyButton(ctx, aiSentence(ctx.lang, ask))}</${tag}>`;
}

function footHtml(ctx: Ctx, all: AllStatus, preview?: string, glossary = true, todos: Todo[] = []): string {
  const { t } = ctx;
  const note = preview ? `<span class="preview-note">${esc(t("Preview data from {file}", { file: preview }))}</span>` : "";
  const link = glossary ? `<button type="button" class="linkish" data-gloss aria-haspopup="dialog" aria-controls="gloss">${esc(t("Glossary"))}</button>` : "";
  return `<footer class="foot">${freshHtml(ctx, all, todos)}${note}${noManifestHtml(ctx, "span")}${link}</footer>`;
}

// ---- glossary: one copy, in a drawer ----

const GLOSS_GROUPS: Array<[string, string[]]> = [
  ["Who handles it", [...ACTORS]],
  ["States", ["synced", "customized", "drift", "missing", "unknown"]],
  ["Habit kinds", [...KINDS]],
  ["Spot-checks", ["pass", "fail", "err", "skip"]],
  ["Other words", ["layer", "safety", "env", "role", "host", "none"]],
];

function glossaryHtml(ctx: Ctx, v: View): string {
  const { t } = ctx;
  const known = new Set(GLOSS_GROUPS.flatMap(([, keys]) => keys));
  const extra = Object.keys(v.g).filter((k) => !known.has(k) && !known.has(k.replace(/^.*[.:/]/, "")));
  const groups = [...GLOSS_GROUPS, ...(extra.length ? ([["More", extra]] as Array<[string, string[]]>) : [])];
  const item = (k: string) => {
    const e = gloss(v.g, k);
    if (!e) return "";
    const raw = k.replace(/^.*[.:/]/, "");
    const lead = ACTORS.includes(k as Actor) ? `<span class="who a-${k}">${icon(k)}</span>` : STATE_ICON[k] ? `<span class="mark m-${k}">${icon(STATE_ICON[k])}</span>` : "";
    return `<div><dt>${lead}${esc(word(ctx, v, k))}${raw !== word(ctx, v, k) ? `<span class="raw">${esc(raw)}</span>` : ""}</dt><dd>${esc(explain(ctx, v, k))}</dd></div>`;
  };
  return `<dialog class="drawer" id="gloss" aria-labelledby="gloss-h"><div class="drawer-h"><h2 id="gloss-h">${esc(t("Glossary"))}</h2><button type="button" class="lt-btn lt-btn-sm lt-btn-ghost" data-close>${esc(t("Close"))}</button></div><div class="drawer-body">${groups
    .map(([h, keys]) => `<div class="gloss-group"><h3>${esc(t(h))}</h3><dl class="glossary">${keys.map(item).join("")}</dl></div>`)
    .join("")}</div></dialog>`;
}

// ---- tab 1: to do ----

function inboxHtml(ctx: Ctx, v: View, todos: Todo[], all: AllStatus): string {
  const { t } = ctx;
  const o = overview(all, todos);
  const evalPass = all.environments.flatMap((e) => evalRows(e)).filter((r) => r.outcome === "pass").length;
  const restBits = [
    todos.length ? "" : t("last check {time}", { time: o.lastCheck ? whenText(ctx, o.lastCheck) : t("time unknown") }),
    o.synced + o.customized ? t("{n} habits in sync", { n: o.synced + o.customized }) + (o.customized ? t(" ({n} customised on purpose)", { n: o.customized }) : "") : "",
    evalPass ? t("{n} spot-checks passed", { n: evalPass }) : "",
  ].filter(Boolean);
  const lead = todos.length ? `${t("Everything else is fine")} · ` : "";
  const rest = restBits.length ? `<p class="rest-ok">${icon("ok")}<span>${esc(lead + restBits.join(" · "))}</span></p>` : "";
  // one small label per group says who handles it, so the rows leave the actor icon out
  const groups = inboxGroups(todos)
    .map(
      (g) => `<section class="group" data-group="${g.actor}"><h2 class="group-h">${esc(word(ctx, v, g.actor))}<span class="num"> · ${g.items.length}</span></h2>
${g.items.map((x) => todoHtml(ctx, v, x, `in-${todos.indexOf(x)}`, { who: false })).join("")}</section>`,
    )
    .join("");
  return groups + rest;
}

// ---- tab 2: by machine ----

function kindCells(ctx: Ctx, v: View, r: AgentRow): string {
  const cells = KINDS.map((k) => {
    const s = kindSummary(r.habits, k);
    if (s.state === "none") return "";
    const state = s.state === "drift" ? (s.counts.missing ? "missing" : "drift") : s.state;
    return `<span><span class="k">${esc(word(ctx, v, k))}</span>${mark(state, word(ctx, v, state), explain(ctx, v, state))}</span>`;
  }).filter(Boolean);
  const ev = r.eval;
  if (ev.state !== "none") cells.push(`<span><span class="k">${esc(ctx.t("Spot-checks"))}</span>${mark(ev.state, ev.state === "skip" ? word(ctx, v, "skip") : `${word(ctx, v, ev.state)} ${ev.passed}/${ev.total}`, ev.skipReason || explain(ctx, v, ev.state))}</span>`);
  return cells.length ? `<p class="cells-line">${cells.join("")}</p>` : `<p class="cells-line muted">${esc(ctx.t("Nothing registered to check"))}</p>`;
}

function machineHtml(ctx: Ctx, v: View, c: MachineCard, todos: Todo[], n: number): string {
  const { t } = ctx;
  const e = c.env_status;
  const fresh = freshness({ environments: e ? [e] : [], errors: [], issues: todos }, ctx.now)[0];
  // "live" only when this very run checked it: a cloud-hosted report read from a status branch is a past result
  const desc = !e ? ""
    : e.checked_from ? t(e.source === "live" ? "cloud-hosted, checked live from {env}" : "cloud-hosted, from the last report of {env}", { env: e.checked_from })
    : e.source === "live" ? t("this machine, checked live") : t("from its last report");
  const state =
    c.kind !== "report"
      ? `<p class="machine-state is-bad">${esc(c.kind === "unreported" ? t("Never reported: what its agents are doing is unknown") : t("Could not be read this time"))}</p>`
      : `<p class="machine-state${c.count ? " is-bad" : ""}" data-count="${c.count}">${esc(c.count ? (c.count === 1 ? t("1 to do") : t("{n} to do", { n: c.count })) : t("All fine"))}${c.ok.length ? `<span class="muted"> · ${esc(t("{n} agents fine", { n: c.ok.length }))}</span>` : ""}</p>`;
  const meta: string[] = [];
  if (e) {
    if (desc) meta.push(`<span>${esc(desc)}</span>`);
    meta.push(`<span class="${fresh?.stale ? "is-stale" : ""}">${esc(t("Reported {time}", { time: whenText(ctx, e.generated_at) }))}${fresh?.stale && fresh.ageDays !== null ? ` (${esc(t("{n} days ago", { n: fresh.ageDays }))})` : ""}</span>`);
    if (e.eval?.ran_at) meta.push(`<span>${esc(t("spot-checks ran {time}", { time: whenText(ctx, e.eval.ran_at) }))}</span>`);
    else meta.push(`<span>${esc(t("no spot-checks run here"))}</span>`);
  }
  const head = `<header class="machine-h"><h2>${esc(c.env)}</h2>${state}${meta.length ? `<p class="machine-meta">${meta.join("")}</p>` : ""}</header>`;
  const envTodos = c.envTodos.length ? `<div class="env-todos">${c.envTodos.map((x) => todoHtml(ctx, v, x, `mc-${todos.indexOf(x)}`, { where: false })).join("")}</div>` : "";
  const problem = c.problem
    .map((p) => {
      const role = roleText(ctx, p.row);
      const body = p.todos.length ? p.todos.map((x) => todoHtml(ctx, v, x, `mc-${todos.indexOf(x)}`, { where: false })).join("") : kindCells(ctx, v, p.row);
      return `<section class="m-agent" data-agent="${esc(p.row.id)}"><h3 class="m-agent-h">${esc(hostLabel(p.row.host))}${role ? `<span class="agent-role">${esc(role)}</span>` : ""}</h3>${body}</section>`;
    })
    .join("");
  const okKey = `ok-${n}`;
  const ok = c.ok.length
    ? `<div class="ok-agents"><button type="button" class="disclose" aria-expanded="false" aria-controls="${okKey}" data-toggle="${okKey}">${mark("synced", !c.problem.length && !c.envTodos.length ? (c.ok.length === 1 ? t("Fine") : t("All {n} are fine", { n: c.ok.length })) : c.ok.length === 1 ? t("1 other is fine") : t("{n} others are fine", { n: c.ok.length }))}<span class="ok-names">${esc(c.ok.map((r) => hostLabel(r.host)).join(" · "))}</span><span class="chev" aria-hidden="true"></span></button>
<ul class="ok-list" id="${okKey}" hidden>${c.ok.map((r) => `<li data-agent="${esc(r.id)}"><span class="ok-name">${esc(hostLabel(r.host))} <span class="agent-role">${esc(roleText(ctx, r))}</span></span>${kindCells(ctx, v, r)}</li>`).join("")}</ul></div>`
    : "";
  const unKey = `un-${n}`;
  const unchecked = c.unchecked.length
    ? `<div class="ok-agents"><button type="button" class="disclose" aria-expanded="false" aria-controls="${unKey}" data-toggle="${unKey}">${mark("unknown", t("{n} not checked this time", { n: c.unchecked.length }))}<span class="ok-names">${esc(c.unchecked.map((r) => hostLabel(r.host)).join(" · "))}</span><span class="chev" aria-hidden="true"></span></button>
<ul class="ok-list" id="${unKey}" hidden>${c.unchecked.map((r) => `<li data-agent="${esc(r.id)}"><span class="ok-name">${esc(hostLabel(r.host))} <span class="agent-role">${esc(roleText(ctx, r))}</span></span>${kindCells(ctx, v, r)}</li>`).join("")}</ul></div>`
    : "";
  const unverified = e && e.targets.some((x) => x.verified === false) ? `<p class="machine-note">${esc(t("This machine reported these results itself; this Mac has not re-checked them"))}</p>` : "";
  return `<article class="machine" data-env="${esc(c.env)}">${head}${envTodos}${problem}${ok}${unchecked}${unverified}</article>`;
}

function machinesHtml(ctx: Ctx, v: View, cards: MachineCard[], todos: Todo[]): string {
  return `<div class="machines">${cards.map((c, i) => machineHtml(ctx, v, c, todos, i)).join("")}</div>`;
}

// ---- tab 3: overview ----

function habitList(ctx: Ctx, v: View, habits: HabitCell[]): string {
  return `<ul>${habits.map((h) => `<li><span>${esc(h.name)}</span>${mark(h.state, word(ctx, v, h.state))}<span class="d">${esc(h.detail ?? "")}${h.layer && h.state === "customized" ? ` · ${esc(word(ctx, v, "layer"))}: ${esc(ctx.t(h.layer))}` : ""}</span></li>`).join("")}</ul>`;
}

function evalList(ctx: Ctx, v: View, rows: EvalRow[]): string {
  return `<ul>${rows
    .map((r) => {
      const name = r.name === "-" ? ctx.t("all cases") : r.label || r.name;
      const state = r.outcome === "unknown" ? "err" : r.outcome;
      return `<li><span>${esc(name)}${r.label && r.name !== "-" ? ` <span class="raw-name">${esc(r.name)}</span>` : ""}</span>${mark(state, word(ctx, v, state))}<span class="d">${esc([r.runs ? ctx.t("passed {p}/{n}", { p: r.passed ?? 0, n: r.runs }) : "", r.detail].filter(Boolean).join(" · "))}</span></li>`;
    })
    .join("")}</ul>`;
}

function cellHtml(ctx: Ctx, v: View, r: AgentRow, col: MatrixCol, its: Todo[], key: string): { td: string; detail: string } {
  const { t } = ctx;
  let state: string;
  let label: string;
  let sub = "";
  let body: string;
  if (col === "eval") {
    const ev = r.eval;
    state = ev.state;
    label = word(ctx, v, ev.state === "none" ? "none" : ev.state);
    if (ev.state === "fail" || ev.state === "err") sub = t("{p}/{n} passed", { p: ev.passed, n: ev.total });
    else if (ev.state === "pass") label = t("{p}/{n} passed", { p: ev.passed, n: ev.total });
    else if (ev.state === "skip") sub = ev.skipReason ? t("no way to run it") : "";
    body = r.evalRows.length ? evalList(ctx, v, r.evalRows) : `<p class="muted">${esc(explain(ctx, v, "none"))}</p>`;
  } else {
    const s = kindSummary(r.habits, col);
    state = s.state === "drift" ? (s.counts.missing ? "missing" : "drift") : s.state;
    label = word(ctx, v, state);
    if (s.total > 1) sub = t("{n} items", { n: s.total });
    const mine = r.habits.filter((h) => h.kind === col);
    body = mine.length ? habitList(ctx, v, mine) : `<p class="muted">${esc(explain(ctx, v, "none"))}</p>`;
  }
  const colWord = col === "eval" ? t("Spot-checks") : word(ctx, v, col);
  const meaning = explain(ctx, v, col === "eval" ? (state === "none" ? "none" : state) : state);
  const tip = tipText(`${colWord}: ${label}${sub ? ` · ${sub}` : ""}`, meaning);
  const td = `<td class="c" data-label="${esc(colWord)}"><button type="button" class="cell m-${esc(state)}${its.length ? " has-todo" : ""}" aria-expanded="false" aria-controls="${key}" data-toggle="${key}" aria-label="${esc(`${colWord}: ${label}${sub ? ` · ${sub}` : ""}`)}" data-tip="${esc(tip)}">${icon(STATE_ICON[state] ?? "none")}</button></td>`;
  const what = `<div class="cell-explain"><p><strong>${esc(hostLabel(r.host))} · ${esc(colWord)}</strong> <span class="muted">${esc(explain(ctx, v, col === "eval" ? (state === "none" ? "none" : state) : col))}</span></p>${body}</div>`;
  const detail = `<tr class="cell-detail" id="${key}" hidden><td colspan="7"><div class="detail">${its.map((x, i) => todoHtml(ctx, v, x, `${key}-${i}`, { where: false })).join("")}${what}</div></td></tr>`;
  return { td, detail };
}

function matrixHtml(ctx: Ctx, v: View, groups: MatrixGroup[], counts: TabCounts): string {
  const { t } = ctx;
  const agents = groups.reduce((n, g) => n + g.rows.length, 0);
  const bar = `<div class="viewbar"><div class="lt-segmented" role="group" aria-label="${esc(t("Rows to show"))}"><button type="button" class="lt-seg-item" aria-pressed="true" data-filter="all">${esc(t("All {n} agents", { n: agents }))}</button><button type="button" class="lt-seg-item" aria-pressed="false" data-filter="problems" data-problems="${counts.problems}">${esc(t("Only the {n} problems", { n: counts.problems }))}</button></div></div>`;
  const head = `<thead><tr><th scope="col">${esc(t("Machine / agent"))}</th>${MATRIX_COLS.map((c) => `<th scope="col" class="c">${esc(c === "eval" ? t("Spot-checks") : word(ctx, v, c))}</th>`).join("")}<th scope="col">${esc(t("Who handles it"))}</th></tr></thead>`;
  const bodies = groups
    .map((g, gi) => {
      const c = g.card;
      const desc = c.kind !== "report" ? (c.kind === "unreported" ? t("never reported") : t("could not be read")) : t("{n} agents", { n: g.rows.length });
      const rowsTodo = g.rows.reduce((n, r) => n + r.count, 0);
      const has = rowsTodo + g.envTodos.length > 0;
      const grp = `<tr class="grp"><th colspan="7" scope="colgroup">${esc(g.env)}<span class="muted">${esc(desc)}</span></th></tr>`;
      // a whole machine's things to do sit on one row of their own, open by default: nothing else explains the gap
      const envRow = g.envTodos.length
        ? `<tr class="agent-h has-todo env-row"><td colspan="7"><div class="detail">${g.envTodos.map((x, i) => todoHtml(ctx, v, x, `mx-${gi}-env-${i}`, { where: false })).join("")}</div></td></tr>`
        : "";
      const rows = g.rows
        .map((r, ri) => {
          const keyBase = `mx-${gi}-${ri}`;
          const cells = MATRIX_COLS.map((col) => cellHtml(ctx, v, r.row, col, r.cells[col], `${keyBase}-${col}`));
          const actors = [...new Set(ACTORS.filter((a) => [...MATRIX_COLS.flatMap((col) => r.cells[col]), ...r.cells.agent].some((x) => x.actor === a)))];
          const whole = r.cells.agent.length ? `<tr class="cell-detail" id="${keyBase}-agent" hidden><td colspan="7"><div class="detail">${r.cells.agent.map((x, i) => todoHtml(ctx, v, x, `${keyBase}-agent-${i}`, { where: false })).join("")}</div></td></tr>` : "";
          const name = `${esc(hostLabel(r.row.host))}${r.cells.agent.length ? ` <button type="button" class="lt-btn lt-btn-ghost lt-btn-sm" aria-expanded="false" aria-controls="${keyBase}-agent" data-toggle="${keyBase}-agent">${mark("changed", t("upgraded"))}</button>` : ""}<span class="agent-role">${esc(roleText(ctx, r.row))}</span>`;
          return `<tr class="agent-h${r.count ? " has-todo" : ""}" data-target="${esc(r.row.id)}" data-count="${r.count}"><th scope="row">${name}</th>${cells.map((x) => x.td).join("")}<td class="who-cell" data-label="${esc(t("Who handles it"))}">${actors.map((a) => whoMark(ctx, v, a)).join("")}</td></tr>${cells.map((x) => x.detail).join("")}${whole}`;
        })
        .join("");
      return `<tbody class="${has ? "grp-todo" : "grp-ok"}" data-env="${esc(g.env)}" data-count="${rowsTodo + g.envTodos.length}">${grp}${envRow}${rows}</tbody>`;
    })
    .join("");
  return `${bar}<div class="matrix-scroll"><table class="matrix">${head}${bodies}</table></div>`;
}

// ---- tab 4: relation graph ----
// Sources on the left, one box per machine on the right holding its agents, each agent with its four habit cells
// right after its name (model.ts relationGraph / relationLayout, fixed px widths). Lines run from a source to a
// machine box's header; normal ones are pale, problem chains are coloured. Click a node or a cell to pin it: its
// chains stay lit, the rest fade, and the side pane shows its details (paths only in hover text or "copy path").
// Click again or press Esc to let go. Under 760px the same data is a tree by machine.

/** Source keys to words; a key the engine adds later shows as given. */
const SOURCE_WORDS: Record<string, [string, string]> = {
  rules: ["Shared rules", "Every agent's rules are made from this file"],
  docs: ["Rule docs", "Read on demand from the rules; not checked per agent"],
  hooks: ["Script folder", "Hook scripts that agents run before or after their actions"],
  skills: ["Shared skills", "Skills shared by every machine; not checked per agent"],
  company_skills: ["Company skills", "Company skills copied into the bots that use them"],
  portal: ["Local Tools list", "The Local Tools portal's list of tools; each tool's files are checked"],
  manifest: ["Listed in the manifest", "MCP servers and switches the manifest lists for each agent"],
  [LOCAL_SOURCE]: ["The machine itself", "Logins, services and settings only that machine has"],
};

const sourceWord = (ctx: Ctx, id: string) => (SOURCE_WORDS[id] ? ctx.t(SOURCE_WORDS[id][0]) : id);
const sourceExplain = (ctx: Ctx, id: string) => (SOURCE_WORDS[id] ? ctx.t(SOURCE_WORDS[id][1]) : "");
const agentName = (ctx: Ctx, n: RelNode) => (n.aux ? ctx.t("{name} account folders", { name: n.label }) : n.label);
const listJoin = (ctx: Ctx, xs: string[]) => xs.join(ctx.lang === "zh" ? "、" : ", ");
/** A portal item is named "门户·<tool>"; under "from Local Tools list" the tool name alone says it. */
const itemName = (h: RelHabit) => (h.origin === "portal" ? h.name.replace(/^[^·]+·/, "") : h.name);

/** How fresh a machine's report is: [short, for the node; long, for the pane]. */
function envReport(ctx: Ctx, all: AllStatus, n: RelNode): [string, string] {
  const { t } = ctx;
  const f = n.report;
  if (!f || f.kind === "unreported") return [t("never reported yet"), t("Never reported: what its agents are doing is unknown")];
  if (f.kind === "unreadable") return [t("could not be read"), t("Could not be read this time")];
  const iso = all.environments.find((e) => e.env === n.id)?.generated_at ?? "";
  const long = t("Reported {time}", { time: whenText(ctx, iso) });
  if (f.stale && f.ageDays !== null) return [t("{n} days without a report", { n: f.ageDays }), long];
  return [t("reported {time}", { time: todayText(ctx, iso) ?? ago(ctx, f.ageDays) }), long];
}

/** The four habit cells of an agent: buttons in the graph (click pins that cell's chains), plain marks elsewhere. */
function relCells(ctx: Ctx, v: View, a: RelNode, live: boolean): string {
  return a.kinds
    .map((c) => {
      const what = c.state === "none" ? (a.report ? ctx.t("not reported, state unknown") : word(ctx, v, "none")) : `${word(ctx, v, c.state)}${c.total > 1 ? ` · ${ctx.t("{n} items", { n: c.total })}` : ""}`;
      const tip = `${word(ctx, v, c.kind)}${ctx.t(": ")}${what}`;
      const inner = `${icon(STATE_ICON[c.state] ?? "none")}${c.problems ? `<span class="num">${c.problems}</span>` : ""}`;
      return live
        ? `<button type="button" class="rc m-${c.state}" data-focus="${esc(c.key)}" data-on="${esc(c.on.join(" "))}" aria-label="${esc(tip)}" data-tip="${esc(tip)}">${inner}</button>`
        : `<span class="rc m-${c.state}" role="img" aria-label="${esc(tip)}">${inner}</span>`;
    })
    .join("");
}

/** "from Shared rules" once per kind; each source's path in its hover text, and one "copy path" button. */
function originLine(ctx: Ctx, g: RelationGraph, env: string, origins: string[]): string {
  if (!origins.length) return "";
  const paths = origins.map((o) => sourcePath(g, o, env)).filter(Boolean);
  const names = origins.map((o) => {
    const p = sourcePath(g, o, env);
    return p ? `<span class="rp-src-name" tabindex="0" data-tip="${esc(p)}">${esc(sourceWord(ctx, o))}</span>` : esc(sourceWord(ctx, o));
  });
  const copy = paths.length ? `<button type="button" class="linkish rp-copy" data-copy="${esc(paths.join("\n"))}">${esc(paths.length === 1 ? ctx.t("Copy path") : ctx.t("Copy {n} paths", { n: paths.length }))}</button>` : "";
  return `<p class="rp-src">${esc(ctx.t("from {list}", { list: "" }))}${names.join(ctx.lang === "zh" ? "、" : ", ")}${copy}</p>`;
}

/** A fix: one copyable line per command (writes and confirmations stay in the Inbox's steps), or the sentence. */
function fixHtml(ctx: Ctx, fix: string, what = ""): string {
  if (!fix) return "";
  const f = habitFix(fix);
  const confirm = f.confirm && f.commands.length ? `<p class="rp-fix">${esc(ctx.t("The step that needs your OK is in the To do tab"))}</p>` : "";
  // the fix is exactly one command: a sentence with that command, careful unless it only reads
  if (f.commands.length === 1 && f.commands[0] === fix.trim()) {
    const c = f.commands[0];
    return `<div class="cmd"><code>${esc(c)}</code>${aiCopyButton(ctx, ctx.t("handle this problem: {what}", { what: what || fix }), [c], { careful: f.confirm || !readsOnly(c), quiet: false })}</div>${confirm}`;
  }
  // a sentence (it may name several commands whose order matters): the commands show as before, as code, and one
  // button copies the whole fix, so no command is handed out apart from the ones before it
  const cmds = f.commands.map((c) => `<div class="cmd"><code>${esc(c)}</code></div>`).join("");
  const text = f.text ? `<p class="rp-fix">${esc(f.text)}</p>` : "";
  return `${text}${cmds}<div class="rp-fix-copy">${copyButton(ctx, aiFixText(ctx.lang, what || fix, fix, whereOf(ctx)), { quiet: false })}</div>${confirm}`;
}

/** One kind of one agent: its source once, problems open with their reason and fix, the rest one folded line. */
function kindSection(ctx: Ctx, v: View, g: RelationGraph, a: RelNode, k: AgentKind, opts: { hidden: boolean; heading: boolean }): string {
  const { t } = ctx;
  let bad = k.problems.length
    ? `<ul class="rp-bad">${k.problems.map((h) => `<li><p class="rp-line"><span class="rp-name">${esc(itemName(h))}</span>${mark(h.state, word(ctx, v, h.state))}</p>${h.detail ? `<p class="rp-why">${esc(h.detail)}</p>` : ""}${fixHtml(ctx, h.fix, `${agentName(ctx, a)} · ${itemName(h)} (${word(ctx, v, h.state)})${h.detail ? `: ${h.detail}` : ""}`)}</li>`).join("")}</ul>`
    : "";
  let rest = "";
  if (k.rest.length) {
    const groups = (["synced", "customized", "unknown"] as const).filter((s) => k.counts[s]);
    const summary = groups.map((s) => t("{n} {state}", { n: k.counts[s], state: word(ctx, v, s) })).join(" · ");
    const names = groups
      .map((s) => `<p class="rp-names"><span class="rp-names-k">${esc(word(ctx, v, s))}</span>${k.rest.filter((h) => h.state === s).map((h) => `<span${h.detail ? ` tabindex="0" data-tip="${esc(h.detail)}"` : ""}>${esc(itemName(h))}</span>`).join(`<span class="sep" aria-hidden="true">${ctx.lang === "zh" ? "、" : ", "}</span>`)}</p>`)
      .join("");
    rest = `<details class="rp-rest"><summary><span class="chev" aria-hidden="true"></span>${esc(summary)}</summary>${names}</details>`;
  }
  const ai = k.problems.length ? cellAi(ctx, v, a.env, a.id, k.kind, false) : "";
  if (ai) bad += `<div class="rp-acts">${ai}</div>`;
  const head = opts.heading ? `<h4 class="rp-kh">${mark(k.state, word(ctx, v, k.kind))}</h4>` : "";
  return `<section class="rp-kind" data-kind="${k.kind}"${opts.hidden ? " hidden" : ""}>${head}${originLine(ctx, g, a.env, k.origins)}${bad}${rest}</section>`;
}

/** Details of one agent. "tabs": one block per kind on top, one kind open below (the side pane); "flat": every kind
 *  one after another (the narrow tree). */
function agentDetail(ctx: Ctx, v: View, g: RelationGraph, a: RelNode, mode: "tabs" | "flat"): string {
  const { t } = ctx;
  if (a.report) return `<p class="rp-note">${esc(t("This machine has not reported: what this agent has is unknown"))}</p>`;
  const ks = agentKinds(a);
  if (!ks.length) return `<p class="rp-note">${esc(t("Nothing registered to check"))}</p>`;
  const open = (ks.find((k) => k.problems.length) ?? ks[0]).kind;
  const chips =
    mode === "tabs"
      ? `<div class="lt-segmented rp-chips" role="group" aria-label="${esc(t("Habit kinds"))}">${a.kinds
          .map((c) => {
            const label = `<span>${esc(word(ctx, v, c.kind))}</span>`;
            if (!c.total) return `<span class="rp-chip is-empty" aria-label="${esc(`${word(ctx, v, c.kind)}${t(": ")}${word(ctx, v, "none")}`)}">${label}<span class="mark m-none">${icon("none")}</span></span>`;
            const n = c.problems || c.total;
            return `<button type="button" class="lt-seg-item rp-chip" data-focus="${esc(c.key)}" data-keep data-chip="${c.kind}" aria-pressed="${c.kind === open}">${label}<span class="mark m-${c.state}">${icon(STATE_ICON[c.state] ?? "none")}<span class="num">${n}</span></span></button>`;
          })
          .join("")}</div>`
      : "";
  return `${chips}${ks.map((k) => kindSection(ctx, v, g, a, k, { hidden: mode === "tabs" && k.kind !== open, heading: mode === "flat" })).join("")}`;
}

/** A compact row in a pane: an agent with its cells, or a problem cell; clicking it pins that node or cell.
 *  `after`: the row's buttons, on their own line under the label. */
const paneRow = (key: string, left: string, right: string, after = "") =>
  `<li${after ? ` class="rp-li"` : ""}><button type="button" class="rp-row" data-focus="${esc(key)}"><span class="rp-row-l">${left}</span>${right}</button>${after ? `<div class="rp-li-acts">${after}</div>` : ""}</li>`;

/** The To do tab's AI button for the things to do behind one cell; "" when no AI task can take them. */
function cellAi(ctx: Ctx, v: View, env: string, agent: string, kind: string, quiet: boolean): string {
  const todos = cellTodos(v.todos, env, agent, kind);
  // several things to do behind one cell: each button says which one it is for
  const name = (t: Todo) => (todos.length > 1 ? t.ref.name || pick(ctx, t.title_zh, t.title_en) : "");
  return todos.map((t) => aiButton(ctx, v, t, rowActions(t).ai, quiet, name(t))).join("");
}

/** Side pane of one pinned node. */
function relPane(ctx: Ctx, v: View, all: AllStatus, g: RelationGraph, n: RelNode): string {
  const { t } = ctx;
  const agentRow = (a: RelNode) => paneRow(a.key, esc(agentName(ctx, a)), `<span class="rc-row">${relCells(ctx, v, a, false)}</span>`);
  let body: string;
  let extra = "";
  if (n.col === "source") {
    const reach = g.agents.filter((a) => a.on.includes(n.key));
    const envs = [...new Set(reach.map((a) => a.env))];
    const paths = [...new Set(envs.map((e) => sourcePath(g, n.id, e)).filter(Boolean))];
    const copy = paths.length ? `<button type="button" class="linkish rp-copy" data-copy="${esc(paths.join("\n"))}" data-tip="${esc(paths.join("\n"))}">${esc(paths.length === 1 ? t("Copy path") : t("Copy {n} paths", { n: paths.length }))}</button>` : "";
    body = `<header class="rp-head"><h3>${esc(sourceWord(ctx, n.id))}</h3><p class="rp-meta">${esc(sourceExplain(ctx, n.id))}</p>${copy ? `<p class="rp-meta">${copy}</p>` : ""}</header><h4 class="rp-sub">${esc(t("Reaches {n} agents", { n: reach.length }))}</h4><ul class="rp-rows">${reach.map(agentRow).join("")}</ul>`;
  } else if (n.col === "env") {
    body = `<header class="rp-head"><h3>${esc(n.label)}</h3><p class="rp-meta">${esc(envReport(ctx, all, n)[1])}</p></header><ul class="rp-rows">${g.agents.filter((a) => a.env === n.id).map(agentRow).join("")}</ul>`;
  } else {
    const env = g.envs.find((e) => e.id === n.env);
    const role = n.roles.map((x) => (ROLE_LABELS[x] ? t(ROLE_LABELS[x]) : x)).join(" · ");
    const ks = agentKinds(n);
    extra = ` data-default-kind="${(ks.find((k) => k.problems.length) ?? ks[0])?.kind ?? ""}"`;
    body = `<header class="rp-head"><h3>${esc(agentName(ctx, n))}</h3><p class="rp-meta">${esc([env?.label ?? n.env, role].filter(Boolean).join(" · "))}<button type="button" class="linkish" data-goto="${esc(n.id)}" data-goto-env="${esc(n.env)}">${esc(t("See it under Machines"))}</button></p></header>${agentDetail(ctx, v, g, n, "tabs")}`;
  }
  return `<section class="rel-pane" data-pane="${esc(n.key)}"${extra} hidden>${body}<p class="rp-hint">${esc(t("Click it again or press Esc to see everything"))}</p></section>`;
}

/** The pane with nothing pinned: the problem cells and the machines nobody heard from, each a shortcut to its chain. */
function relDefaultPane(ctx: Ctx, v: View, all: AllStatus, g: RelationGraph): string {
  const { t } = ctx;
  const bad = g.agents.flatMap((a) => a.kinds.filter((c) => isProblem(c.state as RelState)).map((c) => ({ a, c })));
  const stale = g.envs.filter((e) => e.report && (e.report.kind !== "report" || e.report.stale));
  const rows = [
    ...bad.map(({ a, c }) => paneRow(c.key, `${mark(c.state, `${agentName(ctx, a)} · ${word(ctx, v, c.kind)}`)}`, `<span class="rp-row-r">${esc(word(ctx, v, c.state))}</span>`, cellAi(ctx, v, a.env, a.id, c.kind, true))),
    ...stale.map((e) => paneRow(e.key, mark("none", e.label), `<span class="rp-row-r">${esc(envReport(ctx, all, e)[0])}</span>`)),
  ];
  const head = rows.length ? `<h3>${esc(t("Where the problems are"))}</h3>` : `<h3>${mark("synced", t("No problems"))}</h3>`;
  return `<section class="rel-pane" data-pane="">${head}${rows.length ? `<ul class="rp-rows">${rows.join("")}</ul>` : ""}<p class="rp-hint">${esc(t("Click any item or cell to see only what it connects to"))}</p></section>`;
}

/** The bezier from a source to its machine box's header, in px. */
function relPath(lay: RelLayout, from: RelNode, to: RelNode): string {
  const x1 = lay.cols.source.x + lay.cols.source.w;
  const x2 = lay.cols.box.x;
  const y1 = lay.y[from.key] + lay.h.source / 2;
  const y2 = lay.y[to.key] + lay.h.env / 2;
  const mid = (x1 + x2) / 2;
  return `M${x1} ${y1}C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`;
}

function relGraphHtml(ctx: Ctx, v: View, all: AllStatus, g: RelationGraph, which: "all" | "problems"): string {
  const { t } = ctx;
  const lay = relationLayout(g);
  const byKey = new Map([...g.sources, ...g.envs, ...g.agents].map((n) => [n.key, n]));
  const { source: sc, box } = lay.cols;
  const paths = [...g.links]
    .sort((a, b) => Number(isProblem(a.state)) - Number(isProblem(b.state)))
    .map((l) => `<path class="rl s-${l.state}${isProblem(l.state) ? " is-problem" : ""}" d="${relPath(lay, byKey.get(l.from)!, byKey.get(l.to)!)}" data-on="${esc(l.on.join(" "))}"/>`)
    .join("");
  const unreported = (n: RelNode) => !!n.report && n.report.kind !== "report";
  const base = (n: RelNode) => `rn n-${n.col} s-${n.state}${relAlert(n) ? " is-alert" : ""}${unreported(n) ? " is-unreported" : ""}${n.aux ? " is-aux" : ""}`;
  const attrs = (n: RelNode) => `data-node="${esc(n.key)}" data-on="${esc(n.on.join(" "))}"`;
  const kindHeads = KINDS.map((k) => `<span class="rc-h">${esc(word(ctx, v, k))}</span>`).join("");
  const sources = g.sources.map((n) => {
    const sub = n.problems ? `<span class="rn-bad">${esc(t("{n} with problems", { n: n.problems }))}</span>` : `<span class="rn-sub">${esc(t("{n} items", { n: n.cells }))}</span>`;
    return `<button type="button" class="${base(n)}" style="left:${sc.x}px;width:${sc.w}px;top:${lay.y[n.key]}px;height:${lay.h.source}px" ${attrs(n)} data-focus="${esc(n.key)}" data-tip="${esc(sourceExplain(ctx, n.id))}"><span class="rn-label">${esc(sourceWord(ctx, n.id))}</span>${sub}</button>`;
  });
  const boxes = g.envs.map((n) => {
    const [short] = envReport(ctx, all, n);
    const label = [n.label, short, n.problems ? t("{n} with problems", { n: n.problems }) : ""].filter(Boolean).join("，");
    const frame = `<div class="rbox${unreported(n) ? " is-unreported" : ""}" style="left:${box.x}px;width:${box.w}px;top:${lay.y[n.key]}px;height:${lay.boxH[n.key]}px" aria-hidden="true"></div>`;
    const head = `<button type="button" class="${base(n)}" style="left:${box.x}px;width:${box.w}px;top:${lay.y[n.key]}px;height:${lay.h.env}px" ${attrs(n)} data-focus="${esc(n.key)}" aria-label="${esc(label)}"><span class="rn-two"><span class="rn-label">${esc(n.label)}</span><span class="rn-sub">${esc(short)}</span></span>${kindHeads}</button>`;
    const rows = g.agents
      .filter((a) => a.env === n.id)
      .map((a) => `<div class="${base(a)}" style="left:${box.x + 4}px;width:${box.w - 8}px;top:${lay.y[a.key]}px;height:${lay.h.agent}px" ${attrs(a)}><button type="button" class="rn-name" data-focus="${esc(a.key)}">${esc(agentName(ctx, a))}</button>${relCells(ctx, v, a, true)}</div>`)
      .join("");
    return frame + head + rows;
  });
  const divider = lay.dividers.map((y) => `<span class="rn-divider" style="left:${box.x + 12}px;width:${box.w - 24}px;top:${y}px"></span>`).join("");
  const idle = lay.idleY !== null ? `<p class="rn-idle" style="left:${sc.x}px;width:${sc.w}px;top:${lay.idleY}px" data-tip="${esc(g.idle.map((s) => `${sourceWord(ctx, s.id)}：${s.path}`).join("\n"))}">${esc(t("Not checked per agent: {list}", { list: listJoin(ctx, g.idle.map((s) => sourceWord(ctx, s.id))) }))}</p>` : "";
  const noSources = g.hasSources ? "" : `<p class="rn-idle" style="left:${sc.x}px;width:${sc.w}px;top:0">${esc(t("This report does not say where habits come from (the checking tool on that machine is older)"))}</p>`;
  return `<div class="rel-graph" data-graph="${which}" style="width:${lay.width}px;height:${lay.height}px" role="group" aria-label="${esc(t("Where habits come from and where they go"))}"><svg class="rel-lines" width="${lay.width}" height="${lay.height}" viewBox="0 0 ${lay.width} ${lay.height}" aria-hidden="true">${paths}</svg>${noSources}${idle}${boxes.join("")}${divider}${sources.join("")}</div>`;
}

function relationHtml(ctx: Ctx, v: View, all: AllStatus): string {
  const { t } = ctx;
  const g = relationGraph(all, ctx.now);
  const pg = problemGraph(g);
  const kindHeads = KINDS.map((k) => `<span class="rc-h">${esc(word(ctx, v, k))}</span>`).join("");
  const heads = `<span style="left:${REL_COLS.source.x}px">${esc(t("Comes from"))}</span><span style="left:${REL_COLS.box.x}px">${esc(t("Machine"))}</span>`;
  const panes = [relDefaultPane(ctx, v, all, g), ...[...g.sources, ...g.envs, ...g.agents].map((n) => relPane(ctx, v, all, g, n))].join("");

  // narrow screens: machine → agent rows with their cells; a row opens the agent's details
  const tree = g.envs
    .map((e, ei) => {
      const [short] = envReport(ctx, all, e);
      const rows = g.agents
        .filter((a) => a.env === e.id)
        .map((a, ai) => {
          const key = `rt-${ei}-${ai}`;
          return `<li class="rt-agent${relAlert(a) ? " is-alert" : ""}${a.aux ? " is-aux" : ""}"><button type="button" class="rt-name" aria-expanded="false" aria-controls="${key}" data-toggle="${key}"><span class="rt-label">${esc(agentName(ctx, a))}</span><span class="rc-row">${relCells(ctx, v, a, false)}</span></button><div class="rt-detail" id="${key}" hidden><p class="rp-meta"><button type="button" class="linkish" data-goto="${esc(a.id)}" data-goto-env="${esc(a.env)}">${esc(t("See it under Machines"))}</button></p>${agentDetail(ctx, v, g, a, "flat")}</div></li>`;
        })
        .join("");
      return `<section class="rt-env${relAlert(e) ? " is-alert" : ""}${e.report && e.report.kind !== "report" ? " is-unreported" : ""}"><h3 class="rt-h"><span>${esc(e.label)}</span><span class="rt-sub">${esc(short)}</span></h3>${rows ? `<ul class="rt-agents">${rows}</ul>` : ""}</section>`;
    })
    .join("");

  const n = g.problemCells;
  const bar = `<div class="viewbar"><div class="lt-segmented" role="group" aria-label="${esc(t("Lines to show"))}"><button type="button" class="lt-seg-item" aria-pressed="true" data-rel-filter="all">${esc(t("All lines"))}</button><button type="button" class="lt-seg-item" aria-pressed="false" data-rel-filter="problems" data-problems="${n}"${n ? "" : " disabled"}>${esc(n ? t("Only the {n} with problems", { n }) : t("No problems"))}</button></div><p class="rel-legend"><span class="lg"><span class="lg-line s-missing" aria-hidden="true"></span>${esc(word(ctx, v, "missing"))}</span><span class="lg"><span class="lg-line s-drift" aria-hidden="true"></span>${esc(word(ctx, v, "drift"))}</span><span class="lg"><span class="lg-line" aria-hidden="true"></span>${esc(t("fine"))}</span><span class="lg"><span class="lg-box" aria-hidden="true"></span>${esc(t("not reported, state unknown"))}</span></p></div>`;
  return `<div class="rel" data-rel>${bar}<div class="rel-body"><div class="rel-main"><div class="rel-heads" aria-hidden="true">${heads}</div>${relGraphHtml(ctx, v, all, g, "all")}${relGraphHtml(ctx, v, all, pg, "problems")}</div><div class="rel-aside" role="region" aria-label="${esc(t("Details"))}" aria-live="polite">${panes}</div></div><div class="rel-tree"><div class="rt-head" aria-hidden="true"><span>${esc(t("Agent"))}</span><span class="rc-row">${kindHeads}</span></div>${tree}</div></div>`;
}

// ---- tab 5: plugins (plugins-view.ts) ----

/** The Plugins tab: its content from plugins-view.ts, with this page's marks and AI button, and the hidden fact. */
function pluginsTab(ctx: Ctx, all: AllStatus): string {
  const { t } = ctx;
  const data = readPluginsView(all.plugins_view);
  const fact = data.kind === "ok" ? pluginsFact(data.view, FACT_LIMIT, { workdir: ctx.workdir ?? "", manifest: ctx.noManifest ? null : (ctx.manifest ?? ""), ...(ctx.envPrefix ? { command_prefix: ctx.envPrefix } : {}) }) : "";
  const ai = data.kind !== "ok" ? "" : ctx.askBridge
    ? `<button type="button" class="lt-btn lt-btn-sm" data-ask="${PLUGINS_PRESET}" data-fact="fact-plugins" data-tip="${esc(t("Hands it to AI: it changes which plugins each environment gets, within what each environment allows, and shows you the change first"))}">${esc(t("Let AI adjust plugins"))}</button>`
    : `<button type="button" class="lt-btn lt-btn-sm" data-copy-from="fact-plugins" data-copy-lead="${esc(aiLead(ctx.lang, PLUGINS_PRESET, { ...whereOf(ctx), manifest: ctx.manifest }))}" data-ai-copy data-tip="${esc(t("Copies the request and the current plugin setup, written for an AI. Paste it to your AI and say what to change"))}">${esc(t("Copy plugin change request for AI"))}</button>`;
  const html = pluginsHtml({ t, lang: ctx.lang, esc, mark, when: (iso) => whenText(ctx, iso), aiButton: ai, aiCopy: (what, commands, o) => aiCopyButton(ctx, what, commands, o), aiFix: (problem, fix) => copyButton(ctx, aiFixText(ctx.lang, problem, fix, whereOf(ctx))) }, data);
  return html + (fact ? `<textarea id="fact-plugins" hidden readonly>${esc(fact)}</textarea>` : "");
}

// ---- the page body ----

/** A tab's name stays on one line ("To do 4" must not break in two on a phone): its spaces do not break. */
const tabName = (text: string) => text.replace(/ /g, "&nbsp;");
const TAB_TITLE: Record<string, string> = { inbox: "To do", machines: "Machines", matrix: "Overview", relation: "Map", plugins: "Plugins" };

/** Header with the headline, the three tabs, the footer and the glossary drawer, for a status with something in it. */
export function tabsHtml(ctx: Ctx, all: AllStatus, preview?: string): string {
  const { t } = ctx;
  const { todos, cards, groups, counts, glossary } = views(all);
  const agents = new Map<string, AgentRow>();
  for (const c of cards) for (const r of [...c.problem.map((p) => p.row), ...c.ok]) agents.set(`${c.env}|${r.id}`, r);
  const facts = new Map(todos.map((x, i) => [x.id, `fact-${i}`]));
  const v: View = { g: glossary, facts, todos, agents, multiEnv: cards.length > 1 };
  const panels: Record<string, string> = { inbox: inboxHtml(ctx, v, todos, all), machines: machinesHtml(ctx, v, cards, todos), matrix: matrixHtml(ctx, v, groups, counts), relation: relationHtml(ctx, v, all), plugins: pluginsTab(ctx, all) };
  const tabs = Object.keys(panels)
    .map((id, i) => `<button type="button" class="lt-tab" role="tab" id="tab-${id}" aria-controls="panel-${id}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}" data-tab="${id}">${tabName(esc(t(TAB_TITLE[id])))}${id === "inbox" && todos.length ? `&nbsp;<span class="inbox-count num">${todos.length}</span>` : ""}</button>`)
    .join("");
  const body = Object.entries(panels)
    .map(([id, html], i) => `<section class="tabpanel" role="tabpanel" id="panel-${id}" aria-labelledby="tab-${id}"${i === 0 ? "" : " hidden"}>${html}</section>`)
    .join("");
  const factAreas = todos.map((x, i) => `<textarea id="fact-${i}" hidden readonly>${esc(todoFact(x, FACT_LIMIT, factWhere(ctx)))}</textarea>`).join("");
  const batch = batchPayload(todos, ctx.lang === "zh" ? "要处理的问题" : "Problem to fix", FACT_LIMIT, factWhere(ctx));
  // the batch data goes with the batch button, which shows from two items (headline)
  const batchAreas = batch.facts.length >= 2
    ? `<script type="application/json" id="batch-facts">${JSON.stringify(batch.facts).replace(/</g, "\\u003c")}</script><textarea id="batch-notes" hidden readonly>${esc(batch.facts.map((f) => f.value).join("\n"))}</textarea>`
    : "";
  const counted = `data-count-inbox="${counts.inbox}" data-count-machines="${counts.machines}" data-count-matrix="${counts.matrix}"`;
  return `${topHtml(ctx, headline(ctx, todos))}<div class="views" ${counted}><nav class="lt-tabs" role="tablist" aria-label="${esc(t("Views"))}">${tabs}<span class="view-ink" aria-hidden="true"></span></nav>${body}</div>${footHtml(ctx, all, preview, true, todos)}${glossaryHtml(ctx, v)}${factAreas}${batchAreas}`;
}

// ---- whole-page states ----

/** What is wrong, what it means for the reader, and what to tell the AI. */
const FAILURE_HINT: Record<LoadFailure["kind"], string> = {
  missing: "The checking tool (bin/agent-env) was not found, so this page cannot check anything. Copy the sentence below to your AI; it finds out why the tool is missing and puts it back.",
  exit: "The check stopped with an error, so this page has nothing to show. Your machines are not affected. Copy the sentence below to your AI; it runs the check, reads the full error and fixes it.",
  timeout: "The check took too long and was stopped, so this page has nothing to show. Copy the sentence below to your AI; it runs the check and finds the step that hangs.",
  "not-json": "The check answered with something this page cannot read, so there is nothing to show. Copy the sentence below to your AI; it runs the check and fixes what it prints.",
  shape: "The check answered in a format this page does not know, usually because the tool and this page are different versions. Copy the sentence below to your AI; it brings both to the same version.",
  file: "This page was started with a preview file that cannot be read. Copy the sentence below to your AI; it fixes the file path or switches back to the live check.",
};
/** Whether the failure's `fix` is a command the AI should run (else it is a file or a setting to look at). */
const FAILURE_RUNS: Record<LoadFailure["kind"], boolean> = { missing: true, exit: true, timeout: true, "not-json": true, shape: false, file: false };
export const STATUS_COMMAND = "bin/agent-env status --json --all";
const STATUS_ASK = "check the AI setup on every machine, so the Environments panel has a status to show";

export function failureHtml(ctx: Ctx, f: LoadFailure): string {
  const { t } = ctx;
  const output = f.output.length ? `<h3>${esc(t("Error text (first lines, for your AI)"))}</h3><pre>${esc(f.output.join("\n"))}</pre>` : "";
  return `<div class="empty error" role="alert" data-failure="${esc(f.kind)}"><p><strong>${esc(t("Could not load the status"))}</strong></p>
<p class="fail-detail"><span>${esc(t("Details, for your AI"))}</span><code>${esc(f.command)}</code>: ${esc(f.message)}</p>${output}
<h3>${esc(t("What to do"))}</h3><p>${esc(t(FAILURE_HINT[f.kind]))}</p><div class="fix-line">${aiCopyButton(ctx, t("find out why the Environments panel cannot load its status, and fix it. The panel says: {message}", { message: `${f.command}: ${f.message}` }), FAILURE_RUNS[f.kind] ? [f.fix] : [], { quiet: false, careful: true })}<code>${esc(f.fix)}</code></div>${noManifestHtml(ctx, "p")}</div>`;
}

export function emptyHtml(ctx: Ctx): string {
  return `<div class="empty" role="status"><p><strong>${esc(ctx.t("No environments yet"))}</strong></p><p>${esc(ctx.t("No machine has been checked yet, so there is nothing to show. Copy the sentence below to your AI; it runs the first check on this machine. Then press Refresh."))}</p><div class="fix-line">${aiCopyButton(ctx, ctx.t(STATUS_ASK), [STATUS_COMMAND], { quiet: false })}<code>${STATUS_COMMAND}</code></div></div>`;
}

/** The body of the page for one load: header line, status sentence, three tabs; or the empty or error state. */
export function contentHtml(ctx: Ctx, r: LoadResult, preview?: string): string {
  if (!r.ok) return topHtml(ctx) + failureHtml(ctx, r);
  const all = r.status;
  if (!all.environments.length && !all.errors.length && !(all.issues ?? []).length) return topHtml(ctx) + emptyHtml(ctx) + footHtml(ctx, all, preview, false);
  return tabsHtml(ctx, all, preview);
}

/** Page script: loads the content, then wires tabs, toggles, copy buttons, refresh and the AI Task bridge. Runs in the browser. */
function pageScript(cfg: { base: string; top: string; strings: Record<string, string> }) {
  const S = cfg.strings;
  const content = document.getElementById("content")!;
  // the same two functions as model.ts readTab / writeTab, embedded below by layout()
  const readTabFn = (window as unknown as { __readTab: (s: unknown) => string }).__readTab;
  const writeTabFn = (window as unknown as { __writeTab: (s: unknown, t: string) => boolean }).__writeTab;
  const readRelFn = (window as unknown as { __readRel: (s: unknown) => string }).__readRel;
  const writeRelFn = (window as unknown as { __writeRel: (s: unknown, v: string) => boolean }).__writeRel;
  // model.ts scrollHold, embedded the same way
  const scrollHoldFn = (window as unknown as { __scrollHold: (y: number, windowHeight: number, pageHeight: number) => number }).__scrollHold;
  let store: Storage | null = null;
  try {
    store = window.localStorage;
  } catch {
    store = null;
  }

  function copy(text: string, btn: HTMLElement) {
    const done = (ok: boolean) => {
      const old = btn.dataset.label ?? btn.textContent ?? "";
      btn.dataset.label = old;
      btn.textContent = ok ? S.copied : S.copyFailed;
      btn.setAttribute("role", "status");
      setTimeout(() => (btn.textContent = old), ok ? 1600 : 4000);
    };
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch {
        ok = false;
      }
      ta.remove();
      done(ok);
    };
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(() => done(true), fallback);
    else fallback();
  }

  function toggle(key: string, force?: boolean) {
    const detail = document.getElementById(key);
    if (!detail) return;
    const open = force ?? detail.hidden;
    detail.hidden = !open;
    content.querySelectorAll<HTMLElement>(`[data-toggle="${key}"]`).forEach((b) => b.setAttribute("aria-expanded", String(open)));
  }

  // the underline of the selected view: one bar under the tab row, moved and stretched to the selected tab's label
  function placeInk(on = content.querySelector<HTMLElement>('.views > nav > [aria-selected="true"]'), jump = false) {
    const nav = content.querySelector<HTMLElement>(".views > nav");
    const ink = nav?.querySelector<HTMLElement>(".view-ink");
    if (!nav || !ink || !on) return;
    const box = on.getBoundingClientRect();
    // not laid out (a hidden frame): the shared per-tab underline stays until a later switch can measure
    if (!box.width) return;
    const pad = parseFloat(getComputedStyle(on).paddingLeft) || 0;
    // whole device pixels, so the ends are as sharp as a laid-out box
    const px = (v: number) => Math.round(v * devicePixelRatio) / devicePixelRatio;
    const left = px(box.left + pad);
    // the first placement is not a move (it does not travel in from the corner), nor is following a resized window
    const first = jump || !nav.classList.contains("has-ink");
    if (first) ink.style.transition = "none";
    ink.style.transform = `translateX(${left - nav.getBoundingClientRect().left}px) scaleX(${px(box.right - pad) - left})`;
    nav.classList.add("has-ink");
    if (first) {
      void ink.offsetWidth;
      ink.style.transition = "";
    }
  }

  /** Shows one view: the tab, its underline and the view change on the same frame; only the underline travels. */
  function showTab(id: string, focus = false) {
    const tabs = [...content.querySelectorAll<HTMLElement>("[data-tab]")];
    if (!tabs.length) return;
    if (!tabs.some((b) => b.dataset.tab === id)) id = "inbox";
    // everything is measured before anything changes, so the page is laid out once per switch, not twice (a tab is
    // as wide selected as not, so its label can be measured first)
    const y = window.scrollY;
    // a shorter view would pull a scrolled page up and take the tabs from under the pointer: keep the scroll
    // position (only while the tab row is on screen; once it has scrolled away there is nothing to hold still)
    const hold = y > 0 && (content.querySelector(".views > nav")?.getBoundingClientRect().bottom ?? 0) > 0;
    placeInk(tabs.find((b) => b.dataset.tab === id));
    for (const b of tabs) {
      const on = b.dataset.tab === id;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
    content.style.minHeight = "";
    for (const b of tabs) {
      const panel = document.getElementById(`panel-${b.dataset.tab}`);
      if (panel) panel.hidden = b.dataset.tab !== id;
    }
    // the height the content really has: scrollHeight would never say less than the window
    const more = hold ? scrollHoldFn(y, document.documentElement.clientHeight, document.documentElement.getBoundingClientRect().height) : 0;
    if (more) {
      content.style.minHeight = `${content.getBoundingClientRect().height + more}px`;
      window.scrollTo(window.scrollX, y);
    }
  }

  // relation graph: hovering previews a node's chains; a click pins them (and opens its pane) until clicked again or Esc
  let relPinned: string | null = null;
  let relLit: string | null = null;
  const relGraph = () => content.querySelector<HTMLElement>(".rel.only-problems .rel-graph[data-graph=problems]") ?? content.querySelector<HTMLElement>('.rel-graph[data-graph="all"]');
  function relLight(key: string | null) {
    if (key === relLit) return;
    relLit = key;
    content.querySelectorAll(".rel-graph .is-hot").forEach((x) => x.classList.remove("is-hot"));
    content.querySelectorAll(".rel-graph.is-focus").forEach((x) => x.classList.remove("is-focus"));
    const graph = relGraph();
    if (!key || !graph) return;
    graph.classList.add("is-focus");
    graph.querySelectorAll(`[data-on~="${CSS.escape(key)}"]`).forEach((x) => x.classList.add("is-hot"));
  }
  function relPin(key: string | null) {
    relPinned = key;
    relLit = null;
    relLight(key);
    content.querySelectorAll(".rel-graph .is-pinned").forEach((x) => x.classList.remove("is-pinned"));
    content.querySelectorAll<HTMLElement>(".rel-graph [data-focus]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.focus === key)));
    if (key) relGraph()?.querySelectorAll<HTMLElement>(`[data-focus="${CSS.escape(key)}"]`).forEach((b) => (b.closest(".rn") && !b.classList.contains("rc") ? b.closest(".rn")! : b).classList.add("is-pinned"));
    // an agent cell opens its agent's pane at that kind; the agent itself opens it at its first kind with a problem
    const cell = key?.startsWith("ak:") ? key.slice(3).split("|") : null;
    const pane = cell ? `a:${cell[0]}` : (key ?? "");
    let shown: HTMLElement | null = null;
    content.querySelectorAll<HTMLElement>(".rel-pane").forEach((p) => {
      p.hidden = p.dataset.pane !== pane;
      if (!p.hidden) shown = p;
    });
    const el = shown as HTMLElement | null;
    if (el?.dataset.defaultKind !== undefined) {
      const kind = cell ? cell[1] : el.dataset.defaultKind;
      el.querySelectorAll<HTMLElement>(".rp-kind").forEach((s) => (s.hidden = s.dataset.kind !== kind));
      el.querySelectorAll<HTMLElement>("[data-chip]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.chip === kind)));
    }
    // the pane sits under the graph on medium screens: bring it into view
    const aside = content.querySelector<HTMLElement>(".rel-aside");
    if (key && el && aside && getComputedStyle(aside).position !== "sticky") el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  const relNode = (t: EventTarget | null) => (t as HTMLElement | null)?.closest?.<HTMLElement>(".rel-graph [data-node]") ?? null;
  content.addEventListener("pointerover", (e) => {
    if (relPinned) return;
    const cell = (e.target as HTMLElement).closest?.<HTMLElement>(".rel-graph .rc[data-focus]");
    relLight(cell?.dataset.focus ?? relNode(e.target)?.dataset.node ?? null);
  });
  content.addEventListener("pointerleave", () => relPinned || relLight(null));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && relPinned) relPin(null);
  });

  function relFilter(v: string) {
    const rel = content.querySelector<HTMLElement>(".rel");
    if (!rel) return;
    const btn = rel.querySelector<HTMLButtonElement>('[data-rel-filter="problems"]');
    if (v === "problems" && btn?.disabled) v = "all";
    rel.classList.toggle("only-problems", v === "problems");
    rel.querySelectorAll<HTMLElement>("[data-rel-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.relFilter === v)));
    relPin(null);
  }

  /** From a node of the relation graph to the agent's row under "by machine" (its machine for an agent with no row). */
  function gotoAgent(id: string, env: string) {
    showTab("machines");
    writeTabFn(store, "machines");
    const panel = document.getElementById("panel-machines");
    if (!panel) return;
    const el = [...panel.querySelectorAll<HTMLElement>("[data-agent]")].find((x) => x.dataset.agent === id) ?? [...panel.querySelectorAll<HTMLElement>(".machine")].find((x) => x.dataset.env === env);
    if (!el) return;
    const list = el.closest<HTMLElement>(".ok-list");
    if (list?.hidden) toggle(list.id, true);
    el.setAttribute("tabindex", "-1");
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.focus({ preventScroll: true });
    el.classList.add("is-flash");
    setTimeout(() => el.classList.remove("is-flash"), 1600);
  }

  content.addEventListener("keydown", (e) => {
    const tab = (e.target as HTMLElement).closest<HTMLElement>("[data-tab]");
    if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const tabs = [...content.querySelectorAll<HTMLElement>("[data-tab]")];
    const i = tabs.indexOf(tab);
    const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    e.preventDefault();
    showTab(tabs[next].dataset.tab!, true);
    writeTabFn(store, tabs[next].dataset.tab!);
  });

  // one tooltip for every [data-tip]: words for icons and states, on hover or keyboard focus (shared .lt-tip look)
  const tip = document.createElement("div");
  tip.className = "lt-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  let tipFor: HTMLElement | null = null;
  function showTip(el: HTMLElement) {
    const text = el.dataset.tip;
    if (!text) return;
    tipFor = el;
    tip.textContent = text;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    const y = r.bottom + 8 + h > window.innerHeight - 8 ? r.top - h - 8 : r.bottom + 8;
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${Math.max(8, y)}px`;
    tip.classList.add("visible");
  }
  function hideTip() {
    tipFor = null;
    tip.classList.remove("visible");
    tip.hidden = true;
  }
  document.addEventListener("pointerover", (e) => {
    const el = (e.target as HTMLElement).closest?.<HTMLElement>("[data-tip]") ?? null;
    if (el && el !== tipFor) showTip(el);
    else if (!el && tipFor) hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const el = (e.target as HTMLElement).closest?.<HTMLElement>("[data-tip]") ?? null;
    if (el && (e.target as HTMLElement).matches(":focus-visible")) showTip(el);
    else hideTip();
  });
  document.addEventListener("focusout", hideTip);
  document.addEventListener("keydown", (e) => e.key === "Escape" && hideTip());
  window.addEventListener("scroll", hideTip, true);
  // the room showTab keeps under a short view goes once the page is back at its top
  window.addEventListener("scroll", () => {
    if (!window.scrollY) content.style.minHeight = "";
  });
  // a narrower window can wrap a tab's label: the underline follows its tab
  window.addEventListener("resize", () => placeInk(undefined, true));

  content.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    // a click on the drawer's backdrop closes it
    if (target instanceof HTMLDialogElement) return target.close();
    const el = target.closest<HTMLElement>("[data-tab],[data-copy],[data-copy-from],[data-ask],[data-ask-all],[data-toggle],[data-refresh],[data-filter],[data-gloss],[data-close],[data-rel-filter],[data-goto],[data-focus]");
    if (!el && target.closest(".rel-graph") && relPinned) return relPin(null);
    if (!el) return;
    hideTip();
    if (el.dataset.gloss !== undefined) return (document.getElementById("gloss") as HTMLDialogElement | null)?.showModal();
    if (el.dataset.close !== undefined) return el.closest("dialog")?.close();
    if (el.dataset.tab) {
      showTab(el.dataset.tab);
      writeTabFn(store, el.dataset.tab);
      return;
    }
    if (el.dataset.toggle) return toggle(el.dataset.toggle);
    if (el.dataset.relFilter) {
      relFilter(el.dataset.relFilter);
      writeRelFn(store, el.dataset.relFilter);
      return;
    }
    if (el.dataset.goto) return gotoAgent(el.dataset.goto, el.dataset.gotoEnv ?? "");
    if (el.dataset.focus !== undefined) {
      // pins from the graph; a pane or default-list entry pins the same key in the graph
      if (!content.querySelector(".rel")?.contains(el)) return;
      // a kind block in the pane switches the kind and keeps it pinned; anything else toggles
      if (el.dataset.keep !== undefined) return relPin(el.dataset.focus);
      return relPin(relPinned === el.dataset.focus ? null : el.dataset.focus);
    }
    if (el.dataset.refresh !== undefined) return void load();
    if (el.dataset.copy !== undefined) return copy(el.dataset.copy, el);
    if (el.dataset.copyFrom) {
      const value = (document.getElementById(el.dataset.copyFrom) as HTMLTextAreaElement | null)?.value ?? "";
      return copy(el.dataset.copyLead ? el.dataset.copyLead + "\n" + value : value, el);
    }
    if (el.dataset.filter) {
      const table = content.querySelector(".matrix");
      content.querySelectorAll<HTMLElement>("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b === el)));
      table?.classList.toggle("only-problems", el.dataset.filter === "problems");
      return;
    }
    if (el.dataset.askAll) {
      let facts: unknown = [];
      try {
        facts = JSON.parse(document.getElementById("batch-facts")?.textContent ?? "[]");
      } catch {
        facts = [];
      }
      window.parent.postMessage({ type: "local-tools:ask", version: 1, preset: el.dataset.askAll, text: "", facts }, location.origin);
      return;
    }
    if (el.dataset.ask) {
      const value = (document.getElementById(el.dataset.fact ?? "") as HTMLTextAreaElement | null)?.value ?? "";
      window.parent.postMessage({ type: "local-tools:ask", version: 1, preset: el.dataset.ask, text: "", facts: [{ label: S.factLabel, value }] }, location.origin);
    }
  });

  function errorBox(err: unknown): HTMLElement {
    const box = document.createElement("div");
    box.className = "empty error";
    box.setAttribute("role", "alert");
    box.dataset.loadError = "";
    const p = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = S.loadFailed;
    p.append(strong);
    const pre = document.createElement("pre");
    pre.textContent = String((err as Error)?.message ?? err);
    box.append(p, pre);
    return box;
  }

  // first load: a loading box; a refresh keeps the page on screen and only the refresh button says it is busy
  async function load() {
    const refresh = !!content.querySelector(".views, .empty:not(.loading)");
    const btn = content.querySelector<HTMLButtonElement>("[data-refresh]");
    if (refresh && btn?.disabled) return;
    const open = [...content.querySelectorAll<HTMLElement>(".todo-more:not([hidden])")].map((x) => x.id);
    const scroll = { x: window.scrollX, y: window.scrollY };
    content.setAttribute("aria-busy", "true");
    if (refresh && btn) {
      btn.disabled = true;
      btn.textContent = S.refreshing;
    } else content.innerHTML = `${cfg.top}<div class="empty loading" role="status"><span class="spinner" aria-hidden="true"></span><span><strong>${S.loading}</strong> ${S.running}</span></div>`;
    try {
      const res = await fetch(`${cfg.base}/fragment`, { headers: { "x-env-fragment": "1" }, cache: "no-store" });
      const html = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${html.slice(0, 200)}`);
      content.innerHTML = html;
      showTab(readTabFn(store));
      relLit = null;
      relFilter(readRelFn(store));
      for (const id of open) toggle(id, true);
      if (refresh) window.scrollTo(scroll.x, scroll.y);
    } catch (err) {
      if (refresh) {
        content.querySelector("[data-load-error]")?.remove();
        const head = content.querySelector("header.top");
        if (head) head.after(errorBox(err));
        else content.prepend(errorBox(err));
        if (btn) {
          btn.disabled = false;
          btn.textContent = S.refresh;
        }
      } else {
        content.innerHTML = cfg.top;
        content.append(errorBox(err));
      }
    } finally {
      content.removeAttribute("aria-busy");
    }
  }
  load();
}

export function layout(o: { ctx: Ctx; nonce: string; scheme: string; headerBar: boolean }): string {
  const ctx = { ...o.ctx, headerBar: o.headerBar };
  const { t, lang, base } = ctx;
  const strings = {
    loading: t("Loading"),
    refresh: t("Refresh"),
    refreshing: t("Refreshing…"),
    running: t("Checking every machine…"),
    loadFailed: t("Could not load the status"),
    copied: t("Copied"),
    copyFailed: t("Copy failed: select the text and copy it yourself"),
    factLabel: lang === "zh" ? "要处理的问题" : "Problem to fix",
  };
  const cfg = { base, top: topHtml(ctx), strings };
  const helpers = `window.__readTab=${readTab.toString()};window.__writeTab=${writeTab.toString()};window.__readRel=${readRelFilter.toString()};window.__writeRel=${writeRelFilter.toString()};window.__scrollHold=${scrollHold.toString()};`;
  return `<!DOCTYPE html>
<html lang="${lang === "zh" ? "zh-CN" : "en"}" data-lt-theme="${esc(o.scheme)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(t("Environments"))}</title>
<link rel="stylesheet" href="/_shared/local-tools.css">
<style>${CSS}</style>
</head>
<body>
<main class="wrap"><div id="content">${topHtml(ctx)}<noscript><a href="${esc(base)}/fragment">${esc(t("Show the status without scripts"))}</a></noscript></div></main>
<script nonce="${esc(o.nonce)}">${helpers}(${pageScript.toString()})(${JSON.stringify(cfg).replace(/</g, "\\u003c")});</script>
</body>
</html>`;
}

/** The fragment wrapped as a page of its own, for the no-script link: every tab and step shown, one after another. */
export function fragmentPage(ctx: Ctx, body: string, scheme: string): string {
  return `<!DOCTYPE html><html lang="${ctx.lang === "zh" ? "zh-CN" : "en"}" data-lt-theme="${esc(scheme)}"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(ctx.t("Environments"))}</title><link rel="stylesheet" href="/_shared/local-tools.css"><style>${CSS} .tabpanel[hidden], .todo-more[hidden] { display: block !important; } .lt-tabs, [data-gloss], [data-close] { display: none; } dialog.drawer { display: block; position: static; width: auto; height: auto; margin-top: 32px; border: 0; }</style></head><body><main class="wrap">${body}</main></body></html>`;
}
