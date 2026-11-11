// Looking at a listed file from the page (files.ts, index.ts "/api/file"): what is served, what is refused, and
// which paths get the buttons. Every file lives under a temp home handed in through the `home` option.
import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer, request } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, test } from "node:test";
import { execFileSync } from "node:child_process";
import { renameSync } from "node:fs";
import { fileId, fileRow, fileSet, FOLDER_ENTRIES, FOLDER_READ, isFileId, isSecretPath, LISTED_PATHS, nodeIo, openTargets, PATH_CHARS, type PreviewIo, PREVIEW_BYTES, readPreview, requestGuard, tilde } from "./files.ts";
import { createPanelHandler, type PanelOptions } from "./index.ts";

const NOW = Date.parse("2026-11-09T08:00:00Z");
const FIX = (name: string) => join(import.meta.dirname, "fixtures", `${name}.json`);
const WORK = { overlay: "/home/example-user/agent-home", run: "/home/example-user/agent-home" };
const JSON_CSP = "default-src 'none'; frame-ancestors 'self'";
const servers: Array<{ close: () => void }> = [];
after(() => {
  for (const s of servers) s.close();
});

async function serve(opts: Omit<PanelOptions, "allowedHosts">) {
  let handler: ReturnType<typeof createPanelHandler>;
  const server = createServer((req, res) => void handler(req, res));
  servers.push(server);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as AddressInfo).port;
  const host = `127.0.0.1:${port}`;
  handler = createPanelHandler({ allowedHosts: [host], lang: "zh", now: () => NOW, folders: WORK, ...opts });
  const get = (path: string, o: { method?: string; headers?: Record<string, string> } = {}) =>
    new Promise<{ status: number; headers: Record<string, string | string[] | undefined>; body: string }>((resolve, reject) => {
      const req = request({ host: "127.0.0.1", port, path, method: o.method ?? "GET", headers: { Host: host, ...o.headers } }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (body += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body }));
      });
      req.on("error", reject);
      req.end();
    });
  const fragment = async () => (await get("/fragment", { headers: { "x-env-fragment": "1" } })).body;
  const file = async (id: string) => {
    const r = await get(`/api/file?id=${id}`);
    return { ...r, json: JSON.parse(r.body) as Record<string, unknown> };
  };
  return { get, fragment, file };
}

/** A temp home with the given files ("dir/" makes a folder). */
function tmpHome(files: Record<string, string | Buffer> = {}) {
  const home = mkdtempSync(join(tmpdir(), "env-panel-home-"));
  for (const [rel, content] of Object.entries(files)) {
    const path = join(home, rel);
    if (rel.endsWith("/")) mkdirSync(path, { recursive: true });
    else {
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, content);
    }
  }
  return home;
}
const tmpStatus = (data: unknown) => {
  const file = join(mkdtempSync(join(tmpdir(), "env-panel-")), "status.json");
  writeFileSync(file, JSON.stringify(data));
  return file;
};
/** A small status: this machine ("mac", live) and a branch report ("vps"), with the given source paths. */
const status = (sources: Record<string, string>, o: { live?: Record<string, unknown>; envSources?: Record<string, Record<string, string>> } = {}) => {
  const habits = Object.keys(sources).map((origin) => ({ kind: "rules", name: origin, state: "synced", origin }));
  const env = (name: string, extra: Record<string, unknown>) => ({ schema: 1, env: name, generated_at: "2026-11-09T07:00:00Z", targets: [{ id: `${name}/codex`, host: "codex", habits }], ...extra });
  return {
    schema: 1,
    generated_at: "2026-11-09T07:00:00Z",
    environments: [env("mac", o.live ?? { source: "live" }), env("vps", { source: "branch" })],
    errors: [],
    relations: {
      sources,
      env_sources: o.envSources ?? {},
      env_names: { mac: "Mac", vps: "VPS" },
      targets: [
        { id: "mac/codex", env: "mac", host: "codex", name: "Codex", roles: [] },
        { id: "vps/codex", env: "vps", host: "codex", name: "Codex", roles: [] },
      ],
    },
  };
};
const idOf = (path: string) => fileId(path);
const pane = (html: string, key: string) => new RegExp(`<section class="rel-pane" data-pane="${key}"[^>]*>([\\s\\S]*?)</section>(?=<section class="rel-pane"|</div>)`).exec(html)![1];

test("the route serves a file the status lists for this machine, as JSON text with the strict CSP", async () => {
  const home = tmpHome({ ".claude/AGENTS.md": "# Rules\n<script>alert(1)</script>\n" });
  const { fragment, file, get } = await serve({ statusFile: FIX("v8-plugins"), home });
  const html = await fragment();
  const id = idOf("~/.claude/AGENTS.md");
  assert.ok(html.includes(`data-view="${id}"`), "the page carries the id, not a path, for the request");
  const r = await file(id);
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, { state: "text", path: "~/.claude/AGENTS.md", text: "# Rules\n<script>alert(1)</script>\n", cut: false });
  assert.equal(r.headers["content-type"], "application/json; charset=utf-8", "never served as HTML or Markdown");
  assert.equal(r.headers["content-security-policy"], JSON_CSP);
  assert.equal(r.headers["x-content-type-options"], "nosniff");
  assert.equal(r.headers["cache-control"], "no-store");
  // HEAD answers without a body; every other method is refused as before
  const head = await get(`/api/file?id=${id}`, { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(head.body, "");
  for (const method of ["POST", "PUT", "DELETE", "PATCH"]) {
    const no = await get(`/api/file?id=${id}`, { method });
    assert.equal(no.status, 405, method);
    assert.equal(no.headers.allow, "GET, HEAD");
  }
});

test("every id the page carries is one the server knows; a listed file that is not there says so", async () => {
  const home = tmpHome({ "agent-rules/docs/": "" });
  const { fragment, file } = await serve({ statusFile: FIX("v8-plugins"), home });
  const html = await fragment();
  const ids = [...new Set([...html.matchAll(/data-view="([^"]*)"/g)].map((m) => m[1]))];
  assert.ok(ids.length >= 5, `relation sources and plugin folders: ${ids.length}`);
  for (const id of ids) {
    assert.ok(isFileId(id));
    const r = await file(id);
    assert.equal(r.status, 200, id);
    assert.ok(["gone", "folder"].includes(String(r.json.state)), String(r.json.state));
  }
});

test("the route refuses anything the status did not list: other ids, paths, odd ids", async () => {
  const home = tmpHome({ ".claude/AGENTS.md": "rules\n", "secret.txt": "not listed\n" });
  const { fragment, file, get } = await serve({ statusFile: tmpStatus(status({ rules: "~/.claude/AGENTS.md" })), home });
  await fragment();
  assert.equal((await file(idOf("~/.claude/AGENTS.md"))).status, 200);
  const long = "a".repeat(8000); // near the most a request line may carry
  const tries = [
    idOf("~/secret.txt"), // a real file under home, a well-formed id, but never listed
    "", "0", "0".repeat(15), "0".repeat(17), idOf("~/.claude/AGENTS.md").toUpperCase(), long,
    "~/secret.txt", "~%2Fsecret.txt", encodeURIComponent(join(home, "secret.txt")), "/etc/hosts", "..%2F..%2Fetc%2Fhosts", "%2e%2e%2f%2e%2e%2fetc%2fhosts",
    "..%252f..%252fetc%252fhosts", "%00", `${idOf("~/.claude/AGENTS.md")}%00`, `${idOf("~/.claude/AGENTS.md")}/../secret.txt`, "%ff%fe",
  ];
  for (const id of tries) {
    const r = await file(id);
    assert.equal(r.status, 404, id.slice(0, 40));
    assert.deepEqual(r.json, { state: "stale" });
    assert.equal(r.headers["content-security-policy"], JSON_CSP);
    assert.ok(!r.body.includes("not listed"));
  }
  // no id at all, a path or an id in the URL path, a second id: nothing is read
  for (const path of ["/api/file", "/api/file?path=~/secret.txt", "/api/file?file=secret.txt", "/api/file/~/secret.txt", `/api/file/${idOf("~/.claude/AGENTS.md")}`, "/api/file/..%2f..%2fsecret.txt", "/api/file?id=&id=" + idOf("~/.claude/AGENTS.md")]) {
    const r = await get(path);
    assert.equal(r.status, 404, path);
    assert.ok(!r.body.includes("rules") && !r.body.includes("not listed"), path);
  }
});

test("a listed path outside the home folder, or a link that leaves it, is refused like an unknown id", async () => {
  const outside = tmpHome({ "outside.txt": "outside the home\n", "dir/inner.txt": "x" });
  const home = tmpHome({ "real/notes.md": "inside\n", "sub/": "" });
  symlinkSync(join(outside, "outside.txt"), join(home, "leaves"));
  symlinkSync(join(outside, "dir"), join(home, "leaves-dir"));
  symlinkSync(join(home, "real", "notes.md"), join(home, "stays"));
  symlinkSync(join(home, "gone-target"), join(home, "dangling"));
  const sources = {
    abs: join(outside, "outside.txt"), etc: "/etc/hosts", up: "~/../" + outside.split("/").pop() + "/outside.txt", up2: "~/sub/../../outside.txt",
    leaves: "~/leaves", leavesDir: "~/leaves-dir", through: "~/leaves-dir/inner.txt", stays: "~/stays", dangling: "~/dangling", home: "~",
  };
  const { fragment, file } = await serve({ statusFile: tmpStatus(status(sources)), home });
  const html = await fragment();
  // outside home or with a `..` step: no button and no id at all (the path can still be copied)
  for (const key of ["abs", "etc", "up", "up2"] as const) {
    assert.ok(!html.includes(`data-view="${idOf(sources[key])}"`), key);
    assert.equal((await file(idOf(sources[key]))).status, 404, key);
  }
  assert.ok(!html.includes(`data-open-path="/`), "no absolute path is handed to the open control");
  // a link out of home is listed (the page cannot know) and refused when followed
  for (const key of ["leaves", "leavesDir", "through"] as const) {
    assert.ok(html.includes(`data-view="${idOf(sources[key])}"`), key);
    const r = await file(idOf(sources[key]));
    assert.equal(r.status, 404, key);
    assert.deepEqual(r.json, { state: "unlisted" });
    assert.ok(!r.body.includes("outside the home"));
  }
  // a link that stays inside is followed; one that points nowhere is a file that is not there
  assert.deepEqual((await file(idOf("~/stays"))).json, { state: "text", path: "~/stays", text: "inside\n", cut: false });
  assert.deepEqual((await file(idOf("~/dangling"))).json, { state: "gone", path: "~/dangling" });
  assert.equal((await file(idOf("~"))).json.state, "folder");
});

test("states the reader can meet: not text, cut at the limit, empty, gone after the page was drawn, unreadable", async () => {
  const big = Buffer.alloc(PREVIEW_BYTES + 10, "a");
  const home = tmpHome({
    "bin.dat": Buffer.from([0x89, 0x50, 0x4e, 0x47, 0, 1, 2, 3]),
    "late-nul.txt": Buffer.concat([Buffer.alloc(9000, "b"), Buffer.from([0])]),
    "big.txt": big,
    "exact.txt": Buffer.alloc(PREVIEW_BYTES, "c"),
    // a three-byte character across the limit: it is left out, not shown broken
    "split.txt": Buffer.concat([Buffer.alloc(PREVIEW_BYTES - 1, "d"), Buffer.from("好好")]),
    "empty.txt": "",
    "soon-gone.txt": "here now\n",
    "locked.txt": "no\n",
    "中文 name.md": "文字\n",
  });
  const sources = Object.fromEntries(["bin.dat", "late-nul.txt", "big.txt", "exact.txt", "split.txt", "empty.txt", "soon-gone.txt", "locked.txt", "中文 name.md", "never-there.txt", "empty.txt/child"].map((n, i) => [`s${i}`, `~/${n}`]));
  const { fragment, file } = await serve({ statusFile: tmpStatus(status(sources)), home });
  await fragment();
  const at = async (name: string) => (await file(idOf(`~/${name}`))).json;
  assert.deepEqual(await at("bin.dat"), { state: "binary", path: "~/bin.dat" });
  assert.equal((await at("late-nul.txt")).state, "text", "only the first 8 KB decide");
  const cut = await at("big.txt");
  assert.equal(cut.cut, true);
  assert.equal((cut.text as string).length, PREVIEW_BYTES);
  assert.equal((await at("exact.txt")).cut, false);
  const split = await at("split.txt");
  assert.equal(split.cut, true);
  assert.equal(split.text, "d".repeat(PREVIEW_BYTES - 1));
  assert.deepEqual(await at("empty.txt"), { state: "text", path: "~/empty.txt", text: "", cut: false });
  assert.deepEqual(await at("中文 name.md"), { state: "text", path: "~/中文 name.md", text: "文字\n", cut: false });
  assert.deepEqual(await at("never-there.txt"), { state: "gone", path: "~/never-there.txt" });
  assert.deepEqual(await at("empty.txt/child"), { state: "gone", path: "~/empty.txt/child" }, "a path through a file");
  // deleted between the render and the click
  assert.equal((await at("soon-gone.txt")).text, "here now\n");
  rmSync(join(home, "soon-gone.txt"));
  assert.deepEqual(await at("soon-gone.txt"), { state: "gone", path: "~/soon-gone.txt" });
  // replaced by a folder between the render and the click: the folder is listed
  mkdirSync(join(home, "soon-gone.txt"));
  assert.deepEqual(await at("soon-gone.txt"), { state: "folder", path: "~/soon-gone.txt", entries: [], total: 0, cut: false, more: false });
  if (process.getuid?.() !== 0) {
    chmodSync(join(home, "locked.txt"), 0o000);
    assert.deepEqual(await at("locked.txt"), { state: "error", path: "~/locked.txt" });
    chmodSync(join(home, "locked.txt"), 0o644);
  }
});

test("a folder lists its entries: names only, folders first, at most 200", async () => {
  const files: Record<string, string> = { "docs/b.md": "b", "docs/a.md": "a", "docs/zeta/": "", "docs/alpha/deep.md": "never shown", "docs/.hidden": "h", "many/": "" };
  for (let i = 0; i < FOLDER_ENTRIES + 5; i++) files[`many/f${String(i).padStart(3, "0")}.txt`] = "";
  const home = tmpHome(files);
  symlinkSync(join(home, "docs", "zeta"), join(home, "docs", "link-to-folder"));
  const { fragment, file } = await serve({ statusFile: tmpStatus(status({ docs: "~/docs", many: "~/many" })), home });
  await fragment();
  const docs = await file(idOf("~/docs"));
  assert.deepEqual(docs.json, { state: "folder", path: "~/docs", entries: ["alpha/", "zeta/", ".hidden", "a.md", "b.md", "link-to-folder"], total: 6, cut: false, more: false });
  assert.ok(!docs.body.includes("never shown"));
  const many = (await file(idOf("~/many"))).json;
  assert.equal((many.entries as string[]).length, FOLDER_ENTRIES);
  assert.equal(many.total, FOLDER_ENTRIES + 5);
  assert.equal(many.cut, true);
  assert.equal((many.entries as string[])[0], "f000.txt");
});

test("the answer goes through hide(): the home folder shows as ~, in the file's text too", async () => {
  const home = tmpHome({});
  writeFileSync(join(home, "notes.md"), `see ${home}/projects/x and ${home}/.config\n`);
  mkdirSync(join(home, "dir"));
  const { fragment, file } = await serve({ statusFile: tmpStatus(status({ notes: join(home, "notes.md"), dir: "~/dir" })), home });
  const html = await fragment();
  assert.ok(!html.includes(home), "the page never holds the real home path");
  assert.match(html, /data-open-path="~\/notes\.md" data-open-name="notes\.md" hidden>/, "the open control gets the ~ path");
  const r = await file(idOf("~/notes.md"));
  assert.equal(r.status, 200, "a path the status wrote in full is the same file as its ~ form");
  assert.equal(r.json.text, "see ~/projects/x and ~/.config\n");
  assert.ok(!r.body.includes(home));
});

test("only this machine's paths: another machine's get 复制路径 and a note, no buttons, and are never read", async () => {
  const home = tmpHome({ ".claude/AGENTS.md": "mine\n", "remote/rules.md": "same path, other machine\n", "portal/list.json": "{}\n" });
  const s = status({ rules: "~/.claude/AGENTS.md", list: "~/portal/list.json" }, { envSources: { vps: { rules: "~/remote/rules.md" } } });
  const { fragment, file } = await serve({ statusFile: tmpStatus(s), home });
  const html = await fragment();
  const mine = pane(html, "a:mac/codex");
  const theirs = pane(html, "a:vps/codex");
  assert.match(mine, /data-copy="~\/\.claude\/AGENTS\.md"[^>]*>复制路径<\/button><button type="button" class="linkish" data-view="[0-9a-f]{16}" aria-expanded="false">查看内容<\/button><span class="fv-open" data-open-path="~\/\.claude\/AGENTS\.md"/);
  assert.ok(!mine.includes("这个文件在另一台电脑上"));
  assert.match(theirs, /<div class="fv"><p class="fv-acts"><span class="fv-label">[^<]+<\/span><button type="button" class="linkish" data-copy="~\/remote\/rules\.md" data-tip="~\/remote\/rules\.md">复制路径<\/button><span class="fv-far">这个文件在另一台电脑上<\/span><\/p><\/div>/);
  assert.ok(!theirs.includes("data-view") && !theirs.includes("data-open-path"), "no view or open button for another machine");
  // the path only the other machine has is not on the server's list, though a file of that name exists here
  assert.equal((await file(idOf("~/remote/rules.md"))).status, 404);
  assert.equal((await file(idOf("~/.claude/AGENTS.md"))).status, 200);
  // the source's own pane: a shared path counts as this machine's because this machine is among those shown
  const shared = pane(html, "s:list");
  assert.match(shared, /data-view="[0-9a-f]{16}"/);
  const split = pane(html, "s:rules");
  assert.equal([...split.matchAll(/data-view=/g)].length, 1);
  assert.equal([...split.matchAll(/这个文件在另一台电脑上/g)].length, 1);
});

test("a status this machine did not check itself: no buttons anywhere, nothing served", async () => {
  const home = tmpHome({ ".claude/AGENTS.md": "mine\n", ".agents/": "" });
  for (const live of [{ source: "branch" }, { source: "live", checked_from: "vps" }, {}]) {
    const s = { ...status({ rules: "~/.claude/AGENTS.md" }, { live }), plugins_view: JSON.parse(readFileSync(FIX("v8-plugins"), "utf8")).plugins_view };
    const { fragment, file } = await serve({ statusFile: tmpStatus(s), home });
    const html = await fragment();
    assert.ok(!html.includes("data-view") && !html.includes("data-open-path"), JSON.stringify(live));
    assert.ok(html.includes("这个文件在另一台电脑上"));
    assert.ok(html.includes('data-copy="~/.agents" data-tip="~/.agents">复制路径'), "the Plugins tab keeps the copy button");
    assert.equal((await file(idOf("~/.claude/AGENTS.md"))).status, 404);
    assert.equal((await file(idOf("~/.agents"))).status, 404);
  }
});

test("read from fixtures: the Plugins tab's folders get the buttons on this machine; the relation tab's other machines do not", async () => {
  const home = tmpHome({ ".claude/AGENTS.md": "x", ".agents/skills/": "", ".local/state/agent-env/plugins/public/": "" });
  const { fragment, file } = await serve({ statusFile: FIX("v8-plugins"), home });
  const html = await fragment();
  const plugins = html.slice(html.indexOf('id="panel-plugins"'));
  assert.deepEqual([...plugins.matchAll(/data-open-path="([^"]*)"/g)].map((m) => m[1]), ["~/.claude", "~/.agents", "~/.local/state/agent-env/plugins"]);
  assert.deepEqual((await file(idOf("~/.local/state/agent-env/plugins"))).json, { state: "folder", path: "~/.local/state/agent-env/plugins", entries: ["public/"], total: 1, cut: false, more: false });
  assert.equal((await file(idOf("~/coding/agent-env"))).status, 404, "the generator's own folder is not shown, so not listed");
  for (const key of ["a:grok-bot/bot", "a:vps-helper/hermes"]) {
    const p = pane(html, key);
    assert.ok(!p.includes("data-view") && !p.includes("data-open-path"), key);
  }
  assert.ok(pane(html, "a:mac/codex").includes("data-view"));
  // a fixture without relations or plugins: no path rows at all
  const plain = await (await serve({ statusFile: FIX("v5-real"), home })).fragment();
  assert.ok(!plain.includes("data-view") && !plain.includes("data-open-path"));
});

test("ids of the last renders hold, so two tabs with different data do not break each other; a restart asks for a refresh", async () => {
  const home = tmpHome(Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`f${i}.md`, `file ${i}\n`])));
  const statusFile = tmpStatus(status({ rules: "~/f0.md" }));
  // no page drawn by this server yet (it restarted while the page stayed open): nothing is read, nothing rebuilt
  const fresh = await serve({ statusFile, home });
  const r = await fresh.file(idOf("~/f0.md"));
  assert.equal(r.status, 404);
  assert.deepEqual(r.json, { state: "stale" });
  assert.deepEqual((await fresh.file(idOf("~/f0.md"))).json, { state: "stale" }, "asking again does not build a list either");
  // tab A shows f0, tab B then shows f1: both keep working, and neither can read what only a status mentions
  const { fragment, file } = await serve({ statusFile, home });
  await fragment();
  writeFileSync(statusFile, JSON.stringify(status({ rules: "~/f1.md" })));
  await fragment();
  assert.equal((await file(idOf("~/f0.md"))).json.text, "file 0\n");
  assert.equal((await file(idOf("~/f1.md"))).json.text, "file 1\n");
  assert.deepEqual((await file(idOf("~/f2.md"))).json, { state: "stale" });
  // the list is bounded: after eight more renders the oldest are gone
  for (let i = 2; i <= 9; i++) {
    writeFileSync(statusFile, JSON.stringify(status({ rules: `~/f${i}.md` })));
    await fragment();
  }
  assert.deepEqual((await file(idOf("~/f0.md"))).json, { state: "stale" });
  assert.deepEqual((await file(idOf("~/f1.md"))).json, { state: "stale" });
  for (let i = 2; i <= 9; i++) assert.equal((await file(idOf(`~/f${i}.md`))).status, 200, `f${i}`);
  // a status that cannot be read adds an empty render; the earlier ones still hold
  writeFileSync(statusFile, "not json");
  await fragment();
  assert.deepEqual((await file(idOf("~/f2.md"))).json, { state: "stale" });
  assert.equal((await file(idOf("~/f9.md"))).status, 200);
  // the page says it in plain words, in both languages
  const zh = await (await serve({ statusFile: FIX("v8-plugins"), home })).get("/");
  assert.ok(zh.body.includes('"fileStale":"页面内容已更新，请刷新页面后再试"'));
  assert.match(zh.body, /r\.state === "stale" \? S\.fileStale/);
  const en = await (await serve({ statusFile: FIX("v8-plugins"), home, lang: "en" })).get("/");
  assert.ok(en.body.includes('"fileStale":"This page is out of date. Refresh it and try again."'));
});

test("readPreview and tilde on their own: no home, a home of /, paths that are not ~ paths", async () => {
  const home = tmpHome({ "a.md": "a\n" });
  assert.deepEqual(await readPreview("", "~/a.md"), { state: "outside" });
  assert.deepEqual(await readPreview("/", "~/etc/hosts"), { state: "outside" });
  assert.deepEqual(await readPreview(home, "/etc/hosts"), { state: "outside" });
  assert.deepEqual(await readPreview(home, "a.md"), { state: "outside" });
  assert.deepEqual(await readPreview(home, "~/../" + home.split("/").pop() + "/a.md"), { state: "text", path: "~/../" + home.split("/").pop() + "/a.md", text: "a\n", cut: false }, "resolves back inside: the list (tilde) is what keeps `..` out");
  assert.deepEqual(await readPreview(join(home, "nope"), "~/a.md"), { state: "error", path: "~/a.md" });
  assert.equal(tilde("~/a/../b"), null);
  assert.equal(tilde("~/a/..b"), "~/a/..b");
  assert.equal(tilde("/home/example-user/x", "/home/example-user"), "~/x");
  assert.equal(tilde("/home/example-user-2/x", "/home/example-user"), null);
  assert.equal(tilde("/home/example-user/x", ""), null);
  assert.equal(tilde("/etc/hosts", "/"), null);
  assert.equal(tilde("~"), "~");
  assert.equal(tilde("~other/x"), null);
  assert.equal(tilde(""), null);
});

test("the open control is used only when the portal's API answers with apps (feature detection, stubbed fetch)", async () => {
  const calls: Array<[string, unknown]> = [];
  const answer = (ok: boolean, body: unknown) => async (url: string, init: unknown) => {
    calls.push([url, init]);
    return { ok, json: async () => body };
  };
  assert.equal(await openTargets(answer(true, { targets: [{ id: "finder", name: "Finder" }], defaultApp: "finder" })), true);
  assert.deepEqual(calls[0], ["/_shared/api/open", { headers: { accept: "application/json" }, cache: "no-store" }], "asks the page's own origin, with a GET");
  assert.equal(await openTargets(answer(true, { targets: { finder: {} } })), true);
  // the panel on its own (404), an older portal, a broken answer, no network: a quiet no
  assert.equal(await openTargets(answer(false, { targets: [{ id: "finder" }] })), false);
  for (const body of [{ targets: [] }, { targets: {} }, {}, null, "not found", [], { targets: "all" }, { targets: 3 }]) assert.equal(await openTargets(answer(true, body)), false, JSON.stringify(body));
  assert.equal(await openTargets(async () => ({ ok: true, json: async () => { throw new SyntaxError("Unexpected token <"); } })), false);
  assert.equal(await openTargets(async () => { throw new TypeError("Failed to fetch"); }), false);
});

test("the page: the shared script is loaded by the page's own script with its nonce; the CSP is what it was", async () => {
  const { get } = await serve({ statusFile: FIX("v8-plugins"), home: tmpHome() });
  const page = await get("/");
  const nonce = /<script nonce="([^"]+)">/.exec(page.body)![1];
  assert.equal(page.headers["content-security-policy"], `default-src 'self'; script-src 'nonce-${nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'`);
  assert.equal([...page.body.matchAll(/<script\b/g)].length, 1, "one script element: the shared one is added at run time, only after the API answered");
  assert.ok(!/<script[^>]*src=/.test(page.body));
  assert.match(page.body, /window\.__openTargets=async function openTargets\(get\s*\)/);
  assert.match(page.body, /el\.src = "\/_shared\/open\.js";\s*el\.nonce = nonce;/);
  assert.match(page.body, /"fileGone":"这个文件已经不在了"/);
  for (const s of ["正在读取…", "这不是文本文件，没法在这里显示", "这个文件读不出来", "文件太大：只显示前 256 KB，后面的没有显示"]) assert.ok(page.body.includes(s), s);
  const en = await (await serve({ statusFile: FIX("v8-plugins"), home: tmpHome(), lang: "en" })).get("/");
  for (const s of ["Reading…", "This file no longer exists", "This is not a text file, so it cannot be shown here", "This file could not be read", "only the first 256 KB are shown"]) assert.ok(en.body.includes(s), s);
  // the preview is filled as text, never as HTML
  assert.match(page.body, /text\.textContent = value;/);
  assert.ok(!/fv-text[^;]*innerHTML/.test(page.body));
});

// ---- review round: one set of paths, secrets, safe open, bounded folders, strict text, stale answers ----

const within = <V>(p: Promise<V>, ms = 3000) => Promise.race([p, new Promise<never>((_, no) => setTimeout(() => no(new Error(`still waiting after ${ms} ms`)), ms))]);

test("one set of paths: the route reads exactly what the page drew a view button for", async () => {
  const home = tmpHome({ "AGENTS.md": "only the other machine uses this\n", "mine.md": "mine\n" });
  // this machine is live but has no habit from `rules`; only the other machine uses it
  const s = status({ rules: "~/AGENTS.md", mine: "~/mine.md" });
  s.environments[0].targets[0].habits = s.environments[0].targets[0].habits.filter((h) => h.origin === "mine");
  const { fragment, file } = await serve({ statusFile: tmpStatus(s), home });
  const html = await fragment();
  assert.ok(html.includes('data-copy="~/AGENTS.md"'), "the path is on the page, to copy");
  assert.ok(!html.includes(`data-view="${idOf("~/AGENTS.md")}"`), "no view button: this machine does not use that source");
  assert.match(pane(html, "s:rules"), /这个文件在另一台电脑上/);
  const r = await file(idOf("~/AGENTS.md"));
  assert.equal(r.status, 404, "guessing the id gets nothing");
  assert.deepEqual(r.json, { state: "stale" });
  assert.equal((await file(idOf("~/mine.md"))).json.text, "mine\n");
  // every id the route knows is on the page, and the other way round
  const onPage = new Set([...html.matchAll(/data-view="([^"]*)"/g)].map((m) => m[1]));
  assert.deepEqual([...onPage], [idOf("~/mine.md")]);
});

test("the set is capped: 500 paths of at most 1024 characters; the rest get no button", async () => {
  const set = fileSet("/home/example-user");
  for (let i = 0; i < LISTED_PATHS; i++) assert.ok(isFileId(set.issue(`~/f${i}`) ?? ""));
  assert.equal(set.issue("~/one-more"), null);
  assert.equal(set.issue("~/f3"), idOf("~/f3"), "a path already in the set keeps its button");
  assert.equal(set.size, LISTED_PATHS);
  assert.equal(set.path(idOf("~/one-more")), undefined);
  const other = fileSet("/home/example-user");
  assert.equal(other.issue("~/" + "a".repeat(PATH_CHARS - 2)), idOf("~/" + "a".repeat(PATH_CHARS - 2)));
  assert.equal(other.issue("~/" + "a".repeat(PATH_CHARS - 1)), null);
  assert.equal(other.issue("/home/example-user/x"), idOf("~/x"));
  assert.equal(other.issue("/etc/hosts"), null);
  // without a set nothing can be read, so no view button is drawn (the copy button and the open slot stay)
  const row = fileRow({ t: (x) => x, esc: (x) => String(x) }, "~/x.md", true);
  assert.ok(!row.includes("data-view") && row.includes("data-copy") && row.includes("data-open-path"));
  // through the page: 600 sources on this machine
  const sources = Object.fromEntries(Array.from({ length: 600 }, (_, i) => [`s${i}`, `~/many/f${i}.md`]));
  const { fragment, file } = await serve({ statusFile: tmpStatus(status(sources)), home: tmpHome() });
  const html = await fragment();
  const ids = new Set([...html.matchAll(/data-view="([^"]*)"/g)].map((m) => m[1]));
  assert.equal(ids.size, LISTED_PATHS);
  const without = Object.values(sources).filter((p) => !ids.has(idOf(p)));
  assert.equal(without.length, 100);
  assert.equal((await file(idOf(without[0]))).status, 404);
  assert.ok(html.includes(`data-copy="${without[0]}"`));
});

const SECRETS = [
  ".ssh/config", "work/.gnupg/pubring.kbx", ".aws/config", ".kube/config", ".docker/config.json", "Library/Keychains/login.txt", ".SSH/known_hosts",
  "app/prod.env", "app/.env", "app/.env.local", "certs/site.pem", "certs/server.key", "certs/a.p12", "certs/a.pfx", "keys2/id_rsa", "keys2/id_rsa.pub", "keys2/id_ed25519",
  ".config/agents/secrets/config.json", ".config/agents/credentials/store.json", ".config/agents/tokens/session.json", "a/Secret/n.md", "a/keys/list.md", "a/private/notes.txt", "a/.secrets.d/x.md", "a/.secret/y.md",
  "docs/site.pem", "docs/.env.md", "docs/id_rsa.txt", "secrets", "work/Private",
  "keys2/ID_RSA_old", "cfg/my-credentials.json", "cfg/secrets.yaml", "x/api-token.json", ".netrc", ".npmrc", ".pypirc", "tool/auth.json",
];
const NOT_SECRETS = ["notes/environment.md", "notes/keyboard.txt", "notes/monkey.md", "notes/ssh/readme.md", "notes/author.json", "notes/pem.txt", ".config/agents/rules.md", "vault-secret/readme.md", "rules/tokens.md", "rules/secrets.mdx", "rules/credentials.TXT", "cfg/api-token.txt", "notes/keys.md", "notes/privateer/a.md", "notes/tokens-of-thanks/a.md"];

test("secrets are never shown: no view button, the route answers 404, also through a link inside home", async () => {
  for (const p of SECRETS) assert.equal(isSecretPath(`~/${p}`), true, p);
  for (const p of NOT_SECRETS) assert.equal(isSecretPath(`~/${p}`), false, p);
  const home = tmpHome(Object.fromEntries([...SECRETS, ...NOT_SECRETS].map((p) => [p, `content of ${p}\n`])));
  mkdirSync(join(home, "links"));
  SECRETS.forEach((p, i) => symlinkSync(join(home, p), join(home, "links", `l${i}`)));
  symlinkSync(join(home, ".ssh"), join(home, "links", "folder"));
  symlinkSync(join(home, "notes", "monkey.md"), join(home, "links", "fine"));
  const sources: Record<string, string> = { fine: "~/links/fine", viaFolder: "~/links/folder/config", theFolder: "~/links/folder", sshFolder: "~/.ssh" };
  SECRETS.forEach((p, i) => {
    sources[`d${i}`] = `~/${p}`;
    sources[`l${i}`] = `~/links/l${i}`;
  });
  NOT_SECRETS.forEach((p, i) => (sources[`n${i}`] = `~/${p}`));
  const { fragment, file } = await serve({ statusFile: tmpStatus(status(sources)), home });
  const html = await fragment();
  for (const p of SECRETS) {
    // listed by its own name: no view button, the other two stay
    assert.ok(!html.includes(`data-view="${idOf(`~/${p}`)}"`), p);
    assert.ok(html.includes(`data-copy="~/${p}"`) && html.includes(`data-open-path="~/${p}"`), p);
    const r = await file(idOf(`~/${p}`));
    assert.equal(r.status, 404, p);
    assert.deepEqual(r.json, { state: "stale" });
    assert.deepEqual(await readPreview(home, `~/${p}`), { state: "outside" }, p);
  }
  for (let i = 0; i < SECRETS.length; i++) {
    // listed under an innocent name that links to it: the button is there, the read is refused at the real path
    const link = `~/links/l${i}`;
    assert.ok(html.includes(`data-view="${idOf(link)}"`), SECRETS[i]);
    const r = await file(idOf(link));
    assert.equal(r.status, 404, `${link} → ${SECRETS[i]}`);
    assert.ok(!r.body.includes("content of"));
  }
  for (const p of ["~/links/folder/config", "~/links/folder", "~/.ssh"]) assert.equal((await file(idOf(p))).status, 404, p);
  for (const p of NOT_SECRETS) assert.equal((await file(idOf(`~/${p}`))).json.text, `content of ${p}\n`, p);
  assert.equal((await file(idOf("~/links/fine"))).json.text, "content of notes/monkey.md\n");
});

test("a pipe does not hang the read: listed as one, or swapped in after the check", async (t) => {
  const home = tmpHome({ "rules.md": "rules\n" });
  try {
    execFileSync("mkfifo", [join(home, "pipe")]);
  } catch {
    return t.skip("mkfifo is not available");
  }
  const { fragment, file } = await serve({ statusFile: tmpStatus(status({ pipe: "~/pipe", rules: "~/rules.md" })), home });
  await fragment();
  assert.deepEqual((await within(file(idOf("~/pipe")))).json, { state: "error", path: "~/pipe" });
  // the file is a regular one when resolved, a pipe with no writer by the time it is opened
  let swapped = false;
  const io: PreviewIo = {
    ...nodeIo,
    open: async (path, flags) => {
      if (!swapped) {
        swapped = true;
        rmSync(join(home, "rules.md"));
        execFileSync("mkfifo", [join(home, "rules.md")]);
      }
      return nodeIo.open(path, flags);
    },
  };
  assert.deepEqual(await within(readPreview(home, "~/rules.md", io)), { state: "error", path: "~/rules.md" });
  assert.ok(swapped);
});

test("a folder on the way swapped between the check and the read: nothing outside home, and nothing else, is shown", async () => {
  const outside = tmpHome({ "hosts": "outside the home\n", "sub/inner.txt": "x" });
  /** Runs `swap` once, right after the listed path was resolved the first time (before the open). */
  const swapAfterFirstResolve = (listed: string, swap: () => void): PreviewIo => {
    let done = false;
    return {
      ...nodeIo,
      realpath: async (p) => {
        const real = await nodeIo.realpath(p);
        if (p === listed && !done) {
          done = true;
          swap();
        }
        return real;
      },
    };
  };
  // a file: ~/swap/hosts; `swap` becomes a link to a folder outside home that has a `hosts` too
  let home = tmpHome({ "swap/hosts": "inside\n" });
  let io = swapAfterFirstResolve(join(home, "swap/hosts"), () => {
    renameSync(join(home, "swap"), join(home, "swap-old"));
    symlinkSync(outside, join(home, "swap"));
  });
  assert.deepEqual(await readPreview(home, "~/swap/hosts"), { state: "text", path: "~/swap/hosts", text: "inside\n", cut: false });
  assert.deepEqual(await readPreview(home, "~/swap/hosts", io), { state: "outside" }, "the open reached the outside file; the second check refuses it");
  // a folder below the swapped one
  home = tmpHome({ "swap/sub/a.txt": "a" });
  io = swapAfterFirstResolve(join(home, "swap/sub"), () => {
    renameSync(join(home, "swap"), join(home, "swap-old"));
    symlinkSync(outside, join(home, "swap"));
  });
  assert.deepEqual(await readPreview(home, "~/swap/sub", io), { state: "outside" });
  // the listed folder itself becomes a link: not followed at the open
  home = tmpHome({ "swap/a.txt": "a" });
  io = swapAfterFirstResolve(join(home, "swap"), () => {
    renameSync(join(home, "swap"), join(home, "swap-old"));
    symlinkSync(outside, join(home, "swap"));
  });
  assert.deepEqual(await readPreview(home, "~/swap", io), { state: "error", path: "~/swap" });
  // swapped for another folder inside home: the file opened is not the one the path leads to any more
  home = tmpHome({ "swap/notes.md": "the old one\n", "other/notes.md": "another file\n" });
  io = swapAfterFirstResolve(join(home, "swap/notes.md"), () => {
    renameSync(join(home, "swap"), join(home, "swap-old"));
    renameSync(join(home, "other"), join(home, "swap"));
    // the path opened (already resolved) must still exist for the open to reach a file at all
  });
  assert.deepEqual(await readPreview(home, "~/swap/notes.md", io), { state: "text", path: "~/swap/notes.md", text: "another file\n", cut: false }, "both checks and the read saw the same file: the new one");
  // swapped after the open instead (before the second check): the descriptor holds the old file, the path the new
  home = tmpHome({ "swap/notes.md": "the old one\n", "other/notes.md": "another file\n" });
  let opened = false;
  const h = home;
  io = {
    ...nodeIo,
    open: async (p, f) => {
      const file = await nodeIo.open(p, f);
      if (!opened) {
        opened = true;
        renameSync(join(h, "swap"), join(h, "swap-old"));
        renameSync(join(h, "other"), join(h, "swap"));
      }
      return file;
    },
  };
  assert.deepEqual(await readPreview(home, "~/swap/notes.md", io), { state: "error", path: "~/swap/notes.md" });
  // a folder swapped while its names are being read (the path fallback): the listing is thrown away
  home = tmpHome({ "swap/a.txt": "a", "other/b.txt": "b" });
  const h2 = home;
  let listedOnce = false;
  io = {
    ...nodeIo,
    opendir: async (p) => {
      if (p.startsWith("/dev/fd/")) throw Object.assign(new Error("ENOTDIR"), { code: "ENOTDIR" });
      const dir = await nodeIo.opendir(p);
      if (!listedOnce) {
        listedOnce = true;
        renameSync(join(h2, "swap"), join(h2, "swap-old"));
        symlinkSync(outside, join(h2, "swap"));
      }
      return dir;
    },
  };
  assert.deepEqual(await readPreview(home, "~/swap", io), { state: "outside" });
});

test("text is UTF-8 or it is not text: invalid bytes are refused, a character at the cut is not called invalid", async () => {
  const home = tmpHome({
    "ff.txt": Buffer.from([0xff, 0xfe, 0xfd, 0xfc]),
    "latin1.txt": Buffer.from([0x63, 0x61, 0x66, 0xe9, 0x0a]),
    "utf16.txt": Buffer.from("﻿hi", "utf16le"),
    "late.txt": Buffer.concat([Buffer.alloc(20_000, "a"), Buffer.from([0xc3, 0x28])]),
    "half.txt": Buffer.from([0x61, 0xe5, 0xa5]), // ends inside a character, and is not cut
    "bom.txt": Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from("with a mark\n")]),
    "ok.txt": "中文 and émoji 🙂\n",
    "split.txt": Buffer.concat([Buffer.alloc(PREVIEW_BYTES - 2, "d"), Buffer.from("🙂🙂")]),
    "split-bad.txt": Buffer.concat([Buffer.alloc(PREVIEW_BYTES - 2, "d"), Buffer.from([0xff, 0xff, 0xff, 0xff])]),
  });
  const at = (name: string) => readPreview(home, `~/${name}`);
  for (const name of ["ff.txt", "latin1.txt", "utf16.txt", "late.txt", "half.txt", "split-bad.txt"]) assert.deepEqual(await at(name), { state: "binary", path: `~/${name}` }, name);
  assert.deepEqual(await at("bom.txt"), { state: "text", path: "~/bom.txt", text: "with a mark\n", cut: false });
  assert.deepEqual(await at("ok.txt"), { state: "text", path: "~/ok.txt", text: "中文 and émoji 🙂\n", cut: false });
  assert.deepEqual(await at("split.txt"), { state: "text", path: "~/split.txt", text: "d".repeat(PREVIEW_BYTES - 2), cut: true });
});

test("a huge folder: reading stops after 2000 entries, 200 are shown, and the answer says there are more", async () => {
  const home = tmpHome({ "big/": "" });
  let given = 0;
  let closed = false;
  const io: PreviewIo = {
    ...nodeIo,
    opendir: async () => ({
      async *[Symbol.asyncIterator]() {
        try {
          for (let i = 0; i < 100_000; i++) {
            given++;
            yield { name: `e${String(99_999 - i).padStart(5, "0")}`, isDirectory: () => i % 1000 === 0 };
          }
        } finally {
          closed = true;
        }
      },
    }),
  };
  const r = await readPreview(home, "~/big", io);
  assert.ok(r.state === "folder");
  assert.equal(given, FOLDER_READ + 1, "one look past the limit, to know there are more");
  assert.ok(closed, "the folder is closed, not read to its end");
  assert.equal(r.entries.length, FOLDER_ENTRIES);
  assert.deepEqual([r.total, r.cut, r.more], [FOLDER_READ, true, true]);
  assert.deepEqual(r.entries.slice(0, 3), ["e98999/", "e99999/", "e98000"], "folders first, each group sorted");
  // on a real disk, through the route: just over the limit, exactly at it, and a small one
  const files: Record<string, string> = { "few/a": "", "few/b/": "" };
  for (let i = 0; i < FOLDER_READ + 7; i++) files[`over/f${i}`] = "";
  for (let i = 0; i < FOLDER_READ; i++) files[`exact/f${i}`] = "";
  const real = tmpHome(files);
  const { fragment, file } = await serve({ statusFile: tmpStatus(status({ over: "~/over", exact: "~/exact", few: "~/few" })), home: real });
  await fragment();
  const over = (await file(idOf("~/over"))).json;
  assert.deepEqual([(over.entries as string[]).length, over.total, over.cut, over.more], [FOLDER_ENTRIES, FOLDER_READ, true, true]);
  const exact = (await file(idOf("~/exact"))).json;
  assert.deepEqual([(exact.entries as string[]).length, exact.total, exact.cut, exact.more], [FOLDER_ENTRIES, FOLDER_READ, true, false]);
  assert.deepEqual((await file(idOf("~/few"))).json, { state: "folder", path: "~/few", entries: ["b/", "a"], total: 2, cut: false, more: false });
});

test("a reopened preview ignores an older answer (the guard the page script uses)", async () => {
  const guard = requestGuard();
  const shown: string[] = [];
  const apply = (n: number, text: string) => guard.current(n) && shown.push(text);
  // open, close, open again: the first answer arrives last
  const first = guard.begin();
  guard.cancel();
  const second = guard.begin();
  apply(second, "the file no longer exists");
  apply(first, "old content");
  assert.deepEqual(shown, ["the file no longer exists"]);
  // closed while the answer is on its way: nothing is shown
  const third = guard.begin();
  guard.cancel();
  apply(third, "late");
  assert.deepEqual(shown, ["the file no longer exists"]);
  // answers in order still show; each preview has its own guard
  const other = requestGuard();
  const a = other.begin();
  assert.ok(other.current(a) && !guard.current(third));
  const b = guard.begin();
  const c = guard.begin();
  apply(c, "newest");
  apply(b, "older");
  assert.deepEqual(shown, ["the file no longer exists", "newest"]);
  // the page embeds this very function and checks it before it writes anything
  const page = await (await serve({ statusFile: FIX("v8-plugins"), home: tmpHome() })).get("/");
  assert.match(page.body, /window\.__requestGuard=function requestGuard\(\)/);
  assert.match(page.body, /if \(!open\) return guard\.cancel\(\);\s*const mine = guard\.begin\(\);/);
  const script = page.body.slice(page.body.indexOf("async function viewFile"), page.body.indexOf("function openControl"));
  assert.ok(script.indexOf("if (!guard.current(mine)) return;") > script.indexOf("await fetch("), "checked after the answer came");
  assert.ok(script.indexOf("if (!guard.current(mine)) return;") < script.indexOf('r.state === "text"'), "and before anything is shown");
  assert.ok(page.body.includes("这个文件夹里超过 2000 项：只显示其中 {n} 项"));
});

test("what the reader sees: folder or file on the button, the file's place above the block, 收起 while open", async () => {
  const home = tmpHome({ ".claude/AGENTS.md": "rules\n", "agent-rules/docs/a.md": "a", ".agents/skills/": "" });
  const { fragment, get } = await serve({ statusFile: FIX("v8-plugins"), home });
  const html = await fragment();
  const row = (path: string) => new RegExp(`<button type="button" class="linkish" data-view="${idOf(path)}"( data-folder)? aria-expanded="false">([^<]*)</button>.*?<div class="fv-body" hidden><p class="fv-cap">([^<]*)</p><p class="fv-state" role="status"></p><p class="fv-intro" hidden>([^<]*)</p><pre class="fv-text" tabindex="0" hidden></pre></div>`).exec(html)!.slice(1);
  assert.deepEqual(row("~/.claude/AGENTS.md"), [undefined, "查看内容", "~/.claude/AGENTS.md", "这个文件夹里有："]);
  assert.deepEqual(row("~/.agents"), [" data-folder", "查看文件夹", "~/.agents", "这个文件夹里有："]);
  assert.deepEqual(row("~/.claude").slice(0, 2), [" data-folder", "查看文件夹"]);
  // a path that is not there (yet) is offered as a file; the answer then says it is gone
  assert.deepEqual(row("~/coding/agent-env/manifest/habits.toml").slice(0, 2), [undefined, "查看内容"]);
  const en = await (await serve({ statusFile: FIX("v8-plugins"), home, lang: "en" })).fragment();
  assert.match(en, />View folder<\/button>/);
  assert.match(en, />View<\/button>/);
  assert.match(en, /<p class="fv-intro" hidden>In this folder:<\/p>/);
  // the page script: the words follow the state of the block, with aria-expanded
  const page = (await get("/")).body;
  for (const s of ['"fileView":"查看内容"', '"folderView":"查看文件夹"', '"fileHide":"收起"']) assert.ok(page.includes(s), s);
  assert.match(page, /btn\.setAttribute\("aria-expanded", String\(open\)\);\s*btn\.textContent = open \? S\.fileHide : btn\.dataset\.folder !== undefined \? S\.folderView : S\.fileView;/);
  assert.match(page, /if \(intro\) intro\.hidden = !r\.entries\.length;/);
  const enPage = (await (await serve({ statusFile: FIX("v8-plugins"), home, lang: "en" })).get("/")).body;
  assert.ok(enPage.includes('"fileHide":"Hide"') && enPage.includes('"folderView":"View folder"'));
  // the block scrolls inside itself; nothing animates
  assert.match(page, /\.fv-text \{[^}]*max-height: 320px;[^}]*overflow: auto;/);
  assert.ok(!/\.fv[^{]*\{[^}]*(transition|animation)/.test(page));
});
