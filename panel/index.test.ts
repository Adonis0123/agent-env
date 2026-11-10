import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, request } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { createPanelHandler, type PanelOptions } from "./index.ts";

const NOW = Date.parse("2026-11-09T08:00:00Z");
const FIX = (name: string) => join(import.meta.dirname, "fixtures", `${name}.json`);
const servers: Array<{ close: () => void }> = [];
after(() => {
  for (const s of servers) s.close();
});

async function serve(opts: Omit<PanelOptions, "allowedHosts">) {
  let handler: ReturnType<typeof createPanelHandler>;
  const server = createServer((req, res) => {
    handler(req, res).then((handled) => {
      if (!handled) {
        res.statusCode = 404;
        res.end("outside basePath");
      }
    });
  });
  servers.push(server);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const host = `127.0.0.1:${(server.address() as AddressInfo).port}`;
  handler = createPanelHandler({ allowedHosts: [host], lang: "en", now: () => NOW, ...opts });
  const get = (path: string, o: { method?: string; host?: string; headers?: Record<string, string> } = {}) =>
    new Promise<{ status: number; headers: Record<string, string | string[] | undefined>; body: string }>((resolve, reject) => {
      const port = (server.address() as AddressInfo).port;
      const req = request({ host: "127.0.0.1", port, path, method: o.method ?? "GET", headers: { Host: o.host ?? host, ...o.headers } }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (body += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body }));
      });
      req.on("error", reject);
      req.end();
    });
  const fragment = async (path = "/fragment") => (await get(path, { headers: { "x-env-fragment": "1" } })).body;
  return { get, fragment };
}

const script = (body: string) => {
  const dir = mkdtempSync(join(tmpdir(), "env-panel-"));
  const file = join(dir, "agent-env");
  writeFileSync(file, `#!/bin/sh\n${body}\n`);
  chmodSync(file, 0o755);
  return file;
};

test("host check, read-only methods, health and CSP", async () => {
  const { get } = await serve({ statusFile: FIX("normal") });
  assert.equal((await get("/", { host: "evil.example:80" })).status, 403);
  const post = await get("/", { method: "POST" });
  assert.equal(post.status, 405);
  assert.equal(post.headers.allow, "GET, HEAD");
  assert.deepEqual(JSON.parse((await get("/api/health")).body), { app: "agent-env", version: "0.1.0" });
  const page = await get("/");
  assert.equal(page.status, 200);
  assert.match(String(page.headers["content-security-policy"]), /script-src 'nonce-[^']+'/);
  assert.match(page.body, /<html lang="en" data-lt-theme="auto">/);
  assert.match(page.body, /\/_shared\/local-tools\.css/);
  assert.match(page.body, /<noscript>/, "the shell links to the content when scripts are off");
});

test("basePath: requests outside it are left to the portal", async () => {
  const { get } = await serve({ statusFile: FIX("normal"), basePath: "/panel" });
  assert.equal((await get("/elsewhere")).body, "outside basePath");
  assert.equal((await get("/panel/api/health")).status, 200);
});

const FIX5 = (name: string) => FIX(`v5-${name}`);
const NOW5 = Date.parse("2026-11-09T10:00:00Z");
/** The things to do as rendered in one tab panel: [title, actor] per article. */
const rendered = (html: string, panel: string) => {
  const start = html.indexOf(`id="panel-${panel}"`);
  const end = html.indexOf('<section class="tabpanel"', start + 1);
  const part = html.slice(start, end < 0 ? undefined : end);
  return [...part.matchAll(/<article class="todo" data-todo="([^"]+)" data-actor="(\w+)">.*?<h3 class="todo-title">(.*?)<\/h3>/gs)].map((m) => `${m[2]} | ${m[3]}`);
};

test("normal: everything in order says so in every tab; no buttons to fix anything", async () => {
  const { fragment } = await serve({ statusFile: FIX("normal") });
  const html = await fragment();
  assert.match(html, /<header class="top"><h1>Environments<\/h1><p class="headline is-ok" role="status" data-count-total="0">.*?Everything is fine<\/span><\/p><div class="top-acts"><button[^>]*data-refresh>Refresh<\/button><\/div><\/header>/s);
  assert.match(html, /<p class="rest-ok">.*?last check today \d\d:\d\d/s);
  assert.match(html, /<footer class="foot"><p class="fresh"[^>]*><span class="fresh-label">Last report:<\/span><span class="" title="Last report [^"]+">mac today(?: \d\d:\d\d)?<\/span><span class="sep" aria-hidden="true">·<\/span><span class="" [^>]*>wsl today(?: \d\d:\d\d)?<\/span><\/p>/);
  assert.match(html, /data-tab="inbox">To do<\/button>/, "no count on the tab when there is nothing to do");
  assert.doesNotMatch(html, /<article class="todo"|data-ask|data-ask-all|batch-notes/);
  assert.match(html, /Preview data from/);
});

test("stale: the engine's env_stale issue is a thing to do, said once: the footer leaves it out, its card is marked", async () => {
  const { fragment } = await serve({ statusFile: FIX5("stale"), now: () => NOW5 });
  const html = await fragment();
  const foot = /<footer class="foot">(.*?)<\/footer>/s.exec(html)![1];
  assert.match(foot, /<span class="" [^>]*>mac today(?: \d\d:\d\d)?<\/span>/);
  assert.doesNotMatch(foot, /vps-helper/);
  assert.deepEqual(rendered(html, "inbox"), ["other_env | 云主机 vps-helper 已 9 天没上报"]);
  assert.match(html, /<article class="machine" data-env="vps-helper">.*?<span class="is-stale">Reported [^<]*\(9 days ago\)<\/span>/s);
});

test("drift: the overview's \"only problems\" figure counts things to do, the same number as the to-do tab", async () => {
  const { fragment } = await serve({ statusFile: FIX("drift") });
  const html = await fragment();
  const inbox = Number(/data-count-inbox="(\d+)"/.exec(html)![1]);
  const only = Number(/data-filter="problems" data-problems="(\d+)">Only the (\d+) problems</.exec(html)![1]);
  assert.ok(inbox > 0);
  assert.equal(only, inbox);
  // no copy button anywhere hands out a status write that skipped the read-only check
  for (const m of html.matchAll(/data-copy="([^"]*)"/g)) assert.doesNotMatch(m[1], /status[^\n]*--(commit|push)/, m[1]);
});

test("v5: three tabs, the same five things to do in each with the same title and actor, and agreeing counts", async () => {
  const { fragment } = await serve({ statusFile: FIX5("issues"), now: () => NOW5, ask: { bridge: "postMessage" } });
  const html = await fragment();
  // tabs: shared .lt-tabs, "to do" selected first, a sentence under each
  assert.match(html, /<nav class="lt-tabs" role="tablist"[^>]*><button type="button" class="lt-tab" role="tab" id="tab-inbox" aria-controls="panel-inbox" aria-selected="true"[^>]*>To do <span class="inbox-count num">5<\/span><\/button><button[^>]*id="tab-machines"[^>]*aria-selected="false"[^>]*>By machine<\/button><button[^>]*id="tab-matrix"[^>]*>Overview<\/button><button[^>]*id="tab-relation"[^>]*>Relations<\/button><button[^>]*id="tab-plugins"[^>]*>Plugins<\/button><span class="view-ink" aria-hidden="true"><\/span><\/nav>/);
  assert.doesNotMatch(html, /view-lede|class="legend"|<aside/, "no sentence under the tabs, no legend bar, no glossary column");
  assert.match(html, /id="panel-machines" aria-labelledby="tab-machines" hidden>/);
  // the inbox, grouped by actor in order
  const inbox = rendered(html, "inbox");
  assert.deepEqual(inbox, [
    "ai | Codex 在「对外发送前先问你」抽测里没守规则",
    "user | Grok Bot 的指路规则还没投递",
    "user | 云主机上的小 helper（Hermes）缺少规则文件 AGENTS.md",
    "other_env | WSL 没有上报状态",
    "notice | Claude Code 有 2 个抽测没测成",
  ]);
  assert.deepEqual([...html.matchAll(/<section class="group" data-group="(\w+)">/g)].map((m) => m[1]), ["ai", "user", "other_env", "notice"]);
  // same set in the other two tabs
  const sorted = (x: string[]) => [...x].sort();
  assert.deepEqual(sorted(rendered(html, "machines")), sorted(inbox));
  assert.deepEqual(sorted(rendered(html, "matrix")), sorted(inbox));
  // the group label says who handles it once; the rows under it leave the actor icon out
  const inboxPart = html.slice(html.indexOf('id="panel-inbox"'), html.indexOf('id="panel-machines"'));
  assert.match(inboxPart, /<h2 class="group-h">AI 能修<span class="num"> · 1<\/span><\/h2>/);
  assert.doesNotMatch(inboxPart, /class="who /);
  // counts: headline, inbox, each card's "to do", the overview's total and "only problems"
  assert.match(html, /data-count-total="5"/);
  assert.match(html, /data-count-inbox="5" data-count-machines="5" data-count-matrix="5"/);
  const cards = [...html.matchAll(/<article class="machine" data-env="([^"]+)">(.*?)<\/article>(?=<article class="machine"|<\/div><\/section>)/gs)].map((m) => [m[1], [...m[2].matchAll(/<article class="todo"/g)].length]);
  assert.deepEqual(cards, [["mac", 2], ["grok-bot", 1], ["vps-helper", 1], ["wsl", 1]]);
  assert.match(html, /<article class="machine" data-env="grok-bot">.*?<span>cloud-hosted, checked live from mac<\/span>/s, "Grok Bot is checked from the Mac, not on a machine of its own");
  assert.match(html, /<p class="machine-state is-bad" data-count="2">2 to do<span class="muted"> · 6 agents fine<\/span>/);
  assert.match(html, /<p class="machine-state is-bad">Never reported: what its agents are doing is unknown<\/p>/);
  assert.match(html, /data-filter="problems" data-problems="5">Only the 5 problems</);
  // "fine" holds only agents with nothing wrong
  const okNames = /<span class="ok-names">(.*?)<\/span>/.exec(html)![1];
  assert.equal(okNames, "Grok · pi · Cursor · Hermes · WorkBuddy · sideby");
  // one glossary, in a drawer opened from the footer; the CLI's own names only inside a row's folded part
  assert.equal([...html.matchAll(/<dialog class="drawer" id="gloss"/g)].length, 1);
  assert.match(html, /<button type="button" class="linkish" data-gloss[^>]*>Glossary<\/button>/);
  assert.match(html, /漂移<span class="raw">drift<\/span><\/dt><dd>和清单期望的不一样，要修<\/dd>/);
  assert.match(html, /<div class="todo-more" id="in-1" hidden>.*?<p class="raw-line"><span>As named in the CLI<\/span><code>rules · 指路规则 · missing<\/code><\/p><\/div>/s);
  // every row: one main button, then "more"; nothing else outside the folded part
  for (const m of inboxPart.matchAll(/<div class="todo-acts">(.*?)<\/div><div class="todo-more"/gs)) assert.equal([...m[1].matchAll(/<button /g)].length, 2, m[1]);
  assert.deepEqual([...inboxPart.matchAll(/<article class="todo"[^>]*data-actor="(\w+)">.*?<div class="todo-acts"><button [^>]*>([^<]+)</gs)].map((m) => `${m[1]} ${m[2]}`), ["ai Let AI fix it", "user Let AI fix it", "user Let AI fix it", "other_env Copy 2 commands", "notice Let AI read the logs"]);
  // steps with copy, the confirm step shows the dry run only
  assert.match(html, /<li class="step is-confirm" id="in-2-s1">.*?look at the dry run, then confirm.*?<code>bin\/agent-env deliver --target vps-helper\/hermes --dry-run<\/code>/s);
  assert.doesNotMatch(html, /--yes/);
  // what needs you leads with the AI button too; its steps are behind "more", with no "show steps" button of their own
  assert.doesNotMatch(html, /data-confirm=|Show steps/);
  assert.match(inboxPart, /<article class="todo" data-todo="grok-bot\/bot:rules:指路规则" data-actor="user">.*?<div class="todo-acts"><button type="button" class="lt-btn lt-btn-sm" data-ask="fix-drift"[^>]*>Let AI fix it<\/button><button [^>]*aria-controls="in-1" data-toggle="in-1"/s);
});

test("v5 AI buttons: one per thing to do, labelled by actor, carrying only that issue; no batch button for a single AI fix", async () => {
  const { fragment } = await serve({ statusFile: FIX5("issues"), now: () => NOW5, ask: { bridge: "postMessage" } });
  const html = await fragment();
  const inbox = html.slice(html.indexOf('id="panel-inbox"'), html.indexOf('id="panel-machines"'));
  const buttons = [...inbox.matchAll(/<article class="todo" data-todo="[^"]+" data-actor="(\w+)">.*?data-ask="([\w-]+)" data-fact="(fact-\d+)"[^>]*>([^<]+)</gs)].map((m) => `${m[1]} ${m[2]} ${m[4]}`);
  assert.deepEqual(buttons, ["ai fix-drift Let AI fix it", "user fix-drift Let AI fix it", "user fix-drift Let AI fix it", "other_env prepare-step Let AI prepare it", "notice check-logs Let AI read the logs"]);
  const tabs3 = html.slice(0, html.indexOf('id="panel-relation"'));
  assert.equal([...tabs3.matchAll(/data-ask="/g)].length, 15, "five things to do, a button each, in the first three tabs");
  const rel = html.slice(html.indexOf('id="panel-relation"'));
  assert.ok([...rel.matchAll(/data-ask="/g)].length > 0, "the relation pane offers the same AI buttons next to problem cells");
  const fact = JSON.parse(/<textarea id="fact-3" hidden readonly>(.*?)<\/textarea>/s.exec(html)![1].replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">"));
  assert.deepEqual(Object.keys(fact), ["id", "env", "target", "title_zh", "steps", "ai_task", "ref"]);
  assert.equal(fact.target, "mac/codex");
  assert.doesNotMatch(html, /data-ask-all|batch-facts/, "one AI fix would only repeat its row's button");
});

/** v5-issues with a second problem AI can fix by itself, written to a temp file. */
const twoAiFixes = () => {
  const j = JSON.parse(readFileSync(FIX5("issues"), "utf8"));
  const ai = j.issues.find((x: { actor: string }) => x.actor === "ai");
  j.issues.push({ ...ai, id: `${ai.id}-2`, title_zh: "第二个 AI 能修的问题" });
  const file = join(mkdtempSync(join(tmpdir(), "env-panel-")), "status.json");
  writeFileSync(file, JSON.stringify(j));
  return file;
};

test("v5 batch button: shown from two AI fixes, each carried with its own context", async () => {
  const file = twoAiFixes();
  const html = await (await serve({ statusFile: file, now: () => NOW5, ask: { bridge: "postMessage" } })).fragment();
  assert.match(html, /<button type="button" class="lt-btn lt-btn-primary" data-ask-all="fix-drift" data-batch="2"[^>]*>Let AI fix all \(2\)</);
  const batch = JSON.parse(/<script type="application\/json" id="batch-facts">(.*?)<\/script>/s.exec(html)![1]);
  assert.deepEqual(batch.map((f: { value: string }) => JSON.parse(f.value).id), ["mac/codex:eval:external-send-asks", "mac/codex:eval:external-send-asks-2"]);
  const copy = await (await serve({ statusFile: file, now: () => NOW5 })).fragment();
  assert.match(copy, /<button type="button" class="lt-btn" data-copy-from="batch-notes" data-batch="2">Copy fix notes \(2\)</);
  const zh = await (await serve({ statusFile: file, lang: "zh", now: () => NOW5, ask: { bridge: "postMessage" } })).fragment();
  assert.ok(zh.includes("全部让 AI 修复（2）"));
});

test("a confirm step speaks of a dry run only when an earlier step is one", async () => {
  const html = await (await serve({ statusFile: FIX5("real"), now: () => NOW5 })).fragment();
  const article = (id: string) => new RegExp(`<article class="todo" data-todo="${id}[^"]*".*?</article>`, "s").exec(html)![0];
  const local = article("grok-bot/bot");
  assert.match(local, /<li class="step is-confirm"[^>]*>.*?<span class="confirm-note">Run it only after you confirm<\/span>/s);
  assert.doesNotMatch(local, /dry run/);
  assert.match(article("vps-helper/hermes"), /<span class="confirm-note">look at the dry run, then confirm<\/span>/);
  const zh = await (await serve({ statusFile: FIX5("real"), lang: "zh", now: () => NOW5 })).fragment();
  assert.ok(zh.includes("等你确认后再运行") && zh.includes("先看演练再确认"));
});

test("footer: labelled, with the time for a report from today and the age for an older one", async () => {
  const j = JSON.parse(readFileSync(FIX("normal"), "utf8"));
  j.environments[0].generated_at = new Date(NOW - 5 * 60_000).toISOString();
  j.environments[1].generated_at = new Date(NOW - 50 * 3_600_000).toISOString();
  const file = join(mkdtempSync(join(tmpdir(), "env-panel-")), "status.json");
  writeFileSync(file, JSON.stringify(j));
  const fresh = async (lang: "en" | "zh") => /<p class="fresh"[^>]*>(.*?)<\/p>/s.exec(await (await serve({ statusFile: file, lang })).fragment())![1];
  assert.match(await fresh("en"), /^<span class="fresh-label">Last report:<\/span><span class="" title="Last report [^"]+">mac today \d\d:\d\d<\/span>.*>wsl 2 days ago<\/span>$/s);
  assert.match(await fresh("zh"), /^<span class="fresh-label">上次上报：<\/span><span[^>]*>mac 今天 \d\d:\d\d<\/span>.*>wsl 2 天前<\/span>$/s);
});

test("a machine whose single agent is fine says so without a count", async () => {
  const html = await (await serve({ statusFile: FIX5("ok"), now: () => NOW5 })).fragment();
  const card = /<article class="machine" data-env="vps-helper">.*?<\/article>/s.exec(html)![0];
  assert.match(card, /<span>Fine<\/span><\/span><span class="ok-names">/);
  assert.match(html, /<span>All 8 are fine<\/span>/);
  const zh = await (await serve({ statusFile: FIX5("ok"), lang: "zh", now: () => NOW5 })).fragment();
  assert.ok(zh.includes('<span>正常</span></span><span class="ok-names">'));
  assert.ok(!zh.includes("1 个都正常"));
});


test("v5 without the portal's AI bridge: every AI button becomes a copy of the fix notes", async () => {
  const { fragment } = await serve({ statusFile: FIX5("issues"), now: () => NOW5 });
  const html = await fragment();
  assert.doesNotMatch(html, /data-ask/);
  assert.equal([...html.slice(0, html.indexOf('id="panel-relation"')).matchAll(/data-copy-from="fact-\d+"[^>]*>Copy fix notes</g)].length, 15);
  assert.doesNotMatch(html, /data-batch=|batch-notes/);
});

test("v5: no batch button when nothing can go to AI on its own", async () => {
  const { fragment } = await serve({ statusFile: FIX5("remote-error"), now: () => NOW5, ask: { bridge: "postMessage" } });
  const html = await fragment();
  assert.doesNotMatch(html, /data-ask-all|batch-facts/);
  assert.match(html, /data-ask="prepare-step"/, "the machine-level thing to do still gets its own button");
  assert.match(html, /<p class="machine-state is-bad">Could not be read this time<\/p>/);
  assert.match(html, /1 not checked this time<\/span><\/span><span class="ok-names">Hermes</, "an agent on a machine that could not be checked is not listed as fine");
});

test("a report without issues: one \"update the engine\" thing to do, the same in every tab; the panel's glossary", async () => {
  const { fragment } = await serve({ statusFile: FIX("drift"), ask: { bridge: "postMessage" } });
  const html = await fragment();
  // the engine on this machine is old: one item for the whole report, not one per environment
  const expected = ["user | The report comes from an old engine and lists nothing to do"];
  assert.deepEqual(rendered(html, "inbox"), expected);
  assert.deepEqual(rendered(html, "machines"), expected);
  assert.deepEqual(rendered(html, "matrix"), expected);
  assert.match(html, /data-count-total="1"/);
  assert.match(html, /data-count-inbox="1" data-count-machines="1" data-count-matrix="1"/);
  // nothing guessed from the cells in the to-do tab: no per-habit rows, no fix commands lifted out of detail text
  // (the relation tab's detail pane does show each habit's own engine-written fix; that is not a to-do)
  const inbox = html.slice(html.indexOf('id="panel-inbox"'), html.indexOf('id="panel-machines"'));
  assert.ok(inbox.length > 0);
  assert.doesNotMatch(inbox, /mcp sync --target|data-ask="fix-drift"|data-ask-all/);
  assert.match(html, /data-copy="bin\/agent-env status --json --all --fetch"/, "the only command is the read-only check");
  assert.doesNotMatch(html, /status --commit|--push|--env /, "nothing sends the user to another machine or writes a status branch");
  assert.match(html, /Drift<span class="raw">drift<\/span><\/dt>/, "the panel's glossary when the report has none");
  const zh = await (await serve({ statusFile: FIX("drift"), lang: "zh" })).fragment();
  assert.match(zh, /漂移<span class="raw">drift<\/span><\/dt><dd>和清单期望的不一样，要修<\/dd>/);
  assert.ok(zh.includes("状态报告是旧版引擎生成的，列不出待办"));
});

test("portal header bar: the page title is kept for screen readers only", async () => {
  const { get } = await serve({ statusFile: FIX("normal"), theme: { header: "bar" } });
  assert.match((await get("/fragment", { headers: { "x-env-fragment": "1" } })).body, /<header class="top bar"><h1>/);
});

test("empty: no environments shows the next step", async () => {
  const { fragment } = await serve({ statusFile: FIX("empty") });
  const html = await fragment();
  assert.match(html, /No environments yet/);
  assert.match(html, /bin\/agent-env status --json --all/);
});

test("CLI missing: readable error, how to fix, no blank page", async () => {
  const { fragment, get } = await serve({ command: ["/nonexistent/bin/agent-env", "status", "--json", "--all"] });
  const html = await fragment();
  assert.match(html, /data-failure="missing"/);
  assert.match(html, /Could not load the status/);
  assert.match(html, /does not exist/);
  assert.match(html, /How to fix/);
  assert.match(html, /comes with phase P3/);
  assert.equal((await get("/api/status")).status, 502);
});

test("CLI fails: stderr head is shown, at most 8 lines", async () => {
  const bin = script(`i=1; while [ $i -le 12 ]; do echo "trace line $i" >&2; i=$((i+1)); done; exit 3`);
  const { fragment } = await serve({ command: [bin, "status", "--json", "--all"] });
  const html = await fragment();
  assert.match(html, /data-failure="exit"/);
  assert.match(html, /exited with code 3/);
  assert.match(html, /trace line 8/);
  assert.doesNotMatch(html, /trace line 9/);
});

test("CLI prints something that is not JSON", async () => {
  const bin = script(`echo "Usage: agent-env doctor"`);
  const { fragment } = await serve({ command: [bin] });
  const html = await fragment();
  assert.match(html, /data-failure="not-json"/);
  assert.match(html, /Usage: agent-env doctor/);
});

test("CLI exits non-zero but prints the status: the status is shown", async () => {
  const bin = script(`cat '${FIX("drift")}'; exit 1`);
  const { fragment } = await serve({ command: [bin] });
  assert.match(await fragment(), /1 thing to handle/);
});

test("slow CLI (25 s) still answers under the default timeout", { timeout: 60_000 }, async () => {
  const bin = script(`sleep 25; cat '${FIX("drift")}'`);
  const { fragment } = await serve({ command: [bin] });
  assert.match(await fragment(), /1 thing to handle/);
});

test("timeoutMs is configurable: a CLI slower than it shows the timeout panel", async () => {
  const bin = script(`exec sleep 5`);
  const { fragment } = await serve({ command: [bin], timeoutMs: 300 });
  const html = await fragment();
  assert.match(html, /data-failure="timeout"/);
  assert.match(html, /no answer within/);
});

test("Chinese strings", async () => {
  const { fragment } = await serve({ statusFile: FIX5("issues"), lang: "zh", now: () => NOW5, ask: { bridge: "postMessage" } });
  const html = await fragment();
  for (const s of ["环境", "vps-helper 今天", "5 件事要处理", "AI 能修<span class=\"num\"> · 1", "待办", "按机器", "总览", "让 AI 修复", "让 AI 准备", "让 AI 查日志", "先看演练再确认", "待办 <span class=\"inbox-count num\">5</span>", "上次上报：", "复制 2 条命令", "术语说明", "命令行里的原始叫法", "谁来处理", "有问题 5", "其余 6 个正常", "还没上报过"]) assert.ok(html.includes(s), s);
  for (const s of ["全部让 AI 修复（1）", "去确认", "件待办：", "现在要做什么", "这些词是什么意思", "看步骤和命令", "看步骤", "共 5 件待办"]) assert.ok(!html.includes(s), `gone in v6: ${s}`);
  const ok = await serve({ statusFile: FIX5("ok"), lang: "zh", now: () => NOW5 });
  assert.match(await ok.fragment(), /一切正常/);
});

test("every Chinese key exists for every string the page uses", async () => {
  const { ZH } = await import("./i18n.ts");
  const src = (await import("node:fs")).readFileSync(join(import.meta.dirname, "page.ts"), "utf8");
  const used = [...src.matchAll(/\btr?\("([^"]+)"/g)].map((m) => m[1]);
  const missing = used.filter((k) => !(k in ZH));
  assert.deepEqual(missing, []);
});

test("switching views: the tab row carries the page's own underline, and the page script gets the scroll rule", async () => {
  const { get, fragment } = await serve({ statusFile: FIX5("issues"), now: () => NOW5 });
  assert.match(await fragment(), /<\/button><span class="view-ink" aria-hidden="true"><\/span><\/nav>/, "one bar after the tabs");
  const page = (await get("/")).body;
  assert.match(page, /window\.__scrollHold=function scrollHold\(/);
});

test("the panel reuses the portal's shared components instead of restyling them", async () => {
  const { CSS } = await import("./page.ts");
  const css = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  const selectors = [...css.matchAll(/([^{}]+)\{/g)].map((m) => m[1].trim()).filter((sel) => /\.lt-/.test(sel));
  assert.deepEqual(selectors, [], "restyle these through the shared /_shared/local-tools.css instead");
  assert.doesNotMatch(CSS, /^\s*\.(btn|counts|tabs?)\b/m, "use .lt-btn / .lt-chip / .lt-tabs, not a look-alike");
});

test("v7 relation tab: sources and machine boxes, four cells per agent, paths and raw names only in the detail pane or hover text", async () => {
  const { fragment } = await serve({ statusFile: FIX("v7-relations"), lang: "zh", now: () => NOW5, ask: { bridge: "postMessage" } });
  const html = await fragment();
  assert.match(html, /id="tab-relation"[^>]*>关系图<\/button><button[^>]*id="tab-plugins"[^>]*>插件<\/button><span class="view-ink" aria-hidden="true"><\/span><\/nav>/, "the fourth tab, after the overview; the plugins tab last");
  const start = html.indexOf('id="panel-relation"');
  const panel = html.slice(start, html.indexOf("</section></div><footer", start));
  assert.ok(start > 0 && panel.length > 0);
  // the graph and the tree, without the detail panes: no paths, file names or raw state words on screen
  const graph = panel.slice(0, panel.indexOf('<div class="rel-aside"')).replace(/<[^>]+>/g, " ");
  const tree = panel.slice(panel.indexOf('<div class="rel-tree">')).replace(/<div class="rt-detail"[\s\S]*?<\/div><\/li>/g, "").replace(/<[^>]+>/g, " ");
  for (const visible of [graph, tree]) for (const raw of ["~/", "AGENTS.md", "habits.toml", "missing", "drift", "synced", "company_skills", "manifest"]) assert.ok(!visible.includes(raw), `visible: ${raw}`);
  for (const s of ["来源", "机器", "共享规则", "公司技能", "清单登记", "机器自身", "小 helper 的云主机", "Hermes（小 bot）", "还没上报过", "有问题 2", "没上报，状态不明", "不单独核对：规则附带的文档"]) assert.ok(panel.includes(s), s);
  // no shared habit nodes; one line per source and machine
  assert.doesNotMatch(panel, /data-node="k:/);
  const all = panel.slice(panel.indexOf('data-graph="all"'), panel.indexOf('data-graph="problems"'));
  assert.equal([...all.matchAll(/<path [^>]*data-on="[^"]*"/g)].length, new Set([...all.matchAll(/<path class="[^"]*" d="([^"]+)"/g)].map((m) => m[1])).size, "no line drawn twice");
  assert.equal([...all.matchAll(/<div class="rbox[^"]*"/g)].length, 4, "one box per machine, Grok Bot its own");
  assert.match(all, /<div class="rbox is-unreported"/, "the machine that never reported: a dashed grey box");
  assert.doesNotMatch(all, /data-on="[^"]*" *\/><path[^>]*e:wsl|<path [^>]*data-on="[^"]*e:wsl/, "no line into it");
  // an agent: its name pins it, its four cells pin a kind; the problem cell shows its count
  assert.match(all, /<div class="rn n-agent s-missing is-alert"[^>]*data-node="a:grok-bot\/bot"[^>]*><button type="button" class="rn-name" data-focus="a:grok-bot\/bot">Grok Bot<\/button>(<button type="button" class="rc [^"]*" data-focus="ak:grok-bot\/bot\|(rules|skills|hooks|mcp)"[^>]*>.*?<\/button>){4}<\/div>/);
  assert.match(all, /data-focus="ak:grok-bot\/bot\|rules"[^>]*>.*?<span class="num">2<\/span><\/button>/);
  // detail pane: one block per kind, the kind with a problem open, the problem first; the source once per kind, its
  // path only in hover text and "copy path"; synced items folded into one line
  const pane = /<section class="rel-pane" data-pane="a:mac\/codex" data-default-kind="mcp" hidden>([\s\S]*?)<\/section>(?=<section class="rel-pane"|<\/div>)/.exec(panel)![1];
  assert.match(pane, /<button type="button" class="lt-seg-item rp-chip" data-focus="ak:mac\/codex\|mcp" data-keep data-chip="mcp" aria-pressed="true">/);
  assert.match(pane, /<span class="rp-chip is-empty"/, "a kind with nothing registered is not a button");
  assert.match(pane, /<section class="rp-kind" data-kind="mcp"><p class="rp-src">来自 <span class="rp-src-name" tabindex="0" data-tip="~\/coding\/agent-env\/manifest\/habits\.toml">清单登记<\/span><button type="button" class="linkish rp-copy" data-copy="~\/coding\/agent-env\/manifest\/habits\.toml">复制路径<\/button><\/p><ul class="rp-bad"><li><p class="rp-line"><span class="rp-name">figma<\/span>/);
  assert.match(pane, /<details class="rp-rest"><summary><span class="chev" aria-hidden="true"><\/span>1 项[^<]+<\/summary>.*?kimi-cu/s);
  assert.match(pane, /<section class="rp-kind" data-kind="rules" hidden>/, "other kinds wait behind their block");
  assert.ok(!pane.replace(/<[^>]+>/g, " ").includes("~/"), "no path in the pane's text");
  assert.match(pane, /data-goto="mac\/codex" data-goto-env="mac">去「按机器」里看</);
  assert.match(html, /<section class="m-agent" data-agent="grok-bot\/bot">/);
  assert.match(html, /<li data-agent="mac\/hermes">/);
  // the narrow tree holds the same machines and agents, each opening its details
  assert.deepEqual([...panel.matchAll(/class="rt-name" aria-expanded="false" aria-controls="(rt-\d+-\d+)"/g)].length, 6);
});

// ---- review round 1 (panel findings 15, 18, 19, 20) ----

const tmpStatus = (data: unknown) => {
  const file = join(mkdtempSync(join(tmpdir(), "env-panel-")), "status.json");
  writeFileSync(file, JSON.stringify(data));
  return file;
};
const machineCard = (html: string, env: string) => new RegExp(`<article class="machine" data-env="${env}">(.*?)</article>(?=<article class="machine"|</div></section>)`, "s").exec(html)![1];

test("finding 15: a cloud-hosted report is called live only when this run checked it", async () => {
  const data = JSON.parse(readFileSync(FIX("v7-relations"), "utf8"));
  const bot = data.environments.find((e: { env: string }) => e.env === "grok-bot");
  assert.deepEqual([bot.source, bot.checked_from], ["live", "mac"]);
  const live = machineCard(await (await serve({ statusFile: tmpStatus(data), lang: "zh", now: () => NOW })).fragment(), "grok-bot");
  assert.match(live, /<span>云端托管，在 mac 上实时体检<\/span>/);
  bot.source = "branch"; // the reviewer's input: read from a status branch, checked from the Mac some time ago
  const past = machineCard(await (await serve({ statusFile: tmpStatus(data), lang: "zh", now: () => NOW })).fragment(), "grok-bot");
  assert.match(past, /<span>云端托管，mac 上次上报的结果<\/span>/);
  assert.doesNotMatch(past, /实时体检/);
  const en = machineCard(await (await serve({ statusFile: tmpStatus(data), now: () => NOW })).fragment(), "grok-bot");
  assert.match(en, /<span>cloud-hosted, from the last report of mac<\/span>/);
  assert.doesNotMatch(en, /checked live/);
});

test("finding 18: two things to do behind one cell get two different buttons", async () => {
  for (const ask of [undefined, { bridge: "postMessage" } as const]) {
    const html = await (await serve({ statusFile: FIX("v7-relations"), lang: "zh", now: () => NOW, ...(ask ? { ask } : {}) })).fragment();
    const start = html.indexOf('id="panel-relation"');
    const panel = html.slice(start, html.indexOf("</section></div><footer", start));
    const groups = [...panel.matchAll(/<li class="rp-li[^"]*">(.*?)<\/li>/gs)].map((m) => [...m[1].matchAll(/<button type="button" class="lt-btn[^"]*"[^>]*>([^<]*)<\/button>/g)].map((b) => b[1]));
    const bot = groups.filter((g) => g.length === 2);
    assert.ok(bot.length >= 1, "the Grok Bot rules cell lists both of its things to do");
    for (const g of groups) assert.equal(new Set(g).size, g.length, `no two buttons with the same label side by side: ${g.join(" | ")}`);
    for (const g of bot) assert.deepEqual(g.map((label) => label.split("：")[1]), ["指路规则", "插件 demo-public（公开）"]);
    assert.ok(groups.some((g) => g.length === 1 && !g[0].includes("：")), "a cell with one thing to do keeps the plain label");
  }
});

test("findings 19 and 20: the Grok Bot fixture todo is what the engine emits, and names one command everywhere", async () => {
  for (const name of ["v5-issues", "v5-real", "v7-relations"]) {
    const html = await (await serve({ statusFile: FIX(name), lang: "zh", now: () => NOW })).fragment();
    assert.match(html, /Grok Bot 的指路规则还没投递/, name);
    assert.doesNotMatch(html, /Grok Bot的/, name);
    assert.doesNotMatch(html, /agent-env deliver --target grok-bot\/bot/, `${name}: steps and detail both say plugin pointer`);
    assert.match(html, /bin\/agent-env plugin pointer --target grok-bot\/bot/, name);
  }
});

test("finding G: a row's buttons, one or several, sit on their own wrapping line under the label", async () => {
  const { get, fragment } = await serve({ statusFile: FIX("v7-relations"), lang: "zh", now: () => NOW });
  const html = await fragment();
  const start = html.indexOf('id="panel-relation"');
  const panel = html.slice(start, html.indexOf("</section></div><footer", start));
  const rows = [...panel.matchAll(/<li class="rp-li">(.*?)<\/li>/gs)].map((m) => m[1]);
  const buttons = (body: string) => [...body.matchAll(/<button type="button" class="lt-btn[^"]*"/g)].length;
  assert.deepEqual(rows.map(buttons).sort(), [1, 2], "the Codex MCP row has one button, the Grok Bot rules row two");
  // the row label and its buttons are siblings: label first, then one container holding every button, so a row with
  // one button and a row with two are laid out the same way
  for (const body of rows) assert.match(body, /^<button type="button" class="rp-row" data-focus="[^"]+"><span class="rp-row-l">.*?<\/span>.*?<\/button><div class="rp-li-acts">(<button type="button" class="lt-btn[^"]*"[^>]*>[^<]*<\/button>)+<\/div>$/s);
  assert.doesNotMatch(panel, /is-stack/);
  assert.match(rows.find((b) => buttons(b) === 2)!, /<span class="rp-row-l">.*?Grok Bot · 规则.*?<\/span>/s, "the label is the full text, not shortened");
  // the styles that make it wrap are shipped with the page
  const page = (await get("/")).body + html;
  assert.match(page, /\.rp-li \{ display: flex; flex-wrap: wrap;/);
  assert.match(page, /\.rp-li > \.rp-row \{ flex: 1 0 100%;/);
  assert.match(page, /\.rp-li-acts \{ display: flex; flex-wrap: wrap;[^}]*flex: 1 0 100%;/);
  assert.match(page, /\.rp-li-acts > \* \{ max-width: 100%; \}/);
});

test("AI first: a host upgrade is AI's to fix; what needs you leads with the AI button, never with \"show steps\"", async () => {
  const { fragment } = await serve({ statusFile: FIX("v8-ai-fix"), lang: "zh", now: () => NOW5, ask: { bridge: "postMessage" } });
  const html = await fragment();
  const inbox = html.slice(html.indexOf('id="panel-inbox"'), html.indexOf('id="panel-machines"'));
  // [group, title, main button's preset and label, number of buttons outside the folded part]
  const rows = [...inbox.matchAll(/<article class="todo" data-todo="[^"]+" data-actor="(\w+)">.*?<h3 class="todo-title">(.*?)<\/h3>.*?<div class="todo-acts"><button type="button" class="lt-btn lt-btn-sm" data-ask="([\w-]+)"[^>]*>([^<]+)<\/button>(.*?)<\/div><div class="todo-more"/gs)].map((m) => [m[1], m[2], m[3], m[4], [...m[5].matchAll(/<button /g)].length]);
  assert.deepEqual(rows, [
    ["ai", "Claude Code 升级到了 2.1.322（清单登记的是 2.1.319）", "fix-drift", "让 AI 修复", 1],
    ["ai", "Pi 升级到了 1.5.1（清单登记的是 1.5.0）", "fix-drift", "让 AI 修复", 1],
    ["user", "Grok Bot 的指路规则还没投递", "fix-drift", "让 AI 修复", 1],
    ["user", "Grok Bot 还没有读私有插件的 token", "prepare-step", "让 AI 准备", 1],
    ["user", "小 helper 的后台服务没在运行", "prepare-step", "让 AI 准备", 1],
  ]);
  assert.deepEqual([...inbox.matchAll(/<h2 class="group-h">([^<]+)<span class="num"> · (\d+)<\/span>/g)].map((m) => `${m[1]} ${m[2]}`), ["AI 能修 2", "要你处理 3"]);
  assert.doesNotMatch(html, /看步骤|data-confirm=/);
  assert.match(html, /data-ask-all="fix-drift" data-batch="2"[^>]*>全部让 AI 修复（2）</, "the batch takes the two host upgrades, not what needs you");
  // the AI task gets the engine's steps as they are: what AI checks, where it stops, which write waits for a yes
  const fact = (id: string) => {
    const key = new RegExp(`<article class="todo" data-todo="${id}"[^>]*>.*?data-fact="(fact-\\d+)"`, "s").exec(inbox)![1];
    return JSON.parse(new RegExp(`<textarea id="${key}" hidden readonly>(.*?)</textarea>`, "s").exec(html)![1].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&"));
  };
  const upgrade = fact("mac/claude-code:host_version:claude-code");
  assert.equal(upgrade.ai_task, "fix");
  assert.deepEqual(upgrade.steps.map((x: { who: string; confirm: boolean }) => `${x.who}${x.confirm ? " confirm" : ""}`), ["ai", "ai", "ai", "ai confirm", "ai"]);
  assert.match(upgrade.steps[0].text_zh, /联网查 Claude Code 从 2\.1\.319 到 2\.1\.322 的发布说明和官方文档/);
  assert.match(upgrade.steps[2].text_zh, /^有变化就停在这一步/);
  assert.match(upgrade.steps[3].text_zh, /host_version\.recorded 改成 2\.1\.322$/);
  const pointer = fact("grok-bot/bot:rules:指路规则");
  assert.equal(pointer.ai_task, "fix");
  assert.deepEqual(pointer.steps.map((x: { who: string; command: string | null }) => `${x.who} ${x.command ?? "-"}`), [
    "ai bin/agent-env plugin build --all",
    "ai bin/agent-env plugin pointer --target grok-bot/bot",
    "user -",
    "ai bin/agent-env plugin pointer --target grok-bot/bot --record",
    "ai bin/agent-env doctor --env grok-bot",
  ]);
  assert.match(pointer.steps[0].text_zh, /^把插件生成到最新/, "the plugin records its repository: no precondition step");
  // the steps stay one click away, in the row's folded part
  assert.match(inbox, /<div class="todo-more" id="in-\d+" hidden><ol class="steps"><li class="step" id="in-\d+-s0">/);
  // without the portal's AI bridge the same rows lead with "copy fix notes"
  const plain = await (await serve({ statusFile: FIX("v8-ai-fix"), lang: "zh", now: () => NOW5 })).fragment();
  const plainInbox = plain.slice(plain.indexOf('id="panel-inbox"'), plain.indexOf('id="panel-machines"'));
  assert.equal([...plainInbox.matchAll(/<div class="todo-acts"><button type="button" class="lt-btn lt-btn-sm" data-copy-from="fact-\d+"[^>]*>复制修复说明</g)].length, 5);
});

test("one column for every view: no view widens the page, so the title, tabs, view bar and footer never move", async () => {
  const { CSS } = await import("./page.ts");
  assert.match(CSS, /^\s*\.wrap \{ max-width: 1240px; margin: 0 auto;/m);
  assert.equal([...CSS.matchAll(/(?<!\()max-width: \d+px/g)].filter((m) => m[0] !== "max-width: 1240px").length, 0, "no second column width");
  assert.doesNotMatch(CSS.replace(/\/\*[\s\S]*?\*\//g, ""), /\.wrap:has\(|margin-inline/);
  // the graph and its pane fit the column: 672 + 32 + at least 400 = 1104 = 1152 - 2 × 24, the width the pane drops below at
  assert.match(CSS, /\.rel-body \{ display: grid; grid-template-columns: 672px minmax\(400px, 1fr\); gap: 32px;/);
  assert.match(CSS, /@media \(max-width: 1151px\) \{\s*\.rel-body \{ grid-template-columns: 672px; \}/);
});
