import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { ZH } from "./i18n.ts";
import { createPanelHandler } from "./index.ts";
import { readTab } from "./model.ts";
import { PLUGINS_PRESET, pluginsFact, readPluginsView } from "./plugins-view.ts";

const NOW = Date.parse("2026-11-09T08:00:00Z");
const FIX = (name: string) => join(import.meta.dirname, "fixtures", `${name}.json`);
const fixture = (name: string) => JSON.parse(readFileSync(FIX(name), "utf8"));

/** The fragment for a status file, without a server: the handler writes into a stand-in response. */
async function fragment(statusFile: string, o: { lang?: "en" | "zh"; ask?: boolean } = {}): Promise<string> {
  const handle = createPanelHandler({ allowedHosts: ["h"], lang: o.lang ?? "zh", now: () => NOW, statusFile, ...(o.ask ? { ask: { bridge: "postMessage" } } : {}) });
  let body = "";
  const res = { statusCode: 0, setHeader() {}, end(b?: string) { body = b ?? ""; } } as unknown as ServerResponse;
  await handle({ url: "/fragment", method: "GET", headers: { host: "h", "x-env-fragment": "1" } } as unknown as IncomingMessage, res);
  return body;
}
const panel = (html: string) => html.slice(html.indexOf('id="panel-plugins"'), html.indexOf("</section></div><footer"));

test("plugins tab: the fifth tab, remembered like the others", async () => {
  const html = await fragment(FIX("v8-plugins"));
  assert.match(html, /id="tab-relation"[^>]*>关系图<\/button><button type="button" class="lt-tab" role="tab" id="tab-plugins" aria-controls="panel-plugins" aria-selected="false" tabindex="-1" data-tab="plugins">插件<\/button><span class="view-ink"/);
  assert.match(html, /<section class="tabpanel" role="tabpanel" id="panel-plugins" aria-labelledby="tab-plugins" hidden>/);
  assert.equal(readTab({ getItem: () => "plugins" }), "plugins");
});

test("plugins tab: four columns from the sources to the environments, each repository with its visibility", async () => {
  const p = panel(await fragment(FIX("v8-plugins"), { ask: true }));
  const heads = [...p.matchAll(/<h3 class="pv-ch"[^>]*>(?:<svg[^]*?<\/svg>)?([^<]+)<\/h3>/g)].map((m) => m[1]);
  assert.deepEqual(heads, ["来源", "生成", "插件仓", "环境"]);
  // checked repositories say public or private; the one the host could not answer says why and how to fix it
  assert.match(p, /href="https:\/\/github.com\/example-user\/agent-plugin-public"[^>]*>agent-plugin-public<\/a><span class="pv-vis v-public"[^>]*>公开<\/span>/);
  assert.match(p, /agent-plugin-personal<\/a><span class="pv-vis v-private"[^>]*><svg[^]*?<\/svg>私有<span class="pv-empty">空仓<\/span><\/span>/);
  assert.match(p, /agent-plugin-company<\/a><span class="pv-vis v-unknown" data-tip="tool 没查到：401 Unauthorized\n修法: 确认能联网并已登录（tool login）[^"]*">未知<\/span><\/div><p class="pv-why">tool 没查到：401 Unauthorized<\/p><span class="pv-fix">确认能联网并已登录（tool login）/);
  // the view bar: when it was checked, how many are unknown, the refresh command, the AI button
  assert.match(p, /<div class="viewbar pv-bar"><p class="pv-checked">仓库公开还是私有：查于 今天 \d\d:\d\d<span class="pv-unknown-n">1 个未知<\/span><span class="pv-refresh">重新查：<\/span><span class="pv-cmd"><code>bin\/agent-env plugin repos --refresh<\/code><button[^>]*data-copy="bin\/agent-env plugin repos --refresh"/);
  assert.match(p, new RegExp(`<button type="button" class="lt-btn lt-btn-sm" data-ask="${PLUGINS_PRESET}" data-fact="fact-plugins"[^>]*>让 AI 调整</button>`));
  assert.match(p, /<textarea id="fact-plugins" hidden readonly>/);
  // repository notes from the engine, with their fix
  assert.match(p, /插件未定义/);
});

test("plugins tab: what each environment gets, one row per environment, one column per visibility", async () => {
  const p = panel(await fragment(FIX("v8-plugins")));
  const table = p.slice(p.indexOf('<table class="pv-table">'));
  assert.match(table, /<thead><tr><th scope="col">环境<\/th><th scope="col"><span[^>]*>公开<\/span><\/th><th scope="col"><span[^>]*>个人<\/span><\/th><th scope="col"><span[^>]*>公司<\/span><\/th><th scope="col"><span[^>]*>本机<\/span><\/th><\/tr><\/thead>/);
  const rows = [...table.matchAll(/<tr class="[^"]*"><th scope="row"><span class="pv-name">([^<]+)<\/span>/g)].map((m) => m[1]);
  assert.deepEqual(rows, ["Mac", "WSL", "小 helper 的云主机", "Grok Bot", "别人的机器"]);
  // an environment not on plugins yet: one cell across the four columns
  assert.match(table, /<td class="c is-legacy" colspan="4"><span class="pv-legacy">还没改成插件（阶段 4）<\/span><span class="pv-sub">现在：直接读来源仓<\/span><\/td>/);
  // Grok Bot: the public plugin, company denied by the manifest, its pointer rule note
  const bot = table.slice(table.indexOf("Grok Bot</span>"), table.indexOf("别人的机器"));
  assert.match(bot, /data-label="公开"><span class="pv-pl">demo-public<\/span><\/td>/);
  assert.match(bot, /<td class="c is-denied" data-label="公司"><span class="pv-deny"[^>]*><svg[^]*?<\/svg>禁止<\/span><\/td>/);
  assert.match(bot, /指路规则未投递/);
  assert.match(table, /<tr class="is-others"><th scope="row"><span class="pv-name">别人的机器<\/span>/);
});

test("plugins tab: one of the user's own machines on plugins composes them itself (stage 4)", async () => {
  const data = fixture("v8-plugins");
  const mac = data.plugins_view.environments.find((e: { env: string }) => e.env === "mac");
  Object.assign(mac, {
    mode: "plugins", phase: null, now_zh: null, notes: [],
    credential: { state: "none", name: null, expires: null, detail: "本机从规则源直接合成，不拉插件仓" },
    gets: { public: { state: "gets", plugins: ["demo-public"] }, personal: { state: "gets", plugins: ["demo-personal"] },
      company: { state: "none", plugins: [] }, local: { state: "gets", plugins: ["demo-mac"] } },
  });
  const dir = mkdtempSync(join(tmpdir(), "pv-compose-"));
  try {
    const file = join(dir, "status.json");
    writeFileSync(file, JSON.stringify(data));
    const p = panel(await fragment(file));
    assert.match(p, /<li class="pv-node pv-env"><div class="pv-node-h"><span class="pv-name">Mac<\/span><\/div><p class="pv-sub">本机按插件合成<\/p><p class="pv-what">demo-public、demo-personal、demo-mac<\/p><\/li>/);
    assert.doesNotMatch(p, /Mac<\/span><\/div><p class="pv-sub">本机<\/p>/, "not the bare transport any more");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugins tab: a status without plugins_view shows an empty state, the engine's error shows as an error", async () => {
  const p = panel(await fragment(FIX("v7-relations")));
  assert.match(p, /<div class="empty" role="status" data-pv="absent"><p><strong>这份状态里没有插件数据<\/strong>/);
  assert.doesNotMatch(p, /fact-plugins|data-ask/);
  assert.deepEqual(readPluginsView(undefined), { kind: "absent" });
  assert.deepEqual(readPluginsView({ refresh_command: "x", error: { message: "插件视图算不出来（KeyError）", fix: "bin/agent-env check 看清单哪里不对" } }), { kind: "error", message: "插件视图算不出来（KeyError）", fix: "bin/agent-env check 看清单哪里不对", refresh: "x" });
});

test("plugins view: odd entries are read safely; the AI fact stays within the limit", () => {
  const raw = fixture("v8-plugins").plugins_view;
  const data = readPluginsView({ ...raw, repos: [...raw.repos, 3, { key: "x/y/z", visibility: "secret" }], environments: [...raw.environments, { env: "odd", gets: { public: { state: "?" } } }] });
  assert.equal(data.kind, "ok");
  if (data.kind !== "ok") return;
  assert.equal(data.view.repos.at(-1)!.visibility, "unknown");
  assert.equal(data.view.environments.at(-1)!.gets.public.state, "none");
  const fact = JSON.parse(pluginsFact(data.view, 2000));
  assert.deepEqual(fact.plugins, [{ name: "demo-public", visibility: "public" }, { name: "demo-personal", visibility: "personal" }]);
  const bot = fact.environments.find((e: { env: string }) => e.env === "grok-bot");
  assert.deepEqual(bot, { env: "grok-bot", deny_visibility: ["company"], mode: "plugins", targets: [{ id: "grok-bot/bot", plugins: ["demo-public"] }] });
  assert.ok(!fact.environments.some((e: { env: string | null }) => e.env === null), "the row for other people's machines is not a manifest environment");
  assert.ok(pluginsFact(data.view, 200).length <= 200);
});

test("plugins tab: every string has a Chinese text, and the AI preset exists", async () => {
  const src = readFileSync(join(import.meta.dirname, "plugins-view.ts"), "utf8");
  const keys = [...src.matchAll(/k\.t\("([^"]+)"/g), ...src.matchAll(/^\s+(?:public|personal|company|local|synced|drift|missing|pushed|stale|not_pushed|rules|docs|hooks|skills|company_skills|"remote-exec"|"cloud-hosted"):\s*(?:\["[^"]+",\s*)?"([^"]+)"/gm)].map((m) => m[1]);
  const missing = [...new Set(keys)].filter((key) => !(key in ZH));
  assert.deepEqual(missing, []);
  const tasks = JSON.parse(readFileSync(join(import.meta.dirname, "ai-tasks.json"), "utf8"));
  const preset = tasks.tools.env.presets.find((p: { id: string }) => p.id === PLUGINS_PRESET);
  assert.ok(preset, "ai-tasks.json has the preset the button names");
  assert.match(preset.pointers.zh, /deny_visibility/);
  // outside the portal the same button copies the notes instead
  const plain = panel(await fragment(FIX("v8-plugins")));
  assert.match(plain, /data-copy-from="fact-plugins"[^>]*>复制调整说明</);
});
