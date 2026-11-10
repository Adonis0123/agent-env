// Data side of the Environments panel: the `agent-env status --json --all` shape (docs/status-schema.md, schema 1),
// its checks, and what the page shows (freshness per environment, the things to do, one summary row per agent).
// The things to do and the glossary come from the engine (engine/issues.py); this file never infers who acts or how
// a problem is worded. No HTML here; page.ts renders.
import { BUILTIN_GLOSSARY, type GlossaryWord } from "./i18n.ts";

export const STATES = ["synced", "customized", "drift", "missing", "unknown"] as const;
export type State = (typeof STATES)[number];
export const KINDS = ["rules", "skills", "hooks", "mcp"] as const;
const DAY_MS = 86_400_000;

export interface HabitCell {
  kind: string;
  name: string;
  state: State;
  layer?: string;
  detail?: string;
  fix?: string;
  /** --all only: the source it comes from (a key of `relations.sources`), null for a fact of that machine itself. */
  origin?: string | null;
}

export interface TargetStatus {
  id: string;
  host: string;
  env: string;
  roles?: string[];
  verified?: boolean;
  host_version?: { recorded?: string | null; current?: string | null; changed?: boolean };
  habits: HabitCell[];
}

/** One behaviour spot-check result (docs/status-schema.md `eval.results`). */
export interface EvalResult {
  target: string;
  case: string;
  state: "pass" | "fail" | "err" | "skip" | string;
  runs?: number;
  passed?: number;
  detail?: string;
}

export interface EnvStatus {
  schema: number;
  env: string;
  source?: "live" | "branch" | string;
  /** a cloud-hosted environment checked live by another machine (it only reads that machine's state) */
  checked_from?: string;
  generated_at: string;
  engine_rev?: string;
  summary?: Partial<Record<State, number>>;
  check?: { pass?: number; warn?: number; fail?: number };
  targets: TargetStatus[];
  eval?: { ran_at: string | null; results: EvalResult[] };
  /** a remote environment whose check failed as a whole */
  error?: { message?: string; fix?: string };
}

export interface EnvError {
  env?: string;
  message: string;
  fix?: string;
}

export interface AllStatus {
  schema: number;
  generated_at: string;
  environments: EnvStatus[];
  errors: EnvError[];
  /** v5: the engine's list of things to do (docs/status-schema.md「待办」); absent in older reports. */
  issues?: Todo[];
  /** v5: one definition of every state, kind, actor and spot-check word; absent in older reports. */
  glossary?: Glossary;
  /** --all, read-only: where habits come from and which agents each declared environment has; absent in older reports. */
  relations?: Relations;
  /** --all, read-only: the Plugins tab's data (docs/status-schema.md「插件视图」), read by plugins-view.ts; absent in older reports. */
  plugins_view?: unknown;
}

export interface Relations {
  /** source key → path, in the manifest's order ("~/" paths) */
  sources: Record<string, string>;
  /** environment → source key → path, for a source whose path differs by machine (each machine's portal list) */
  env_sources: Record<string, Record<string, string>>;
  env_names: Record<string, string>;
  /** every agent the manifest declares, by environment, also on machines that never reported */
  targets: { id: string; env: string; host: string; name: string; roles: string[] }[];
}

export class StatusShapeError extends Error {}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Checks the parts the page relies on; anything else is passed through as given. */
export function parseAllStatus(raw: unknown): AllStatus {
  if (!isObj(raw)) throw new StatusShapeError("the output is not a JSON object");
  if (raw.schema !== 1) throw new StatusShapeError(`unknown schema ${JSON.stringify(raw.schema)}; this panel reads schema 1`);
  if (!Array.isArray(raw.environments)) throw new StatusShapeError('"environments" is missing or not an array');
  const environments = raw.environments.map((e, i): EnvStatus => {
    if (!isObj(e)) throw new StatusShapeError(`environments[${i}] is not an object`);
    if (typeof e.env !== "string" || !e.env) throw new StatusShapeError(`environments[${i}].env is missing`);
    const targets = Array.isArray(e.targets) ? e.targets : [];
    return {
      ...(e as unknown as EnvStatus),
      targets: targets.filter(isObj).map((t) => ({
        ...(t as unknown as TargetStatus),
        habits: Array.isArray(t.habits) ? (t.habits.filter(isObj) as unknown as HabitCell[]).map(normalizeCell) : [],
      })),
    };
  });
  const errors = Array.isArray(raw.errors) ? (raw.errors.filter(isObj) as unknown as EnvError[]) : [];
  const out: AllStatus = { schema: 1, generated_at: String(raw.generated_at ?? ""), environments, errors };
  if (Array.isArray(raw.issues)) out.issues = raw.issues.filter(isObj).map(normalizeTodo);
  if (isObj(raw.glossary)) out.glossary = normalizeGlossary(raw.glossary);
  if (isObj(raw.relations)) out.relations = normalizeRelations(raw.relations);
  if (isObj(raw.plugins_view)) out.plugins_view = raw.plugins_view;
  return out;
}

function normalizeCell(c: HabitCell): HabitCell {
  const state = (STATES as readonly string[]).includes(c.state) ? c.state : "unknown";
  return { ...c, kind: String(c.kind ?? ""), name: String(c.name ?? ""), state };
}

/** Whole days since `iso`, or null when the time cannot be read. */
export function ageDays(iso: string, now: number): number | null {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((now - t) / DAY_MS));
}

/** One habit kind on one agent, rolled up into one table cell.
 *  "drift": something drifted or is missing; "unknown": nothing broken but some of it cannot be checked;
 *  "customized": an expected customisation; "synced": all match; "none": nothing of this kind is registered. */
export type SummaryState = "drift" | "unknown" | "customized" | "synced" | "none";

export interface KindSummary {
  kind: string;
  state: SummaryState;
  total: number;
  counts: Record<State, number>;
  /** drift + missing */
  bad: number;
}

export function kindSummary(habits: HabitCell[], kind: string): KindSummary {
  const counts = Object.fromEntries(STATES.map((s) => [s, 0])) as Record<State, number>;
  let total = 0;
  for (const h of habits) {
    if (h.kind !== kind) continue;
    counts[h.state]++;
    total++;
  }
  const bad = counts.drift + counts.missing;
  const state: SummaryState = bad ? "drift" : counts.unknown ? "unknown" : counts.customized ? "customized" : total ? "synced" : "none";
  return { kind, state, total, counts, bad };
}

/** Behaviour spot-checks of one agent: "fail" beats "err" (host crashed, UNVERIFIED) beats "pass";
 *  "skip" when every row skipped (reason in skipReason), "none" when nothing ran. */
export type EvalSummaryState = "fail" | "err" | "pass" | "skip" | "none";

export interface EvalSummary {
  state: EvalSummaryState;
  passed: number;
  /** cases that ran (skips left out) */
  total: number;
  failed: number;
  errored: number;
  skipReason: string;
}

export function evalSummary(rows: EvalRow[]): EvalSummary {
  const ran = rows.filter((r) => r.outcome !== "skip");
  const passed = ran.filter((r) => r.outcome === "pass").length;
  const failed = ran.filter((r) => r.outcome === "fail").length;
  // an unreadable state is not a pass either; it counts with the errors
  const errored = ran.length - passed - failed;
  const skipReason = rows.filter((r) => r.outcome === "skip").map((r) => r.detail).filter(Boolean).join("; ");
  const state: EvalSummaryState = failed ? "fail" : errored ? "err" : ran.length ? "pass" : rows.length ? "skip" : "none";
  return { state, passed, total: ran.length, failed, errored, skipReason };
}

export interface VersionSummary {
  /** "changed": the host was upgraded since the classification table was checked. */
  state: "changed" | "same" | "none";
  recorded: string;
  current: string;
}

export function versionSummary(t: Pick<TargetStatus, "host_version">): VersionSummary {
  const hv = t.host_version;
  const recorded = String(hv?.recorded ?? "");
  const current = String(hv?.current ?? "");
  const state = hv?.changed === true ? "changed" : recorded || current ? "same" : "none";
  return { state, recorded, current };
}

const HABIT_RANK: Record<State, number> = { missing: 0, drift: 1, unknown: 2, customized: 3, synced: 4 };

/** One agent (target) as one table row: a summary per habit kind, spot-checks, host version, and the detail list. */
export interface AgentRow {
  id: string;
  env: string;
  /** "hermes" for "mac/hermes". */
  short: string;
  host: string;
  roles: string[];
  unverified: boolean;
  kinds: KindSummary[];
  eval: EvalSummary;
  evalRows: EvalRow[];
  version: VersionSummary;
  /** Every habit of this agent, missing and drifted first. */
  habits: HabitCell[];
  /** drift + missing habits, failed and errored spot-checks, and an upgraded host. */
  issues: number;
}

const shortId = (id: string, env: string) => (id.startsWith(`${env}/`) ? id.slice(env.length + 1) : id);

/** Agents of one environment in report order; a spot-checked target the report does not list gets a row of its own. */
export function agentRows(e: EnvStatus): AgentRow[] {
  const evals = evalRows(e);
  const row = (t: TargetStatus): AgentRow => {
    const id = String(t.id ?? t.host ?? "?");
    const habits = [...t.habits].sort((a, b) => HABIT_RANK[a.state] - HABIT_RANK[b.state]);
    const kinds = KINDS.map((k) => kindSummary(t.habits, k));
    const mine = evals.filter((r) => r.target === id);
    const ev = evalSummary(mine);
    const version = versionSummary(t);
    const bad = t.habits.filter((h) => h.state === "drift" || h.state === "missing").length;
    return {
      id,
      env: e.env,
      short: shortId(id, e.env),
      host: String(t.host ?? shortId(id, e.env)),
      roles: Array.isArray(t.roles) ? t.roles.map(String) : [],
      unverified: t.verified === false,
      kinds,
      eval: ev,
      evalRows: mine,
      version,
      habits,
      issues: bad + ev.failed + ev.errored + (version.state === "changed" ? 1 : 0),
    };
  };
  const rows = e.targets.map(row);
  const known = new Set(rows.map((r) => r.id));
  for (const id of new Set(evals.map((r) => r.target)))
    if (id && !known.has(id)) rows.push(row({ id, host: shortId(id, e.env), env: e.env, habits: [] }));
  return rows;
}

export const EVAL_STATES = ["pass", "fail", "err", "skip"] as const;
export type EvalState = (typeof EVAL_STATES)[number] | "unknown";

export interface EvalRow {
  name: string;
  /** the case's plain-language name (`case_zh`), "" when the engine has none */
  label: string;
  target: string;
  /** "err" means the host timed out or crashed (UNVERIFIED); an unlisted state reads as "unknown". */
  outcome: EvalState;
  /** The state as reported, for an unknown one. */
  outcomeText: string;
  runs: number | null;
  passed: number | null;
  detail: string;
}

const count = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);

/** Behaviour spot-check results (P5): `{ target, case, state, runs, passed, detail }` per docs/status-schema.md. */
export function evalRows(e: EnvStatus): EvalRow[] {
  const results = Array.isArray(e.eval?.results) ? e.eval!.results : [];
  return (results as unknown[]).filter(isObj).map((r) => {
    const state = String(r.state ?? "");
    return {
      name: String(r.case ?? "—"),
      label: String(r.case_zh ?? ""),
      target: String(r.target ?? ""),
      outcome: (EVAL_STATES as readonly string[]).includes(state) ? (state as EvalState) : "unknown",
      outcomeText: state,
      runs: count(r.runs),
      passed: count(r.passed),
      detail: evalDetail(String(r.detail ?? ""), state),
    };
  });
}

/** Display form of a spot-check detail: drops the runner's state word that repeats the row's label
 *  ("#1 FAIL FAIL ..." on a fail row) and separators left dangling at either end. The engine output is unchanged. */
export function evalDetail(detail: string, state: string): string {
  const word = state.toUpperCase();
  const cleaned = /^(PASS|FAIL|ERR|SKIP)$/.test(word) ? detail.replace(new RegExp(`(^|\\s)${word}(?=\\s|$)`, "g"), "$1") : detail;
  return cleaned
    .replace(/\s{2,}/g, " ")
    .replace(/^[\s|·;；,，]+|[\s|·;；,，]+$/g, "");
}

// ---- counts, dots and freshness ----

export interface Overview {
  /** things to do (todoList): the one count the page shows for problems */
  issues: number;
  /** habit cells across the readable environments */
  synced: number;
  customized: number;
  /** newest report time, "" when none can be read */
  lastCheck: string;
}

export function overview(all: AllStatus, issues: Todo[] = todoList(all)): Overview {
  let synced = 0;
  let customized = 0;
  let lastCheck = "";
  let lastMs = -Infinity;
  for (const e of all.environments) {
    for (const t of e.targets)
      for (const h of t.habits) {
        if (h.state === "synced") synced++;
        else if (h.state === "customized") customized++;
      }
    const ms = Date.parse(e.generated_at);
    if (!Number.isNaN(ms) && ms > lastMs) [lastCheck, lastMs] = [e.generated_at, ms];
  }
  return { issues: issues.length, synced, customized, lastCheck };
}

/** Agents of every readable environment: those with something in the "to fix" list first, then the healthy ones. */
export function agentGroups(all: AllStatus): { problem: AgentRow[]; ok: AgentRow[] } {
  const rows = all.environments.flatMap((e) => agentRows(e));
  return { problem: rows.filter((r) => r.issues > 0), ok: rows.filter((r) => r.issues === 0) };
}

/** One dot per habit kind; "none" leaves its slot empty. */
export type DotState = "synced" | "customized" | "unknown" | "drift" | "missing" | "none";

export interface KindDot {
  kind: string;
  state: DotState;
}

export function kindDots(r: Pick<AgentRow, "kinds">): KindDot[] {
  return r.kinds.map((k) => ({ kind: k.kind, state: k.state === "drift" ? (k.counts.missing ? "missing" : "drift") : k.state }));
}

/** The dots as text for narrow screens: kinds in order (synced or customized) out of the kinds registered. */
export function dotsScore(dots: KindDot[]): { ok: number; total: number } {
  const total = dots.filter((d) => d.state !== "none").length;
  const ok = dots.filter((d) => d.state === "synced" || d.state === "customized").length;
  return { ok, total };
}

/** One environment in the header line: how fresh its report is, or why there is none. */
export interface Freshness {
  env: string;
  /** "report": a readable report; "unreported": no status branch yet; "unreadable": a report that cannot be read */
  kind: "report" | "unreported" | "unreadable";
  ageDays: number | null;
  /** the engine lists an `env_stale` issue for it (engine/issues.py STALE_DAYS decides); no local threshold */
  stale: boolean;
}

/** An error saying the environment's status branch does not exist (it never reported), as the engine words it. */
export function neverReported(e: EnvError): boolean {
  return /^(读不到|No)\s*status\/\S+\s*(分支|branch)/.test(String(e.message ?? "").trim());
}

/** The engine says this environment's report is too old: an `env_stale` issue on it. */
export const isStale = (issues: Todo[] | undefined, env: string): boolean => (issues ?? []).some((t) => t.category === "env_stale" && t.env === env);

export function freshness(all: Pick<AllStatus, "environments" | "errors" | "issues">, now: number): Freshness[] {
  const out: Freshness[] = all.environments.map((e) => ({ env: e.env, kind: "report", ageDays: ageDays(e.generated_at, now), stale: isStale(all.issues, e.env) }));
  const seen = new Set(out.map((f) => f.env));
  for (const e of all.errors) {
    const env = String(e.env ?? "");
    if (!env || seen.has(env)) continue;
    seen.add(env);
    out.push({ env, kind: neverReported(e) ? "unreported" : "unreadable", ageDays: null, stale: false });
  }
  return out;
}

// ---- v5: three tabs (to do / by machine / overview) over one list of things to do ----
// The engine writes `issues` and `glossary` (docs/status-schema.md「待办」「术语表」); the page renders titles, actors,
// counts and the legend from them only. A report without `issues` gets one "too old, report again" item per environment;
// a report without `glossary` gets the panel's one copy (BUILTIN_GLOSSARY in i18n.ts).

/** Who handles a thing to do, in the order the "to do" tab groups them. */
export const ACTORS = ["ai", "user", "other_env", "notice"] as const;
export type Actor = (typeof ACTORS)[number];
export const AI_TASKS = ["fix", "prepare", "logs"] as const;
export type AiTask = (typeof AI_TASKS)[number];

export interface TodoStep {
  /** "other_env": the user, on the other machine */
  who: "ai" | "user" | "other_env";
  text_zh: string;
  text_en?: string;
  /** one command, copied on its own; for a write step the dry-run form */
  command: string | null;
  /** a write step: show the dry run, the user confirms before anything changes */
  confirm: boolean;
}

/** One thing to do: an entry of the engine's `issues`, or the "report too old" item for a report without them. */
export interface Todo {
  id: string;
  env: string;
  /** agent id ("mac/codex") or, for a whole environment, the environment name */
  target: string;
  category: string;
  state: string;
  actor: Actor;
  title_zh: string;
  title_en?: string;
  impact_zh: string;
  impact_en?: string;
  steps: TodoStep[];
  ai_task: AiTask | null;
  ref: { kind: string; name: string; case: string | null };
  /** "engine": from `issues`; "outdated": the report has no `issues`, so the panel asks for a new one */
  source?: "engine" | "outdated";
}

export type GlossaryEntry = GlossaryWord;
export type Glossary = Record<string, GlossaryEntry>;

const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));

function normalizeTodo(r: Record<string, unknown>): Todo {
  const actor = (ACTORS as readonly string[]).includes(str(r.actor)) ? (r.actor as Actor) : "notice";
  const ai = (AI_TASKS as readonly string[]).includes(str(r.ai_task)) ? (r.ai_task as AiTask) : null;
  const steps = (Array.isArray(r.steps) ? r.steps : []).filter(isObj).map(
    (x): TodoStep => ({
      who: x.who === "ai" ? "ai" : x.who === "other_env" ? "other_env" : "user",
      text_zh: str(x.text_zh),
      ...(x.text_en ? { text_en: str(x.text_en) } : {}),
      command: x.command ? str(x.command) : null,
      confirm: x.confirm === true,
    }),
  );
  const ref = isObj(r.ref) ? r.ref : {};
  const out: Todo = {
    id: str(r.id),
    env: str(r.env) || str(r.target).split("/")[0] || "?",
    target: str(r.target || r.env) || "?",
    category: str(r.category),
    state: str(r.state),
    actor,
    title_zh: str(r.title_zh || r.title_en || r.id),
    impact_zh: str(r.impact_zh),
    // the engine already splits its writes; this keeps an older report's copy button off a write all the same
    steps: guardWrites(steps),
    ai_task: ai,
    ref: { kind: str(ref.kind), name: str(ref.name), case: ref.case ? str(ref.case) : null },
    source: "engine",
  };
  if (r.title_en) out.title_en = str(r.title_en);
  if (r.impact_en) out.impact_en = str(r.impact_en);
  return out;
}

function normalizeGlossary(g: Record<string, unknown>): Glossary {
  const out: Glossary = {};
  for (const [k, v] of Object.entries(g)) {
    if (!isObj(v) || !v.label_zh) continue;
    out[k] = { label_zh: str(v.label_zh), explain_zh: str(v.explain_zh) };
    if (v.label_en) out[k].label_en = str(v.label_en);
    if (v.explain_en) out[k].explain_en = str(v.explain_en);
  }
  return out;
}

/** Display names of hosts; an unlisted host shows as reported. */
export const HOST_NAMES: Record<string, string> = {
  "claude-code": "Claude Code",
  claude: "Claude Code",
  codex: "Codex",
  grok: "Grok",
  "grok-bot": "Grok Bot",
  cursor: "Cursor",
  hermes: "Hermes",
  workbuddy: "WorkBuddy",
  pi: "pi",
  sideby: "sideby",
};
export const hostLabel = (host: string) => HOST_NAMES[host] ?? host;

/** The glossary the page shows: the report's entries over the built-in ones. */
export function glossaryOf(all: Pick<AllStatus, "glossary">): Glossary {
  return { ...BUILTIN_GLOSSARY, ...(all.glossary ?? {}) };
}

/** One entry by key; also finds "state.drift" / "actor:ai" style keys. */
export function gloss(g: Glossary, key: string): GlossaryEntry | null {
  if (g[key]) return g[key];
  const hit = Object.keys(g).find((k) => k.endsWith(`.${key}`) || k.endsWith(`:${key}`) || k.endsWith(`/${key}`));
  return hit ? g[hit] : null;
}

/** `bin/agent-env status` with --commit or --push writes (a status branch, maybe pushed); without them it only checks. */
const STATUS_WRITE = /^bin\/agent-env status\b.*\s--(commit|push)\b/;

/** A copyable step never holds a status write: it becomes the read-only check (copyable), then a confirm step that
 *  names the write in its text and has no command, as the engine does for its own write steps. */
export function guardWrites(steps: TodoStep[]): TodoStep[] {
  return steps.flatMap((s): TodoStep[] => {
    if (!s.command || !STATUS_WRITE.test(s.command)) return [s];
    const write = s.command;
    const push = /\s--push\b/.test(write);
    const dry = write.replace(/\s--(commit|push)\b/g, "");
    return [
      { who: s.who, text_zh: "先跑一遍检查看结果（只读，不提交也不推送）", text_en: "First run the check and look at the result (read-only: nothing committed or pushed)", command: dry, confirm: false },
      push
        ? { who: s.who, text_zh: `看过结果后确认，再运行 ${write}（会提交并推送状态分支）`, text_en: `Once you have looked, confirm and run ${write} (commits and pushes the status branch)`, command: null, confirm: true }
        : { who: s.who, text_zh: `看过结果后确认，再运行 ${write}（会提交到本地状态分支）`, text_en: `Once you have looked, confirm and run ${write} (commits to the local status branch)`, command: null, confirm: true },
    ];
  });
}

/** The one thing to do for a report without `issues`. The engine that runs `status --json --all` on this machine rebuilds
 *  `issues` for every environment from the branches and errors it reads (engine/agent_env.py all_status), so a report
 *  without them means this machine's engine is too old (or a hand-fed old file). The fix is here, not on the other
 *  machines, so the panel neither guesses per-environment steps nor where a remote environment is checked from. */
export function outdatedTodos(all: Pick<AllStatus, "environments">): Todo[] {
  if (!all.environments.length) return []; // nothing reported at all: nothing to update for
  const env = all.environments.find((e) => e.source === "live")?.env ?? all.environments[0].env;
  return [
    {
      id: "report_outdated",
      env,
      target: env,
      category: "report_outdated",
      state: "outdated",
      actor: "user",
      title_zh: "状态报告是旧版引擎生成的，列不出待办",
      title_en: "The report comes from an old engine and lists nothing to do",
      impact_zh: "报告里没有待办清单，面板不会自己猜；更新本机的 agent-env 后才能看到各环境的待办",
      impact_en: "The report has no to-do list and the panel does not guess one; update agent-env on this machine to see each environment's",
      steps: [
        { who: "user", text_zh: "更新本机的 agent-env 仓库（拉取最新代码），再点刷新", text_en: "Update the agent-env repository on this machine (pull the latest code), then refresh", command: null, confirm: false },
        { who: "ai", text_zh: "确认新报告带待办清单", text_en: "Check that the new report has a to-do list", command: "bin/agent-env status --json --all --fetch", confirm: false },
      ],
      ai_task: null,
      ref: { kind: "env", name: env || null, case: null },
      source: "outdated",
    },
  ];
}

/** Whether a fix string is itself one shell command (not a sentence that mentions one). */
function isCommand(fix: string | undefined): boolean {
  if (!fix) return false;
  const s = fix.trim();
  if (!s || s.includes("\n")) return false;
  if (/[\u3000-\u9fff\uff00-\uffef]/.test(s)) return false;
  if (/[.!?;:,]$/.test(s)) return false;
  // First word is a path (bin/agent-env, ~/x) or a lowercase program name (git, trash, agent-env); sentences start capitalised.
  const first = s.split(/\s+/)[0];
  return /^[\w.~$/-]+$/.test(first) && (first.includes("/") || /^[a-z]/.test(first));
}

/** The `bin/agent-env ...` commands an engine-written fix sentence names, or the fix itself when it is one command. */
function fixCommands(fix: string | undefined): string[] {
  if (!fix) return [];
  if (isCommand(fix)) return [fix.trim()];
  const out: string[] = [];
  for (const m of fix.matchAll(/bin\/agent-env(?=\s|$)/g)) {
    const words = [m[0]];
    let flags = false;
    for (const raw of fix.slice(m.index! + m[0].length).split(/[ \t]+/).slice(1)) {
      const w = raw.split(/[\u3000-\u9fff\uff00-\uffef；;，,（）()]/)[0].replace(/[.,;:!?]+$/, "");
      if (!w) break;
      if (w.startsWith("-")) flags = true;
      else if (flags ? !/[/.~=:0-9-]/.test(w) : !/^[a-z][\w-]*$/.test(w)) break;
      words.push(w);
      if (w !== raw) break;
    }
    out.push(words.join(" "));
  }
  return [...new Set(out)];
}

/** A habit's fix for the relation pane: the commands it can copy (a status write becomes its read-only check, a
 *  confirming --yes never shows), the sentence when there is no command, and whether a step waits for the user.
 *  It only splits the engine's own fix text into commands; it does not guess actors or wording. */
export function habitFix(fix: string): { commands: string[]; text: string; confirm: boolean } {
  const asked = /--yes|--dry-run|确认|confirm/i.test(fix ?? "");
  const cmds = fixCommands(fix);
  if (!cmds.length) return { commands: [], text: fix ?? "", confirm: asked };
  const steps = guardWrites(cmds.map((command): TodoStep => ({ who: "user", text_zh: "", text_en: "", command, confirm: false })));
  const commands = steps.map((s) => s.command).filter((c): c is string => !!c && !/\s--yes\b/.test(c));
  return { commands, text: commands.length ? "" : fix, confirm: asked || steps.some((s) => s.confirm) };
}

/** The things to do: the engine's `issues`, or for a report without them the one "update the engine" item. */
export function todoList(all: Pick<AllStatus, "environments" | "issues">): Todo[] {
  return Array.isArray(all.issues) ? all.issues : outdatedTodos(all);
}

/** "to do" tab: one group per actor that has something, in ACTORS order. */
export function inboxGroups(todos: Todo[]): { actor: Actor; items: Todo[] }[] {
  return ACTORS.map((actor) => ({ actor, items: todos.filter((t) => t.actor === actor) })).filter((g) => g.items.length);
}

export function actorCounts(todos: Todo[]): Record<Actor, number> {
  return Object.fromEntries(ACTORS.map((a) => [a, todos.filter((t) => t.actor === a).length])) as Record<Actor, number>;
}

/** An agent in the "by machine" tab: its things to do, or none when it is truly fine. */
export interface MachineAgent {
  row: AgentRow;
  todos: Todo[];
}

export interface MachineCard {
  env: string;
  /** "report": readable; "unreported": no status branch; "unreadable": a report or remote check that failed */
  kind: "report" | "unreported" | "unreadable";
  env_status: EnvStatus | null;
  /** things to do on the machine as a whole (unreadable, stale) and on targets the report does not list */
  envTodos: Todo[];
  /** agents with something to do, report order */
  problem: MachineAgent[];
  /** agents with nothing to do and nothing wrong in their cells, on a machine that could be checked */
  ok: AgentRow[];
  /** agents on a machine whose check failed this time: neither fine nor known to be wrong */
  unchecked: AgentRow[];
  /** every thing to do on this card: envTodos + each problem agent's */
  count: number;
}

/** Wrong in its own cells: drifted or missing habits, failed or errored spot-checks, an upgraded host. */
const rowHasProblem = (r: AgentRow) => r.issues > 0;

/** "by machine" tab: one card per environment (reported, unreported or unreadable), each holding every thing to do there. */
export function machineCards(all: AllStatus, todos: Todo[]): MachineCard[] {
  const envs: string[] = [];
  const add = (e: string) => e && !envs.includes(e) && envs.push(e);
  all.environments.forEach((e) => add(e.env));
  all.errors.forEach((e) => add(String(e.env ?? "")));
  todos.forEach((t) => add(t.env || t.target));
  return envs.map((env) => {
    const e = all.environments.find((x) => x.env === env) ?? null;
    const err = all.errors.find((x) => String(x.env ?? "") === env);
    const mine = todos.filter((t) => (t.env || t.target) === env);
    const rows = e ? agentRows(e) : [];
    const problem: MachineAgent[] = [];
    const ok: AgentRow[] = [];
    const unchecked: AgentRow[] = [];
    for (const r of rows) {
      const its = mine.filter((t) => t.target === r.id);
      if (its.length || rowHasProblem(r)) problem.push({ row: r, todos: its });
      else if (e?.error) unchecked.push(r);
      else ok.push(r);
    }
    const onAgents = new Set(rows.map((r) => r.id));
    const envTodos = mine.filter((t) => !onAgents.has(t.target));
    const kind: MachineCard["kind"] = e && !e.error ? "report" : err && neverReported(err) ? "unreported" : "unreadable";
    return { env, kind, env_status: e, envTodos, problem, ok, unchecked, count: mine.length };
  });
}

/** Overview columns: the four habit kinds, then spot-checks. */
export const MATRIX_COLS = [...KINDS, "eval"] as const;
export type MatrixCol = (typeof MATRIX_COLS)[number];

/** The column a thing to do belongs to on its agent's row; null for the agent as a whole (host upgraded). */
export function todoColumn(t: Todo): MatrixCol | null {
  if (t.category === "eval" || t.category === "eval_error" || t.ref.kind === "eval") return "eval";
  return (KINDS as readonly string[]).includes(t.ref.kind) ? (t.ref.kind as MatrixCol) : null;
}

export interface MatrixRow {
  env: string;
  row: AgentRow;
  /** things to do on this agent, by column ("agent" for the agent as a whole) */
  cells: Record<MatrixCol | "agent", Todo[]>;
  count: number;
}

export interface MatrixGroup {
  env: string;
  card: MachineCard;
  /** things to do on the machine as a whole, shown on the group's header row */
  envTodos: Todo[];
  rows: MatrixRow[];
}

/** "overview" tab: agents × (four kinds + spot-checks), grouped by environment. */
export function matrixGroups(all: AllStatus, todos: Todo[], cards = machineCards(all, todos)): MatrixGroup[] {
  return cards.map((card) => {
    const rows = [...card.problem.map((p) => p.row), ...card.ok, ...card.unchecked];
    const order = new Map((card.env_status ? agentRows(card.env_status) : []).map((r, i) => [r.id, i]));
    rows.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
    return {
      env: card.env,
      card,
      envTodos: card.envTodos,
      rows: rows.map((row) => {
        const its = todos.filter((t) => t.target === row.id && (t.env || t.target) === card.env);
        const cells = { rules: [], skills: [], hooks: [], mcp: [], eval: [], agent: [] } as Record<MatrixCol | "agent", Todo[]>;
        for (const t of its) cells[todoColumn(t) ?? "agent"].push(t);
        return { env: card.env, row, cells, count: its.length };
      }),
    };
  });
}

/** The three tabs' figures, which must agree: to-do total = sum of cards' counts = issues on the overview's rows. */
export interface TabCounts {
  inbox: number;
  machines: number;
  matrix: number;
  /** the overview's "only problems" figure: the same number as the to-do total, counted by thing to do, not by row */
  problems: number;
}

export function tabCounts(todos: Todo[], cards: MachineCard[], groups: MatrixGroup[]): TabCounts {
  const inbox = inboxGroups(todos).reduce((n, g) => n + g.items.length, 0);
  const machines = cards.reduce((n, c) => n + c.count, 0);
  const matrix = groups.reduce((n, g) => n + g.envTodos.length + g.rows.reduce((m, r) => m + r.count, 0), 0);
  return { inbox, machines, matrix, problems: inbox };
}

/** Everything the page shows, computed once. */
export function views(all: AllStatus) {
  const todos = todoList(all);
  const cards = machineCards(all, todos);
  const groups = matrixGroups(all, todos, cards);
  return { todos, cards, groups, counts: tabCounts(todos, cards, groups), glossary: glossaryOf(all) };
}

// ---- tab memory (the page script embeds these two functions as they are) ----

export const TABS = ["inbox", "machines", "matrix", "relation", "plugins"] as const;
export type Tab = (typeof TABS)[number];

/** The tab to open: the remembered one, or "inbox" when storage is missing, blocked or holds something else. */
export function readTab(storage: Pick<Storage, "getItem"> | null | undefined): string {
  try {
    const v = storage?.getItem("agent-env.panel.tab");
    return v === "machines" || v === "matrix" || v === "relation" || v === "plugins" || v === "inbox" ? v : "inbox";
  } catch {
    return "inbox";
  }
}

/** Remembers the tab; false when storage is missing or refuses (private mode, blocked site data). */
export function writeTab(storage: Pick<Storage, "setItem"> | null | undefined, tab: string): boolean {
  try {
    if (!storage) return false;
    storage.setItem("agent-env.panel.tab", tab);
    return true;
  } catch {
    return false;
  }
}

// ---- switching views (the page script embeds this function as it is) ----

/** Height (px) to add under a shorter view so the page keeps its scroll position and the tabs stay under the
 *  pointer: what the page lacks to reach the bottom of the window from `scrollY`, 0 when it reaches it.
 *  `pageHeight` is the height of the content itself, which can be less than the window. */
export function scrollHold(scrollY: number, windowHeight: number, pageHeight: number): number {
  return Math.max(0, Math.ceil(scrollY + windowHeight - pageHeight));
}

/** The AI button every thing to do gets. AI fixes its own. A "needs you" row the engine marks `ai_task: "fix"` is
 *  carried through by AI too: it does every step it can, stops at the one that is the user's (paste into an app, OK a
 *  dry run) and goes on once the user says it is done. Any other "needs you" or "another machine" row AI only
 *  prepares (dry run first, the user finishes); for a notice AI reads the logs. */
export function aiTaskFor(t: Pick<Todo, "actor"> & { ai_task?: AiTask | null }): AiTask {
  if (t.actor === "ai" || (t.actor === "user" && t.ai_task === "fix")) return "fix";
  return t.actor === "notice" ? "logs" : "prepare";
}

/** Where the AI works, as part of a fact: `workdir` is the folder on this machine commands run in; `command_prefix`
 *  goes before every bin/agent-env command; `manifest` is the manifest file, or null when no personal manifest was
 *  found. Empty values are left out. */
export interface FactWhere {
  workdir?: string;
  command_prefix?: string;
  manifest?: string | null;
}
export const factWhere = (w: FactWhere): FactWhere => ({ ...(w.workdir ? { workdir: w.workdir } : {}), ...(w.command_prefix ? { command_prefix: w.command_prefix } : {}), ...(w.manifest === null ? { manifest: null } : w.manifest ? { manifest: w.manifest } : {}) });

/** The context an AI Task gets for one thing to do: only that issue's fields, as JSON, at most `limit` characters
 *  (steps are dropped from the end, then the title is cut, when it would not fit). */
export function todoPayload(t: Todo, limit = 2000, where: FactWhere = {}): string {
  const steps = t.steps.map((s) => ({ who: s.who, text_zh: s.text_zh, command: s.command, confirm: s.confirm }));
  const make = (n: number, title = t.title_zh) =>
    JSON.stringify({ id: t.id, env: t.env, target: t.target, title_zh: title, steps: steps.slice(0, n), ai_task: t.ai_task ?? aiTaskFor(t), ref: t.ref, ...factWhere(where) });
  for (let n = steps.length; n >= 0; n--) {
    const text = make(n);
    if (text.length <= limit) return text;
  }
  return make(0, t.title_zh.slice(0, 200));
}

/** Kept for the v4 name: the AI Task context of one thing to do. */
export const todoFact = todoPayload;

/** What the top "let AI fix all" button sends: the things AI can fix by itself (actor "ai"), one fact each.
 *  Empty when everything needs the user, another machine or is only a notice; the page then hides the button.
 *  The portal takes at most 20 facts per task. */
export function batchTodos(todos: Todo[]): Todo[] {
  return todos.filter((t) => t.actor === "ai");
}

export function batchPayload(todos: Todo[], label: string, limit = 2000, where: FactWhere = {}): { preset: string; facts: { label: string; value: string }[] } {
  const items = batchTodos(todos).slice(0, 20);
  return { preset: "fix-drift", facts: items.map((t, i) => ({ label: `${label} ${i + 1}`, value: todoPayload(t, limit, where) })) };
}

/** Which buttons a thing to do gets, the same in every tab. */
export interface TodoActions {
  /** every thing to do has an AI button; this picks its label and preset */
  ai: AiTask;
  /** commands to copy, every step's, in order */
  commands: string[];
  /** has a write step to confirm after a dry run */
  confirm: boolean;
}

export function todoActions(t: Todo): TodoActions {
  return { ai: aiTaskFor(t), commands: t.steps.map((s) => s.command).filter((c): c is string => !!c), confirm: t.steps.some((s) => s.confirm) };
}

/** The one main button a row shows. "ai": hand it to AI, the main button of every row AI can start on from this
 *  machine: what AI fixes, what needs you (AI fixes or prepares it and stops at your step) and notices (AI reads the
 *  logs). "copy": copy the commands to run on the other machine, where this machine's AI cannot reach. The steps
 *  themselves are behind "more" in every row. A row on another machine with nothing to copy falls back to its AI button. */
export type PrimaryAction = "ai" | "copy";

export interface RowActions extends TodoActions {
  primary: PrimaryAction;
  /** the AI button sits behind "more" (the primary is something else) */
  aiInMore: boolean;
  /** "copy commands" sits behind "more" */
  copyInMore: boolean;
}

export function primaryAction(t: Todo): PrimaryAction {
  return t.actor === "other_env" && todoActions(t).commands.length ? "copy" : "ai";
}

export function rowActions(t: Todo): RowActions {
  const a = todoActions(t);
  const primary = primaryAction(t);
  return { ...a, primary, aiInMore: primary !== "ai", copyInMore: primary !== "copy" && a.commands.length > 0 };
}

/** The things to do behind one relation cell (an agent's rules, skills, hooks or MCP) that an AI task can take on;
 *  the relation pane puts the To do tab's AI button next to that cell's problems. */
export function cellTodos(todos: Todo[], env: string, agent: string, kind: string): Todo[] {
  return todos.filter((t) => t.ai_task && t.target === agent && (t.env || t.target) === env && todoColumn(t) === kind);
}

// ---- v7: the relation graph (sources → machine boxes holding their agents, four habit cells in each agent) ----
// Lines stand for real habit cells: one source → machine line for every source some cell on that machine comes from,
// carrying the worst state of the cells behind it; only the problem ones (drift, missing) are drawn in colour. An
// agent sits inside its machine's box, so there is no machine → agent line. Nothing here is drawn; page.ts renders.

function normalizeRelations(r: Record<string, unknown>): Relations {
  const sources: Record<string, string> = {};
  if (isObj(r.sources)) for (const [k, v] of Object.entries(r.sources)) if (typeof v === "string") sources[k] = v;
  const env_sources: Record<string, Record<string, string>> = {};
  if (isObj(r.env_sources))
    for (const [env, m] of Object.entries(r.env_sources))
      if (isObj(m)) env_sources[env] = Object.fromEntries(Object.entries(m).filter(([, v]) => typeof v === "string")) as Record<string, string>;
  const env_names: Record<string, string> = {};
  if (isObj(r.env_names)) for (const [k, v] of Object.entries(r.env_names)) if (typeof v === "string" && v) env_names[k] = v;
  const targets = (Array.isArray(r.targets) ? r.targets : []).filter(isObj).map((t) => ({
    id: str(t.id),
    env: str(t.env),
    host: str(t.host),
    name: str(t.name),
    roles: Array.isArray(t.roles) ? t.roles.map(String) : [],
  })).filter((t) => t.id && t.env);
  return { sources, env_sources, env_names, targets };
}

/** A line's or node's state: a habit state, or "none" when nothing is known (a machine that never reported). */
export type RelState = State | "none";
const REL_RANK: Record<RelState, number> = { missing: 0, drift: 1, unknown: 2, customized: 3, synced: 4, none: 5 };
const worse = (a: RelState, b: RelState): RelState => (REL_RANK[a] <= REL_RANK[b] ? a : b);
export const isProblem = (s: RelState) => s === "missing" || s === "drift";

/** Source of cells that do not come from a shared source: a login, a service, a setting of that machine only. */
export const LOCAL_SOURCE = "local";
/** Hosts that are not agents but checks of the machine (the account folders); listed last, apart from the agents. */
export const AUX_HOSTS = ["sideby"];

export type RelCol = "source" | "env" | "agent";

/** One habit of an agent, for the detail pane: the source it comes from and that source's path. */
export interface RelHabit {
  kind: string;
  name: string;
  state: State;
  /** a source key, LOCAL_SOURCE, or "" when the report has no source data */
  origin: string;
  detail: string;
  fix: string;
}

/** One of the four cells inside an agent node; the same states as the overview's dots. */
export interface RelCell {
  kind: string;
  state: DotState;
  total: number;
  problems: number;
  /** focus key of this cell: "ak:<agent>|<kind>" */
  key: string;
  /** focus keys this cell lights up for: its agent, machine, and the sources of its habits */
  on: string[];
}

export interface RelNode {
  /** unique on the page, no spaces: "s:rules", "e:mac", "a:mac/codex" */
  key: string;
  col: RelCol;
  /** source key, env name or agent id */
  id: string;
  /** the human name; for a source the page picks the words by id */
  label: string;
  /** an agent's environment */
  env: string;
  /** a source's path */
  path: string;
  host: string;
  roles: string[];
  state: RelState;
  /** habit cells behind this node */
  cells: number;
  /** drift + missing cells behind it */
  problems: number;
  /** a machine's report freshness; on an agent, set only when its machine gave no readable report */
  report: Freshness | null;
  /** agent only: the account folders and the like, not an agent */
  aux: boolean;
  /** agent only: the four habit cells, and every habit for the detail pane */
  kinds: RelCell[];
  habits: RelHabit[];
  /** focus keys this node lights up for: every node and agent cell on a chain through it, itself included */
  on: string[];
}

export interface RelLink {
  from: string;
  to: string;
  state: RelState;
  cells: number;
  /** focus keys on a chain through this line */
  on: string[];
}

export interface RelationGraph {
  /** sources some cell comes from, in the report's order (the machine itself last) */
  sources: RelNode[];
  /** sources no agent is checked against: a grey note, no node, no line */
  idle: { id: string; path: string }[];
  envs: RelNode[];
  agents: RelNode[];
  links: RelLink[];
  /** the report says where habits come from (an older engine does not) */
  hasSources: boolean;
  /** source key → path (a source used on several machines lists every path) */
  paths: Record<string, string>;
  /** environment → source key → that machine's own path, where it differs */
  envPaths: Record<string, Record<string, string>>;
  /** agent cells in drift or missing: the "only problems" figure */
  problemCells: number;
}

/** One habit cell as a path through the columns. */
interface Chain {
  src: string | null;
  env: string;
  agent: string;
  kind: string;
  state: State;
}

/** Something to look at: a problem behind it, a machine with no fresh report, or an agent on one. */
export function relAlert(n: RelNode): boolean {
  if (n.problems > 0) return true;
  if (n.col === "env") return !!n.report && (n.report.kind !== "report" || n.report.stale);
  return n.col === "agent" && n.report !== null;
}

export function relationGraph(all: AllStatus, now: number): RelationGraph {
  const rel = all.relations;
  const hasSources = !!rel && Object.keys(rel.sources).length > 0;
  const chains: Chain[] = [];
  const agents: RelNode[] = [];
  const blank = (key: string, col: RelCol, id: string, label: string): RelNode => ({ key, col, id, label, env: "", path: "", host: "", roles: [], state: "none", cells: 0, problems: 0, report: null, aux: false, kinds: [], habits: [], on: [key] });
  const cellsOf = (id: string, habits: HabitCell[] | null): RelCell[] =>
    KINDS.map((k) => {
      const s = kindSummary(habits ?? [], k);
      const state: DotState = !habits ? "none" : s.state === "drift" ? (s.counts.missing ? "missing" : "drift") : s.state;
      return { kind: k, state, total: s.total, problems: s.bad, key: `ak:${id}|${k}`, on: [`ak:${id}|${k}`, `a:${id}`] };
    });

  // machines: reported ones in report order, then unreadable or never reported, then declared ones not seen
  const fresh = freshness(all, now);
  const envIds: string[] = [];
  for (const f of fresh) if (!envIds.includes(f.env)) envIds.push(f.env);
  for (const env of Object.keys(rel?.env_names ?? {})) if (!envIds.includes(env)) envIds.push(env);
  for (const t of rel?.targets ?? []) if (!envIds.includes(t.env)) envIds.push(t.env);
  const names = new Map((rel?.targets ?? []).map((t) => [t.id, t.name]));
  const envs = envIds.map((env) => {
    const n = blank(`e:${env}`, "env", env, rel?.env_names[env] || env);
    n.report = fresh.find((f) => f.env === env) ?? { env, kind: "unreported", ageDays: null, stale: false };
    n.env = env;
    return n;
  });

  for (const env of envIds) {
    const e = all.environments.find((x) => x.env === env && !x.error);
    const mine: RelNode[] = [];
    if (e)
      for (const r of agentRows(e)) {
        const a = blank(`a:${r.id}`, "agent", r.id, names.get(r.id) || hostLabel(r.host));
        const habits = r.habits.filter((h) => (KINDS as readonly string[]).includes(h.kind));
        Object.assign(a, { env, host: r.host, roles: r.roles, state: "synced" as RelState, aux: AUX_HOSTS.includes(r.host), kinds: cellsOf(r.id, habits) });
        for (const h of habits) {
          const src = hasSources ? (h.origin && rel!.sources[h.origin] !== undefined ? h.origin : LOCAL_SOURCE) : null;
          a.habits.push({ kind: h.kind, name: h.name, state: h.state, origin: src ?? "", detail: String(h.detail ?? ""), fix: String(h.fix ?? "") });
          chains.push({ src, env, agent: r.id, kind: h.kind, state: h.state });
        }
        mine.push(a);
      }
    // agents the manifest declares on a machine that gave no readable report: shown, nothing known about them
    else
      for (const t of rel?.targets ?? [])
        if (t.env === env) {
          const a = blank(`a:${t.id}`, "agent", t.id, t.name || hostLabel(t.host));
          Object.assign(a, { env, host: t.host, roles: t.roles, aux: AUX_HOSTS.includes(t.host), kinds: cellsOf(t.id, null), report: envs.find((x) => x.id === env)!.report });
          mine.push(a);
        }
    // the account folders and the like go last, apart from the agents
    agents.push(...mine.filter((a) => !a.aux), ...mine.filter((a) => a.aux));
  }

  const used = new Set(chains.map((c) => c.src));
  const sourceIds = hasSources ? [...Object.keys(rel!.sources).filter((k) => used.has(k)), ...(used.has(LOCAL_SOURCE) ? [LOCAL_SOURCE] : [])] : [];
  const idle = hasSources ? Object.keys(rel!.sources).filter((k) => !used.has(k)).map((id) => ({ id, path: rel!.sources[id] })) : [];
  const sources = sourceIds.map((id) => Object.assign(blank(`s:${id}`, "source", id, id), { path: rel!.sources[id] ?? "" }));
  const nodes = new Map<string, RelNode>([...sources, ...envs, ...agents].map((n) => [n.key, n]));
  const keysOf = (c: Chain) => [...(c.src ? [`s:${c.src}`] : []), `e:${c.env}`, `a:${c.agent}`, `ak:${c.agent}|${c.kind}`];
  const add = (list: string[], keys: string[]) => keys.forEach((k) => list.includes(k) || list.push(k));

  // nodes: worst state and counts over their chains
  const touched = new Set<string>();
  for (const c of chains)
    for (const k of keysOf(c).filter((x) => nodes.has(x))) {
      const n = nodes.get(k)!;
      n.state = touched.has(k) ? worse(n.state, c.state) : c.state;
      touched.add(k);
      n.cells++;
      if (isProblem(c.state)) n.problems++;
      add(n.on, keysOf(c));
    }
  const cellOf = new Map(agents.flatMap((a) => a.kinds.map((k) => [k.key, k] as const)));
  for (const c of chains) add(cellOf.get(`ak:${c.agent}|${c.kind}`)!.on, keysOf(c));
  for (const a of agents) for (const k of a.kinds) add(k.on, [`e:${a.env}`]);
  for (const n of [...envs, ...agents]) if (!touched.has(n.key)) n.state = "none";
  for (const e of envs)
    for (const a of agents.filter((x) => x.env === e.id)) {
      add(e.on, [a.key, ...a.kinds.map((k) => k.key)]);
      add(a.on, [e.key, ...a.kinds.map((k) => k.key)]);
    }

  // lines: one per pair of neighbouring nodes that some chain passes through
  const links = new Map<string, RelLink>();
  for (const c of chains) {
    if (!c.src) continue;
    const id = `s:${c.src}>e:${c.env}`;
    let l = links.get(id);
    if (!l) links.set(id, (l = { from: `s:${c.src}`, to: `e:${c.env}`, state: c.state, cells: 0, on: [] }));
    l.state = worse(l.state, c.state);
    l.cells++;
    add(l.on, keysOf(c));
  }

  const problemCells = agents.reduce((n, a) => n + a.kinds.filter((k) => isProblem(k.state as RelState)).length, 0);
  return { sources, idle, envs, agents, links: [...links.values()], hasSources, paths: { ...(rel?.sources ?? {}) }, envPaths: rel?.env_sources ?? {}, problemCells };
}

/** The path of a source as one machine sees it: that machine's own path when the report has one, else the shared one. */
export function sourcePath(g: Pick<RelationGraph, "paths" | "envPaths">, origin: string, env: string): string {
  return g.envPaths[env]?.[origin] ?? g.paths[origin] ?? "";
}

/** One habit kind of an agent, for the detail pane: problems first and open, the rest folded into a count. */
export interface AgentKind {
  kind: string;
  state: DotState;
  total: number;
  /** drift and missing, in report order */
  problems: RelHabit[];
  /** everything else: synced, customised, can't check */
  rest: RelHabit[];
  /** the sources its items come from, each once, in order */
  origins: string[];
  counts: Record<State, number>;
}

export function agentKinds(a: Pick<RelNode, "kinds" | "habits">): AgentKind[] {
  return a.kinds
    .filter((c) => c.total)
    .map((c) => {
      const mine = a.habits.filter((h) => h.kind === c.kind);
      const counts = Object.fromEntries(STATES.map((s) => [s, mine.filter((h) => h.state === s).length])) as Record<State, number>;
      return {
        kind: c.kind,
        state: c.state,
        total: c.total,
        problems: mine.filter((h) => isProblem(h.state)),
        rest: mine.filter((h) => !isProblem(h.state)),
        origins: [...new Set(mine.map((h) => h.origin).filter(Boolean))],
        counts,
      };
    });
}

/** "Only problems": the nodes on a problem chain (and machines with no fresh report, with their agents), and the
 *  problem lines between them. Same nodes, a smaller graph. */
export function problemGraph(g: RelationGraph): RelationGraph {
  const agents = g.agents.filter(relAlert);
  const envs = g.envs.filter((e) => relAlert(e) || agents.some((a) => a.env === e.id));
  const keep = new Set([...agents, ...envs].map((n) => n.key));
  const links = g.links.filter((l) => isProblem(l.state) && keep.has(l.to));
  const sources = g.sources.filter((s) => links.some((l) => l.from === s.key));
  return { ...g, sources, idle: [], envs, agents, links };
}

/** Where each node sits, in px: sources in a column on the left, one box per machine on the right (its header, then
 *  its agents' rows). A machine node is its box's header: lines end there. Widths are fixed so names and the four
 *  cells sit together instead of spreading over the page. */
export interface RelLayout {
  width: number;
  height: number;
  cols: { source: { x: number; w: number }; box: { x: number; w: number } };
  /** node key → top (a machine: the top of its box) */
  y: Record<string, number>;
  /** row heights: a source, a machine box's header, an agent row */
  h: Record<RelCol, number>;
  /** machine key → height of its whole box */
  boxH: Record<string, number>;
  /** top of the line that sets the account folders apart from the agents */
  dividers: number[];
  /** top of the note listing sources no agent is checked against */
  idleY: number | null;
}

export const REL_COLS = { source: { x: 0, w: 200 }, box: { x: 272, w: 400 } };

export function relationLayout(g: RelationGraph, o: { nodeH?: number; headH?: number; rowH?: number; gap?: number; boxGap?: number; pad?: number; auxGap?: number } = {}): RelLayout {
  const nodeH = o.nodeH ?? 32;
  const headH = o.headH ?? 44;
  const rowH = o.rowH ?? 32;
  const gap = o.gap ?? 8;
  const boxGap = o.boxGap ?? 16;
  const pad = o.pad ?? 4;
  const auxGap = o.auxGap ?? 8;
  const y: Record<string, number> = {};
  const boxH: Record<string, number> = {};
  const dividers: number[] = [];
  let top = 0;
  for (const e of g.envs) {
    y[e.key] = top;
    let cur = top + headH + pad;
    const mine = g.agents.filter((a) => a.env === e.id);
    mine.forEach((a, i) => {
      if (i > 0 && a.aux && !mine[i - 1].aux) {
        dividers.push(cur + auxGap / 2);
        cur += auxGap;
      }
      y[a.key] = cur;
      cur += rowH;
    });
    boxH[e.key] = (mine.length ? cur + pad : top + headH) - top;
    top += boxH[e.key] + boxGap;
  }
  const boxesH = Math.max(0, top - boxGap);
  g.sources.forEach((n, i) => (y[n.key] = i * (nodeH + gap)));
  const srcH = g.sources.length ? g.sources.length * (nodeH + gap) - gap : 0;
  const idleY = g.idle.length ? srcH + 12 : null;
  const height = Math.max(boxesH, srcH + (g.idle.length ? 52 : 0), headH);
  return { width: REL_COLS.box.x + REL_COLS.box.w, height, cols: REL_COLS, y, h: { source: nodeH, env: headH, agent: rowH }, boxH, dividers, idleY };
}

/** The relation tab's "only problems" switch, remembered like the tab (the page script embeds these two functions). */
export function readRelFilter(storage: Pick<Storage, "getItem"> | null | undefined): "all" | "problems" {
  try {
    return storage?.getItem("agent-env.panel.relation") === "problems" ? "problems" : "all";
  } catch {
    return "all";
  }
}

export function writeRelFilter(storage: Pick<Storage, "setItem"> | null | undefined, v: string): boolean {
  try {
    if (!storage) return false;
    storage.setItem("agent-env.panel.relation", v === "problems" ? "problems" : "all");
    return true;
  } catch {
    return false;
  }
}
