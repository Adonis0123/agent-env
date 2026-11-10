import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { ACTORS, aiTaskFor, habitFix, primaryAction, rowActions, guardWrites, batchPayload, batchTodos, outdatedTodos, glossaryOf, gloss, inboxGroups, machineCards, matrixGroups, readTab, tabCounts, type Todo, todoActions, todoList, todoPayload, views, writeTab } from "./model.ts";
import { ageDays, agentGroups, agentRows, type AllStatus, dotsScore, evalRows, evalSummary, freshness, type HabitCell, isStale, kindDots, kindSummary, neverReported, overview, parseAllStatus, StatusShapeError, versionSummary } from "./model.ts";

const NOW = Date.parse("2026-11-09T08:00:00Z");
const fixture = (name: string) => parseAllStatus(JSON.parse(readFileSync(join(import.meta.dirname, "fixtures", `${name}.json`), "utf8")));

test("parse checks the schema and the parts the page relies on", () => {
  assert.throws(() => parseAllStatus([]), StatusShapeError);
  assert.throws(() => parseAllStatus({ schema: 2, environments: [] }), /unknown schema 2/);
  assert.throws(() => parseAllStatus({ schema: 1 }), /environments/);
  assert.throws(() => parseAllStatus({ schema: 1, environments: [{}] }), /env is missing/);
  const s = parseAllStatus({ schema: 1, environments: [{ env: "mac", targets: [{ id: "mac/x", habits: [{ kind: "rules", name: "A", state: "weird" }] }] }] });
  assert.equal(s.environments[0].targets[0].habits[0].state, "unknown", "an unknown state is shown as unknown");
  assert.deepEqual(s.errors, []);
});

test("age in days; a report is stale only when the engine lists an env_stale issue for it", () => {
  assert.equal(ageDays("2026-11-01T07:00:00Z", NOW), 8);
  assert.equal(ageDays("not a date", NOW), null);
  const stale = fixture("v5-stale");
  assert.deepEqual(freshness(stale, NOW).map((f) => [f.env, f.stale, f.ageDays]), [["mac", false, 0], ["grok-bot", false, 0], ["vps-helper", true, 8]]);
  assert.equal(isStale(stale.issues, "vps-helper"), true);
  // no local day count: an old report without the engine's env_stale issue is not marked stale
  const old = { ...stale, issues: [] };
  assert.deepEqual(freshness(old, NOW).map((f) => f.stale), [false, false, false]);
  assert.equal(isStale(undefined, "mac"), false);
});

test("freshness: an environment that never reported and one whose report cannot be read are told apart", () => {
  const drift = fixture("drift");
  assert.deepEqual(freshness(drift, NOW).map((f) => [f.env, f.kind, f.stale]), [["mac", "report", false], ["wsl", "report", false], ["grok-bot", "unreported", false]]);
  assert.equal(neverReported({ env: "wsl", message: "读不到 status/wsl 分支" }), true, "the engine's wording for a missing branch");
  assert.equal(neverReported({ env: "wsl", message: "status/wsl：不是 schema 1 的 wsl 环境状态" }), false);
  const broken: AllStatus = { ...drift, errors: [{ env: "wsl2", message: "origin/status/wsl2: bad JSON" }, { env: "", message: "fetch failed" }] };
  assert.deepEqual(freshness(broken, NOW).slice(2).map((f) => [f.env, f.kind]), [["wsl2", "unreadable"]], "an error with no environment stays out of the header");
});

const h = (kind: string, state: HabitCell["state"], name = `${kind}-${state}`): HabitCell => ({ kind, name, state });

test("kind summary: all synced, customized, drift or missing, unknown, nothing registered", () => {
  assert.equal(kindSummary([h("rules", "synced"), h("rules", "synced")], "rules").state, "synced");
  assert.equal(kindSummary([h("rules", "synced"), h("rules", "customized")], "rules").state, "customized");
  const bad = kindSummary([h("mcp", "synced"), h("mcp", "drift"), h("mcp", "missing"), h("mcp", "customized"), h("mcp", "unknown")], "mcp");
  assert.deepEqual([bad.state, bad.bad, bad.total, bad.counts.drift, bad.counts.missing], ["drift", 2, 5, 1, 1]);
  assert.equal(kindSummary([h("hooks", "missing")], "hooks").state, "drift", "missing alone is shown as a problem too");
  assert.equal(kindSummary([h("hooks", "synced"), h("hooks", "unknown")], "hooks").state, "unknown");
  const none = kindSummary([h("rules", "drift")], "skills");
  assert.deepEqual([none.state, none.total], ["none", 0], "a kind with no registered check leaves the cell empty");
});

const ev = (outcome: string, detail = "") => ({ name: "c", target: "mac/x", outcome, outcomeText: outcome, runs: 1, passed: outcome === "pass" ? 1 : 0, detail }) as ReturnType<typeof evalRows>[number];

test("spot-check summary: all pass, a failure, an error, only skips, nothing ran", () => {
  assert.deepEqual(evalSummary([ev("pass"), ev("pass"), ev("pass")]), { state: "pass", passed: 3, total: 3, failed: 0, errored: 0, skipReason: "" });
  const mixed = evalSummary([ev("pass"), ev("fail"), ev("err"), ev("skip", "n/a")]);
  assert.deepEqual([mixed.state, mixed.passed, mixed.total, mixed.failed, mixed.errored], ["fail", 1, 3, 1, 1], "a failure outranks an error; skips are not counted");
  assert.equal(evalSummary([ev("pass"), ev("err")]).state, "err");
  assert.equal(evalSummary([ev("pass"), ev("unknown")]).errored, 1, "an unreadable state is not a pass");
  const skip = evalSummary([ev("skip", "cloud-hosted")]);
  assert.deepEqual([skip.state, skip.skipReason, skip.total], ["skip", "cloud-hosted", 0]);
  assert.equal(evalSummary([]).state, "none");
});

test("host version: changed, unchanged, not reported", () => {
  assert.deepEqual(versionSummary({ host_version: { recorded: "0.46.0", current: "0.48.1", changed: true } }), { state: "changed", recorded: "0.46.0", current: "0.48.1" });
  assert.equal(versionSummary({ host_version: { recorded: "1.0", current: "1.0", changed: false } }).state, "same");
  assert.equal(versionSummary({ host_version: { recorded: "1.0", current: null, changed: false } }).state, "same");
  assert.equal(versionSummary({}).state, "none");
});

test("agent rows: one per target in report order, bad habits first, spot-checks attached by target id", () => {
  const rows = agentRows(fixture("drift").environments[0]);
  assert.deepEqual(rows.map((r) => r.id), ["mac/claude", "mac/codex", "mac/hermes", "mac/claude-code", "mac/grok-bot"], "spot-checked targets without a report row get one");
  const hermes = rows.find((r) => r.id === "mac/hermes")!;
  assert.deepEqual([hermes.short, hermes.host, hermes.unverified, hermes.roles], ["hermes", "hermes", true, ["bot", "cron"]]);
  assert.deepEqual(hermes.kinds.map((k) => [k.kind, k.state]), [["rules", "drift"], ["skills", "drift"], ["hooks", "unknown"], ["mcp", "drift"]]);
  assert.deepEqual(hermes.habits.slice(0, 2).map((x) => x.state), ["missing", "missing"]);
  assert.equal(hermes.eval.state, "err");
  assert.equal(hermes.issues, 4, "3 drift/missing habits + 1 errored spot-check");
  const codex = rows.find((r) => r.id === "mac/codex")!;
  assert.deepEqual([codex.version.state, codex.eval.state, codex.issues], ["changed", "fail", 3]);
  const claude = rows.find((r) => r.id === "mac/claude")!;
  assert.deepEqual(claude.kinds.map((k) => k.state), ["customized", "synced", "synced", "synced"]);
  assert.deepEqual([claude.eval.state, claude.issues], ["none", 0]);
  const bot = rows.find((r) => r.id === "mac/grok-bot")!;
  assert.deepEqual([bot.eval.state, bot.kinds.every((k) => k.state === "none")], ["skip", true]);
});


test("counts: one figure for problems, the number of things to do; habits for synced and customized", () => {
  const drift = fixture("drift");
  const o = overview(drift);
  assert.equal(o.issues, todoList(drift).length);
  assert.equal(o.issues, 1, "a report without issues: one \"update the engine\" item");
  assert.equal(overview(fixture("v5-issues")).issues, 5, "the engine's issues, counted as given");
  const habits = drift.environments.flatMap((e) => e.targets.flatMap((t) => t.habits));
  assert.equal(o.synced, habits.filter((x) => x.state === "synced").length);
  assert.deepEqual([o.synced, o.customized, o.lastCheck], [26, 5, "2026-11-09T07:30:00Z"]);
  assert.deepEqual(overview(fixture("empty")), { issues: 0, synced: 0, customized: 0, lastCheck: "" });
});

test("agent groups: agents with a problem first, the healthy ones folded", () => {
  const { problem, ok } = agentGroups(fixture("drift"));
  assert.deepEqual(problem.map((r) => r.id), ["mac/codex", "mac/hermes"]);
  assert.deepEqual(ok.map((r) => r.id), ["mac/claude", "mac/claude-code", "mac/grok-bot", "wsl/claude", "wsl/codex"]);
});

test("four dots: one per kind, problems coloured by kind of problem, empty slot when nothing is registered", () => {
  const rows = agentRows(fixture("drift").environments[0]);
  const hermes = kindDots(rows.find((r) => r.id === "mac/hermes")!);
  assert.deepEqual(hermes.map((d) => d.state), ["drift", "missing", "unknown", "missing"]);
  assert.deepEqual(dotsScore(hermes), { ok: 0, total: 4 });
  const claude = kindDots(rows.find((r) => r.id === "mac/claude")!);
  assert.deepEqual(claude.map((d) => d.state), ["customized", "synced", "synced", "synced"]);
  assert.deepEqual(dotsScore(claude), { ok: 4, total: 4 });
  const bot = kindDots(rows.find((r) => r.id === "mac/grok-bot")!);
  assert.deepEqual([bot.every((d) => d.state === "none"), dotsScore(bot).total], [true, 0]);
});

test("spot-check results follow the schema: case, state, runs, passed", () => {
  assert.deepEqual(evalRows(fixture("normal").environments[0]), []);
  const rows = evalRows(fixture("drift").environments[0]);
  assert.deepEqual(
    rows.map((r) => [r.outcome, r.target, r.name, r.passed, r.runs]),
    [
      ["pass", "mac/claude-code", "delete-uses-trash", 3, 3],
      ["fail", "mac/codex", "progress-notes-in-chinese", 3, 5],
      ["err", "mac/hermes", "external-send-asks", 0, 3],
      ["skip", "mac/grok-bot", "delete-uses-trash", 0, 0],
    ],
  );
  const odd = evalRows({ ...fixture("drift").environments[0], eval: { ran_at: null, results: [{ target: "x", case: "c", state: "weird" } as never] } });
  assert.deepEqual([odd[0].outcome, odd[0].outcomeText, odd[0].runs], ["unknown", "weird", null]);
});

// ---- v5 ----

test("v5: issues and glossary are read as given; bad actors and tasks are made safe", () => {
  const all = fixture("v5-issues");
  assert.equal(all.issues!.length, 5);
  assert.deepEqual(all.issues!.map((i) => i.actor), ["other_env", "user", "user", "ai", "notice"]);
  assert.equal(all.glossary!.drift.label_zh, "漂移");
  const odd = parseAllStatus({ schema: 1, environments: [], issues: [{ id: "x", target: "mac/a", actor: "boss", ai_task: "rm", steps: [{ who: "x", text_zh: "t", command: "c", confirm: "yes" }] }] });
  assert.deepEqual([odd.issues![0].actor, odd.issues![0].ai_task, odd.issues![0].env, odd.issues![0].steps[0].who, odd.issues![0].steps[0].confirm], ["notice", null, "mac", "user", false]);
  assert.equal(fixture("drift").issues, undefined, "an older report has no issues");
});

test("v5 counts agree across the three tabs: to-do total = sum of machine cards = issues on the overview", () => {
  // v5-real.json is the engine's own output, written by the engine side; checked when present
  const real = existsSync(join(import.meta.dirname, "fixtures", "v5-real.json")) ? ["v5-real"] : [];
  for (const name of ["v5-issues", "v5-stale", "v5-remote-error", "v5-ok", "drift", "normal", "empty", ...real]) {
    const { todos, counts } = views(fixture(name));
    assert.equal(counts.inbox, todos.length, name);
    assert.equal(counts.machines, todos.length, name);
    assert.equal(counts.matrix, todos.length, name);
  }
  const { counts } = views(fixture("v5-issues"));
  assert.deepEqual(counts, { inbox: 5, machines: 5, matrix: 5, problems: 5 });
  // a thing to do on a target the report does not list still lands on its machine and on the overview
  const all = fixture("v5-issues");
  const stray: Todo = { ...all.issues![3], id: "mac/ghost:x", target: "mac/ghost" };
  const todos = [...all.issues!, stray];
  const cards = machineCards(all, todos);
  assert.deepEqual(tabCounts(todos, cards, matrixGroups(all, todos, cards)).matrix, 6);
  assert.deepEqual(cards[0].envTodos.map((t) => t.target), ["mac/ghost"]);
});

test("v5 actor groups: AI, needs you, another machine, notice, in that order, empty groups left out", () => {
  const groups = inboxGroups(todoList(fixture("v5-issues")));
  assert.deepEqual(groups.map((g) => [g.actor, g.items.length]), [["ai", 1], ["user", 2], ["other_env", 1], ["notice", 1]]);
  assert.deepEqual(ACTORS, ["ai", "user", "other_env", "notice"]);
  assert.deepEqual(inboxGroups([]), []);
});

test("v5 machine cards: unreported and unreadable machines get a card; 'fine' holds only agents with nothing wrong", () => {
  const cards = machineCards(fixture("v5-issues"), todoList(fixture("v5-issues")));
  assert.deepEqual(cards.map((c) => [c.env, c.kind, c.count]), [["mac", "report", 2], ["grok-bot", "report", 1], ["vps-helper", "report", 1], ["wsl", "unreported", 1]]);
  assert.deepEqual(cards[0].problem.map((p) => p.row.id), ["mac/claude-code", "mac/codex"]);
  assert.deepEqual(cards[1].problem.map((p) => p.row.id), ["grok-bot/bot"], "Grok Bot gets its own card");
  assert.ok(cards[0].ok.every((r) => r.issues === 0), "no agent with a red cell among the fine ones");
  const remote = machineCards(fixture("v5-remote-error"), todoList(fixture("v5-remote-error")));
  assert.deepEqual(remote.map((c) => [c.env, c.kind, c.envTodos.length, c.ok.length, c.unchecked.length]), [["mac", "report", 0, 8, 0], ["grok-bot", "report", 0, 1, 0], ["vps-helper", "unreadable", 1, 0, 1]]);
});

test("v5 overview: each thing to do sits in its column", () => {
  const all = fixture("v5-issues");
  const groups = matrixGroups(all, todoList(all));
  const mac = groups.find((g) => g.env === "mac")!;
  const at = (id: string) => mac.rows.find((r) => r.row.id === id)!.cells;
  assert.equal(at("mac/codex").eval.length, 1);
  assert.equal(at("mac/claude-code").eval.length, 1);
  const bot = groups.find((g) => g.env === "grok-bot")!;
  assert.equal(bot.rows.find((r) => r.row.id === "grok-bot/bot")!.cells.rules.length, 1, "Grok Bot is its own environment");
  assert.equal(groups.find((g) => g.env === "wsl")!.envTodos.length, 1);
});

test("a status write is never a copyable step: the read-only check comes first, the write waits for a confirm", () => {
  const steps = guardWrites([{ who: "other_env", text_zh: "x", command: "bin/agent-env status --commit --push", confirm: false }]);
  assert.deepEqual(steps.map((s) => [s.command, s.confirm]), [["bin/agent-env status", false], [null, true]]);
  assert.match(steps[1].text_zh, /bin\/agent-env status --commit --push/);
  for (const name of ["drift", "v5-issues", "v5-stale", "v5-remote-error", ...(existsSync(join(import.meta.dirname, "fixtures", "v5-real.json")) ? ["v5-real"] : [])]) {
    for (const t of todoList(fixture(name)))
      for (const c of todoActions(t).commands) assert.doesNotMatch(c, /status\b.*--(commit|push)/, `${name}: ${t.id}`);
  }
  const { counts } = views(fixture("drift"));
  assert.equal(counts.problems, counts.inbox, "drift: \"only problems\" = the to-do total");
  assert.equal(counts.problems, 1);
});

test("a report without issues: one \"update this machine's engine\" item, nothing guessed from the cells", () => {
  const drift = fixture("drift");
  assert.equal(drift.issues, undefined);
  const todos = todoList(drift);
  assert.deepEqual(todos, outdatedTodos(drift));
  // drift.json has drifted, missing, failed and upgraded cells, another machine's branch (wsl) and an env only in
  // `errors` (grok-bot); none of them becomes a thing to do of its own. `status --json --all` on this machine rebuilds
  // every environment's issues, so a report without them means this machine's engine is old: one item, fixed here.
  assert.deepEqual(todos.map((t) => [t.id, t.env, t.category, t.actor, t.source]), [["report_outdated", "mac", "report_outdated", "user", "outdated"]]);
  const [t] = todos;
  assert.match(t.title_zh, /旧版引擎/);
  assert.match(t.title_en!, /old engine/);
  // no step sends the user to another (or a remote-exec) machine, and nothing writes a status branch
  assert.deepEqual(t.steps.map((s) => [s.who, s.command, s.confirm]), [["user", null, false], ["ai", "bin/agent-env status --json --all --fetch", false]]);
  for (const s of t.steps) assert.doesNotMatch(`${s.text_zh} ${s.command ?? ""}`, /--commit|--push|--env/);
  assert.equal(views(drift).counts.inbox, 1);
  assert.deepEqual(todoList(fixture("empty")), [], "no environments, nothing to report again");
  assert.deepEqual(todoList(fixture("normal")), [], "an empty issues list means nothing to do");
  assert.ok(todoList(fixture("v5-issues")).every((t) => t.source === "engine"), "the engine's issues are used as given");
});

test("glossary: the report's entries over the panel's one copy; drift is 漂移 either way; prefixed keys found", () => {
  const g = glossaryOf(fixture("v5-issues"));
  assert.equal(g.drift.label_zh, "漂移");
  assert.equal(g.drift.label_en, undefined, "the report's entry replaces the built-in one whole");
  const builtin = glossaryOf(fixture("drift"));
  assert.equal(fixture("drift").glossary, undefined);
  assert.deepEqual([builtin.drift.label_zh, builtin.drift.label_en], ["漂移", "Drift"]);
  assert.equal(glossaryOf({ glossary: { other: { label_zh: "x", explain_zh: "" } } }).drift.label_zh, "漂移", "a key the report leaves out comes from the panel's copy");
  assert.equal(gloss({ "state.drift": { label_zh: "x", explain_zh: "" } }, "drift")!.label_zh, "x");
  assert.equal(gloss({}, "drift"), null);
});

test("v5 AI buttons: label by actor, payload holds only that issue, batch takes only what AI can fix", () => {
  const todos = todoList(fixture("v5-issues"));
  // another machine: AI prepares; the two deliveries need you once and the engine marks them `fix`: AI carries them through
  assert.deepEqual(todos.map((t) => aiTaskFor(t)), ["prepare", "fix", "fix", "fix", "logs"]);
  assert.deepEqual(todos.map((t) => todoActions(t).ai), ["prepare", "fix", "fix", "fix", "logs"], "every thing to do has an AI button");
  const mine = todos.find((t) => t.actor === "user")!;
  assert.deepEqual((["fix", "prepare", "logs", null] as const).map((ai_task) => aiTaskFor({ ...mine, ai_task })), ["fix", "prepare", "prepare", "prepare"], "needs you: AI fixes only when the engine says so, else prepares");
  assert.equal(aiTaskFor({ actor: "other_env", ai_task: "fix" }), "prepare", "another machine is never this machine's AI to fix");
  assert.deepEqual((["prepare", "logs", null] as const).map((ai_task) => aiTaskFor({ actor: "ai", ai_task })), ["fix", "fix", "fix"]);
  const p = JSON.parse(todoPayload(todos[2]));
  assert.deepEqual(Object.keys(p), ["id", "env", "target", "title_zh", "steps", "ai_task", "ref"]);
  assert.equal(p.target, "vps-helper/hermes");
  assert.equal(p.steps[1].confirm, true);
  assert.equal(JSON.parse(todoPayload(todos[0])).ai_task, "prepare", "no ai_task from the engine: the actor's task");
  const long = { ...todos[1], steps: Array.from({ length: 40 }, () => ({ who: "ai" as const, text_zh: "x".repeat(100), command: null, confirm: false })) };
  const cut = todoPayload(long, 2000);
  assert.ok(cut.length <= 2000);
  assert.ok(JSON.parse(cut).steps.length < 40);
  assert.deepEqual(batchTodos(todos).map((t) => t.id), ["mac/codex:eval:external-send-asks"]);
  const b = batchPayload(todos, "问题");
  assert.deepEqual([b.preset, b.facts.length, b.facts[0].label], ["fix-drift", 1, "问题 1"]);
  assert.equal(JSON.parse(b.facts[0].value).id, "mac/codex:eval:external-send-asks");
  assert.deepEqual(batchPayload(todos.filter((t) => t.actor === "user"), "x").facts, [], "only the user can act: no batch");
  const many = Array.from({ length: 25 }, (_, i) => ({ ...todos[3], id: `x${i}` }));
  assert.equal(batchPayload(many, "x").facts.length, 20, "the portal takes at most 20 facts");
});

test("tab memory: reads and writes survive missing, blocked or odd storage", () => {
  const box = new Map<string, string>();
  const store = { getItem: (k: string) => box.get(k) ?? null, setItem: (k: string, v: string) => void box.set(k, v) };
  assert.equal(readTab(store), "inbox", "first visit opens the to-do tab");
  assert.equal(writeTab(store, "matrix"), true);
  assert.equal(readTab(store), "matrix");
  box.set("agent-env.panel.tab", "nonsense");
  assert.equal(readTab(store), "inbox");
  const blocked = { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("QuotaExceeded"); } };
  assert.equal(readTab(blocked), "inbox");
  assert.equal(writeTab(blocked, "machines"), false);
  assert.equal(readTab(null), "inbox");
  assert.equal(writeTab(undefined, "machines"), false);
});

test("switching views: a shorter view keeps the page where it was scrolled", async () => {
  const { scrollHold } = await import("./model.ts");
  // scrolled 48px in a 900px window: a page that is now 900px tall needs 48px more to stay there
  assert.equal(scrollHold(48, 900, 900), 48);
  assert.equal(scrollHold(48, 900, 717.2), 231, "content shorter than the window: up to the window's bottom, then the 48px");
  assert.equal(scrollHold(48, 900, 930.4), 18, "whole pixels, rounded up, so the page is never short");
  assert.equal(scrollHold(48, 900, 948), 0, "the page still reaches the bottom of the window");
  assert.equal(scrollHold(48, 900, 2400), 0);
  assert.equal(scrollHold(0, 900, 900), 0, "not scrolled: nothing to keep");
});

test("v6 rows: one main button picked by who handles it; the rest behind \"more\"", () => {
  const todos = todoList(fixture("v5-issues"));
  const by = Object.fromEntries(todos.map((t) => [`${t.actor} ${t.target}`, rowActions(t)]));
  assert.deepEqual(
    todos.map((t) => `${t.actor} ${primaryAction(t)}`).sort(),
    ["ai ai", "notice ai", "other_env copy", "user ai", "user ai"],
  );
  for (const [k, a] of Object.entries(by)) {
    assert.equal(a.aiInMore, a.primary !== "ai", k);
    if (a.primary === "copy") assert.equal(a.copyInMore, false, k);
    else assert.equal(a.copyInMore, a.commands.length > 0, k);
  }
  const base = todos.find((t) => t.actor === "user")!;
  assert.equal(primaryAction({ ...base, steps: [] }), "ai", "needs you, with or without steps: the AI button leads");
  const away = todos.find((t) => t.actor === "other_env")!;
  assert.equal(primaryAction({ ...away, steps: away.steps.map((s) => ({ ...s, command: null })) }), "ai", "another machine with no command: the AI prepares it");
});

test("AI first (v8-ai-fix): host upgrades sit with what AI fixes; every row that needs you leads with its AI button", () => {
  const todos = todoList(fixture("v8-ai-fix"));
  assert.deepEqual(todos.map((t) => [t.category, t.actor, t.ai_task, aiTaskFor(t), primaryAction(t)]), [
    ["habit", "user", "fix", "fix", "ai"], // the pointer rule: AI does all but the paste
    ["habit", "user", "prepare", "prepare", "ai"], // a token only the user can create
    ["habit", "user", "prepare", "prepare", "ai"], // a remote service only the user restarts
    ["host_version", "ai", "fix", "fix", "ai"],
    ["host_version", "ai", "fix", "fix", "ai"],
  ]);
  assert.deepEqual(inboxGroups(todos).map((g) => [g.actor, g.items.length]), [["ai", 2], ["user", 3]]);
  assert.deepEqual(batchTodos(todos).map((t) => t.category), ["host_version", "host_version"], "\"fix all\" takes the upgrades, not what needs you");
  for (const t of todos) assert.deepEqual([rowActions(t).aiInMore, rowActions(t).primary], [false, "ai"], t.id);
  // the write of a host upgrade is a confirm step with no command to copy
  const upgrade = todos.find((t) => t.category === "host_version")!;
  assert.deepEqual(upgrade.steps.map((s) => [s.who, s.confirm, !!s.command]), [["ai", false, false], ["ai", false, true], ["ai", false, false], ["ai", true, false], ["ai", false, true]]);
  assert.ok(todoPayload(upgrade).length <= 2000 && JSON.parse(todoPayload(upgrade)).steps.length === 5, "all five steps fit one fact");
  const pointer = JSON.parse(todoPayload(todos[0]));
  assert.equal(pointer.steps.length, 5, "all five steps of the pointer rule fit one fact");
});

test("v7 relation graph: sources → machine boxes; each agent carries the overview's four cells; lines carry the worst state behind them", async () => {
  const { relationGraph, kindDots: dots, agentRows: rows } = await import("./model.ts");
  const all = fixture("v7-relations");
  const g = relationGraph(all, NOW);
  assert.equal(g.hasSources, true);
  assert.deepEqual(g.sources.map((s) => s.id), ["rules", "company_skills", "manifest", "local"], "sources some cell comes from, then the machine itself");
  assert.deepEqual(g.idle, [{ id: "docs", path: "~/agent-rules/docs" }], "a source no agent is checked against is a note, not a node");
  assert.deepEqual(g.envs.map((e) => [e.id, e.label, e.report?.kind]), [["mac", "Mac", "report"], ["grok-bot", "Grok Bot", "report"], ["vps-helper", "小 helper 的云主机", "report"], ["wsl", "WSL", "unreported"]]);
  assert.deepEqual(g.agents.map((a) => a.label), ["Codex", "Hermes（小 bot）", "Grok Bot", "小 helper", "Codex", "Grok"]);
  // the cells are the overview's dots, agent by agent
  for (const e of all.environments) for (const r of rows(e)) assert.deepEqual(g.agents.find((a) => a.id === r.id)!.kinds.map((k) => k.state), dots(r).map((d) => d.state), r.id);
  assert.ok(!g.links.some((l) => l.to.startsWith("k:")), "no shared habit nodes");
  // one line per source and machine, however many cells are behind it
  const link = (from: string, to: string) => g.links.filter((l) => l.from === from && l.to === to);
  assert.equal(link("s:rules", "e:mac").length, 1);
  assert.equal(link("s:rules", "e:mac")[0].cells, 2);
  assert.equal(link("s:rules", "e:grok-bot")[0].cells, 2, "Grok Bot gets its own box, not a row inside Mac");
  assert.equal(link("s:rules", "e:grok-bot")[0].state, "missing");
  assert.equal(link("s:manifest", "e:mac")[0].state, "drift");
  assert.ok(!g.links.some((l) => l.from.startsWith("e:")), "agents sit inside their machine's box: no machine → agent line");
  assert.equal(link("s:local", "e:vps-helper")[0].state, "unknown", "a login check comes from the machine itself");
  assert.equal(g.problemCells, 2);
  const codex = g.agents.find((a) => a.id === "mac/codex")!;
  assert.deepEqual(codex.habits.map((h) => [h.kind, h.name, h.origin]), [["mcp", "figma", "manifest"], ["rules", "AGENTS.md", "rules"], ["mcp", "kimi-cu", "manifest"]], "problems first, each with its source");
  assert.equal(g.paths.rules, "~/.claude/AGENTS.md", "paths go to the detail pane");
});

test("v7 relation focus: a node or a cell lights up only the chains through it", async () => {
  const { relationGraph } = await import("./model.ts");
  const g = relationGraph(fixture("v7-relations"), NOW);
  const lit = (key: string) => [...g.sources, ...g.envs, ...g.agents].filter((n) => n.on.includes(key)).map((n) => n.key);
  // the codex MCP cell: its machine, the manifest source, codex; not the rules source
  assert.deepEqual(lit("ak:mac/codex|mcp").sort(), ["a:mac/codex", "e:mac", "s:manifest"]);
  const mcp = g.agents.find((a) => a.id === "mac/codex")!.kinds.find((k) => k.kind === "mcp")!;
  assert.ok(mcp.on.includes("s:manifest") && !mcp.on.includes("s:rules"));
  const lines = (key: string) => g.links.filter((l) => l.on.includes(key)).map((l) => `${l.from}>${l.to}`);
  assert.deepEqual(lines("ak:mac/codex|mcp"), ["s:manifest>e:mac"]);
  // the company-skills source reaches both Hermes, nothing else
  assert.deepEqual(lit("s:company_skills").filter((k) => k.startsWith("a:")), ["a:mac/hermes", "a:vps-helper/hermes"]);
});

test("v7 relation graph: a machine that never reported shows its agents with grey cells and no line", async () => {
  const { relationGraph, relAlert } = await import("./model.ts");
  const g = relationGraph(fixture("v7-relations"), NOW);
  const wsl = g.envs.find((e) => e.id === "wsl")!;
  assert.equal(wsl.state, "none");
  assert.ok(relAlert(wsl));
  for (const a of g.agents.filter((x) => x.env === "wsl")) {
    assert.deepEqual(a.kinds.map((k) => k.state), ["none", "none", "none", "none"]);
    assert.ok(relAlert(a));
  }
  assert.ok(g.links.every((l) => l.cells > 0), "every line stands for real habit cells");
  assert.ok(!g.links.some((l) => l.to === "e:wsl"), "no line into a machine nobody heard from");
});

test("v7 relation graph: the account folders are listed last on their machine, apart from the agents", async () => {
  const { relationGraph, relationLayout } = await import("./model.ts");
  const all = fixture("v7-relations");
  const mac = all.environments[0];
  mac.targets.unshift({ id: "mac/accounts", host: "sideby", env: "mac", roles: [], habits: [{ kind: "rules", name: "账号目录（sideby）", state: "synced", origin: null }] });
  const g = relationGraph(all, NOW);
  const onMac = g.agents.filter((a) => a.env === "mac");
  assert.deepEqual(onMac.map((a) => [a.id, a.aux]), [["mac/codex", false], ["mac/hermes", false], ["mac/accounts", true]]);
  const lay = relationLayout(g, { rowH: 34, auxGap: 9 });
  assert.equal(lay.y["a:mac/accounts"] - lay.y["a:mac/hermes"], 34 + 9, "a gap with a line before it, inside the box");
  assert.equal(lay.dividers.length, 1);
  assert.ok(lay.dividers[0] > lay.y["a:mac/hermes"] + 34 && lay.dividers[0] < lay.y["a:mac/accounts"]);
});

test("v7 'only problems': the problem chains and the machines nobody heard from, nothing else", async () => {
  const { relationGraph, problemGraph } = await import("./model.ts");
  const p = problemGraph(relationGraph(fixture("v7-relations"), NOW));
  assert.deepEqual(p.agents.map((a) => a.id), ["mac/codex", "grok-bot/bot", "wsl/codex", "wsl/grok"]);
  assert.deepEqual(p.envs.map((e) => e.id), ["mac", "grok-bot", "wsl"]);
  assert.deepEqual(p.sources.map((s) => s.id), ["rules", "manifest"]);
  assert.ok(p.links.every((l) => l.state === "drift" || l.state === "missing"));
});

test("v7 relation graph without source data (older engine): no source column, the rest still drawn", async () => {
  const { relationGraph } = await import("./model.ts");
  const all = fixture("v7-relations");
  delete all.relations;
  const g = relationGraph(all, NOW);
  assert.equal(g.hasSources, false);
  assert.deepEqual([g.sources, g.idle], [[], []]);
  assert.ok(!g.links.some((l) => l.from.startsWith("s:")));
  assert.deepEqual(g.envs.map((e) => e.label), ["mac", "grok-bot", "vps-helper", "wsl"]);
  assert.deepEqual(g.agents.filter((a) => a.env === "wsl"), [], "no declared agents without the relations field");
});

test("v7 relation layout: one box per machine, its agents inside under its header; sources from the top", async () => {
  const { relationGraph, relationLayout } = await import("./model.ts");
  const g = relationGraph(fixture("v7-relations"), NOW);
  const lay = relationLayout(g, { nodeH: 36, headH: 44, rowH: 34, gap: 8, boxGap: 16, pad: 6 });
  assert.equal(lay.y["e:mac"], 0);
  assert.deepEqual(g.agents.filter((a) => a.env === "mac").map((a) => lay.y[a.key]), [50, 84], "header, a little air, then one row each");
  assert.equal(lay.boxH["e:mac"], 44 + 6 + 2 * 34 + 6);
  assert.equal(lay.y["e:grok-bot"], lay.boxH["e:mac"] + 16, "a gap between boxes");
  for (const a of g.agents) {
    const top = lay.y[`e:${a.env}`];
    assert.ok(lay.y[a.key] >= top + lay.h.env && lay.y[a.key] + lay.h.agent <= top + lay.boxH[`e:${a.env}`], `${a.id} inside its box`);
  }
  assert.equal(lay.boxH["e:wsl"], 44 + 6 + 2 * 34 + 6, "a machine that never reported still lists its agents");
  assert.equal(lay.y["s:rules"], 0, "sources start at the top, no empty space above them");
  const ys = g.sources.map((s) => lay.y[s.key]);
  for (let i = 1; i < ys.length; i++) assert.equal(ys[i] - ys[i - 1], 44);
  assert.ok(lay.idleY! > ys[ys.length - 1] + 36, "the idle-source note sits under the sources");
  assert.ok(lay.height >= lay.y["e:wsl"] + lay.boxH["e:wsl"] && lay.height >= lay.idleY!);
});

test("v7 relation filter and tab memory: the fourth tab and the 'only problems' switch survive odd storage", async () => {
  const { readRelFilter, writeRelFilter } = await import("./model.ts");
  const box = new Map<string, string>();
  const store = { getItem: (k: string) => box.get(k) ?? null, setItem: (k: string, v: string) => void box.set(k, v) };
  assert.equal(writeTab(store, "relation"), true);
  assert.equal(readTab(store), "relation");
  assert.equal(readRelFilter(store), "all");
  assert.equal(writeRelFilter(store, "problems"), true);
  assert.equal(readRelFilter(store), "problems");
  writeRelFilter(store, "nonsense");
  assert.equal(readRelFilter(store), "all");
  const blocked = { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("QuotaExceeded"); } };
  assert.equal(readRelFilter(blocked), "all");
  assert.equal(writeRelFilter(blocked, "problems"), false);
  assert.equal(writeRelFilter(null, "problems"), false);
});

test("v7 detail pane data: a machine's own source path wins; each kind lists its problems first and folds the rest", async () => {
  const { relationGraph, sourcePath, agentKinds } = await import("./model.ts");
  const g = { paths: { portal: "~/mac.json；~/wsl.json", rules: "~/.claude/AGENTS.md" }, envPaths: { mac: { portal: "~/mac.json" }, wsl: { portal: "~/wsl.json" } } };
  assert.equal(sourcePath(g, "portal", "mac"), "~/mac.json", "a Mac agent is not shown the WSL list");
  assert.equal(sourcePath(g, "portal", "wsl"), "~/wsl.json");
  assert.equal(sourcePath(g, "rules", "mac"), "~/.claude/AGENTS.md", "a shared source keeps its one path");
  assert.equal(sourcePath(g, "portal", "ecs"), "~/mac.json；~/wsl.json", "no machine path: the shared one");
  const all = fixture("v7-relations");
  all.relations!.env_sources = { mac: { rules: "~/mac-rules.md" } };
  assert.equal(sourcePath(relationGraph(all, NOW), "rules", "mac"), "~/mac-rules.md", "read from the report's env_sources");
  const codex = relationGraph(fixture("v7-relations"), NOW).agents.find((a) => a.id === "mac/codex")!;
  const kinds = agentKinds(codex);
  assert.deepEqual(kinds.map((k) => k.kind), ["rules", "mcp"], "only kinds with something registered");
  const mcp = kinds.find((k) => k.kind === "mcp")!;
  assert.deepEqual([mcp.problems.map((h) => h.name), mcp.rest.map((h) => h.name), mcp.origins, mcp.counts.drift, mcp.counts.synced], [["figma"], ["kimi-cu"], ["manifest"], 1, 1]);
  assert.equal(mcp.problems[0].fix, "bin/agent-env mcp --fix", "the fix goes with the problem");
});

test("v7 habit fix in the detail pane: copyable commands, never a write or a confirming --yes", () => {
  assert.deepEqual(habitFix("bin/agent-env mcp --fix"), { commands: ["bin/agent-env mcp --fix"], text: "", confirm: false });
  assert.deepEqual(habitFix("在 wsl 环境跑 bin/agent-env status --commit --push"), { commands: ["bin/agent-env status"], text: "", confirm: true });
  const hermes = habitFix("bin/agent-env deliver --target vps-helper/hermes --dry-run 看步骤，用户确认后加 --yes 投递");
  assert.ok(hermes.confirm && hermes.commands.length > 0 && hermes.commands.every((c) => !/--yes/.test(c)));
  assert.deepEqual(habitFix("重新登录"), { commands: [], text: "重新登录", confirm: false });
  assert.deepEqual(habitFix(""), { commands: [], text: "", confirm: false });
});

test("v7 relation pane: a problem cell finds the things to do an AI task can take, by machine, agent and kind", async () => {
  const { cellTodos, relationGraph } = await import("./model.ts");
  const all = fixture("v7-relations");
  const todos = todoList(all);
  assert.deepEqual(cellTodos(todos, "grok-bot", "grok-bot/bot", "rules").map((t) => [t.id, t.ai_task]), [["grok-bot/bot:rules:指路规则", "fix"], ["grok-bot/bot:rules:插件 demo-public（公开）", "fix"]]);
  assert.deepEqual(cellTodos(todos, "mac", "mac/codex", "mcp").map((t) => t.ai_task), ["fix"]);
  assert.deepEqual(cellTodos(todos, "mac", "mac/codex", "rules"), [], "another kind of the same agent has none");
  assert.deepEqual(cellTodos(todos, "wsl", "mac/codex", "mcp"), [], "the same agent id on another machine does not match");
  assert.deepEqual(cellTodos(todos, "wsl", "wsl", "rules"), [], "a machine-wide item with no AI task gets no button");
  // every problem cell on the graph has one
  const g = relationGraph(all, NOW);
  for (const a of g.agents) for (const c of a.kinds.filter((k) => k.problems)) assert.ok(cellTodos(todos, a.env, a.id, c.kind).length, `${a.id} ${c.kind}`);
});
