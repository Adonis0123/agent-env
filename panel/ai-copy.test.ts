import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { aiFixText, aiLead, aiSentence, type AiStep, aiStepsText, aiStepText, presetWhat, readsOnly } from "./ai-copy.ts";
import { REPO_ROOT, workFolders } from "./source.ts";

const DIR = "~/agent-home";

test("copy for AI: one plain sentence with the command inside it, in both languages", () => {
  assert.equal(
    aiSentence("zh", "重新查一下各个仓库是公开还是私有", ["bin/agent-env plugin repos --refresh"], { dir: DIR }),
    "帮我重新查一下各个仓库是公开还是私有（在 ~/agent-home 目录里运行 bin/agent-env plugin repos --refresh）。",
  );
  assert.equal(
    aiSentence("en", "check again whether each repository is public or private", ["bin/agent-env plugin repos --refresh"], { dir: DIR }),
    "Please check again whether each repository is public or private (in the folder ~/agent-home, run bin/agent-env plugin repos --refresh).",
  );
});

test("copy for AI: a command that is not read-only asks to be shown first; a read-only one does not", () => {
  for (const c of ["bin/agent-env status --json --all --fetch", "bin/agent-env check", "bin/agent-env doctor --env grok-bot", "bin/agent-env plugin repos --refresh", "bin/agent-env deliver --target a/b --dry-run", "bin/agent-env render --target mac/codex --check", "python3 engine/mcp.py manifest/habits.toml --target mac/codex --plan", "bin/agent-env eval --target mac/codex", "ssh vps-helper true"]) assert.equal(readsOnly(c), true, c);
  for (const c of ["bin/agent-env render --target mac/codex --write", "bin/agent-env mcp --fix", "bin/agent-env mcp sync --target mac/hermes", "bin/agent-env status --commit --push", "bin/agent-env plugin publish --all", "bin/agent-env plugin build --all", "bin/agent-env plugin pointer --target grok-bot/bot", "bin/agent-env plugin pointer --target grok-bot/bot --record", "bin/agent-env deliver --target a/b --yes", "git pull"]) assert.equal(readsOnly(c), false, c);
  // the clause comes from the command, with no flag from the caller
  assert.equal(
    aiSentence("zh", "处理这个问题：Codex · delete-guard（不一致）", ["bin/agent-env render --target mac/codex --write"], { dir: DIR }),
    "帮我处理这个问题：Codex · delete-guard（不一致）（在 ~/agent-home 目录里运行 bin/agent-env render --target mac/codex --write）。会改动或上传东西的步骤，先给我看，等我同意再做。",
  );
  assert.equal(
    aiSentence("en", "handle this problem: figma is off", ["bin/agent-env mcp --fix"], { dir: DIR }),
    "Please handle this problem: figma is off (in the folder ~/agent-home, run bin/agent-env mcp --fix). For any step that changes or uploads something, show me first and wait for my yes.",
  );
  assert.equal(aiSentence("zh", "检查一下", ["bin/agent-env status --json --all"], { dir: DIR }), "帮我检查一下（在 ~/agent-home 目录里运行 bin/agent-env status --json --all）。");
  // the caller can still ask for it: updating the tool is a change although the command only reads
  assert.match(aiSentence("zh", "更新 agent-env", ["bin/agent-env status --json --all"], { dir: DIR, careful: true }), /先给我看，等我同意再做。$/);
  // no command: still a whole sentence; no folder: none is named
  assert.equal(aiSentence("zh", "处理这个问题：读不出插件信息"), "帮我处理这个问题：读不出插件信息。");
  assert.equal(aiSentence("en", "do it", [" bin/agent-env check "]), "Please do it (run bin/agent-env check).");
  // the same inputs give the same text
  assert.equal(aiSentence("zh", "a", ["b"], { dir: DIR }), aiSentence("zh", "a", ["b"], { dir: DIR }));
});

const WSL: AiStep[] = [
  { who: "other_env", text: "在 WSL 上先跑一遍检查看结果（只读，不提交也不推送）", command: "bin/agent-env status", confirm: false },
  { who: "other_env", text: "看过结果后确认，再运行 bin/agent-env status --commit --push（会提交并推送状态分支）", command: null, confirm: true },
  { who: "ai", text: "回到本机拉取最新状态", command: "bin/agent-env status --json --all --fetch", confirm: false },
];

test("copy for AI, a thing to do on another machine: every step in order, each on the machine its own `who` names", () => {
  assert.equal(
    aiStepsText("zh", "WSL 还没上报状态", WSL, { dir: DIR, machine: "wsl" }),
    [
      "帮我处理这个问题：WSL 还没上报状态。按下面的顺序做，一步做完再做下一步：",
      "1. 在 wsl 这台机器上：在 WSL 上先跑一遍检查看结果（只读，不提交也不推送）。运行：bin/agent-env status",
      // the step without a command carries its text: it is the one that reports
      "2. 在 wsl 这台机器上：看过结果后确认，再运行 bin/agent-env status --commit --push（会提交并推送状态分支）（先给我看，等我同意再做）",
      // back on this machine, in its folder: not on wsl
      "3. 在本机的 ~/agent-home 目录里：回到本机拉取最新状态。运行：bin/agent-env status --json --all --fetch",
      "wsl 上的步骤，你连不上那台机器的话，把要运行的内容给我，我去那台机器上做。",
    ].join("\n"),
  );
  const en = aiStepsText("en", "WSL has not reported", WSL.map((s) => ({ ...s, text: "x" })), { dir: DIR, machine: "wsl" }).split("\n");
  assert.equal(en[0], "Please handle this problem: WSL has not reported. Do these steps in order, one after the other:");
  assert.equal(en[1], "1. On the machine wsl: x. Run: bin/agent-env status");
  assert.equal(en[2], "2. On the machine wsl: x (show me first and wait for my yes)");
  assert.equal(en[3], "3. On this machine, in the folder ~/agent-home: x. Run: bin/agent-env status --json --all --fetch");
  // a step of the person's names no machine (the engine does not say where; its text does), and the AI stops there
  const mine = aiStepsText("zh", "云主机这次没检查成功", [{ who: "user", text: "确认这台 Mac 能登录云主机", command: "ssh vps-helper true", confirm: false }, { who: "ai", text: "重新检查", command: "bin/agent-env status --env vps-helper", confirm: false }], { dir: DIR, machine: "vps-helper" });
  assert.match(mine, /\n1\. 这一步由我来做：确认这台 Mac 能登录云主机。命令：ssh vps-helper true\n2\. 在本机的 ~\/agent-home 目录里：重新检查。运行：bin\/agent-env status --env vps-helper\n轮到我做的步骤，停下来告诉我具体做什么，等我说做完了再继续。$/);
  assert.doesNotMatch(mine, /vps-helper 这台机器上/, "no command is sent to the machine the row is about");
});

test("copy for AI, one step: which step of how many, not before the earlier ones, a write waits, and 你 is the person", () => {
  const steps: AiStep[] = [
    { who: "ai", text: "生成指路规则", command: "bin/agent-env plugin pointer --target grok-bot/bot", confirm: false },
    { who: "user", text: "在 Grok Bot 里粘贴发送", command: null, confirm: false },
    { who: "ai", text: "你说已存好后，记录这次投递", command: "bin/agent-env plugin pointer --target grok-bot/bot --record", confirm: true },
    { who: "user", text: "看过演练确认没问题后，正式投递", command: "bin/agent-env deliver --target vps/hermes --dry-run", confirm: true },
  ];
  const o = { dir: DIR, machine: "grok-bot" };
  assert.equal(aiStepText("zh", "指路规则还没投递", steps, 0, o), "帮我做「指路规则还没投递」的第 1 步（共 4 步）。在本机的 ~/agent-home 目录里：生成指路规则。运行：bin/agent-env plugin pointer --target grok-bot/bot（先给我看，等我同意再做）。");
  assert.equal(
    aiStepText("zh", "指路规则还没投递", steps, 2, o),
    "帮我做「指路规则还没投递」的第 3 步（共 4 步；前面的步骤还没做完的话，先告诉我，不要直接做这一步）。在本机的 ~/agent-home 目录里：你说已存好后，记录这次投递。运行：bin/agent-env plugin pointer --target grok-bot/bot --record（先给我看，等我同意再做）。引用的文字是面板写给我看的，其中的「你」指我本人，不是你这个 AI。",
  );
  // a write step carries its dry-run form: the command is named as the dry run, not as the real thing
  assert.match(aiStepText("zh", "t", steps, 3, o), /这一步由我来做：看过演练确认没问题后，正式投递。演练用：bin\/agent-env deliver --target vps\/hermes --dry-run；正式执行前先给我看演练结果，等我同意。/);
  assert.equal(
    aiStepText("en", "The pointer is not delivered", steps.map((s) => ({ ...s, text: s.text.includes("你") ? "Once you say it is saved, record the delivery" : "x" })), 2, o),
    "Please do step 3 of 4 of “The pointer is not delivered”; if the earlier steps are not done yet, tell me first instead of doing this one. On this machine, in the folder ~/agent-home: Once you say it is saved, record the delivery. Run: bin/agent-env plugin pointer --target grok-bot/bot --record (show me first and wait for my yes). The quoted text was written by the panel for me: “you” (“你”) in it means me, the person, not you, the AI.",
  );
});

test("copy for AI, a fix written as a sentence: quoted whole, so commands whose order matters are never split", () => {
  const fix = "bin/agent-env plugin pointer --target grok-bot/bot 输出指路规则，由你在该 app 里存进它的 memory 并确认后，运行 bin/agent-env plugin pointer --target grok-bot/bot --record";
  assert.equal(
    aiFixText("zh", "Grok Bot · 指路规则（缺失）：没有投递记录", fix, { dir: DIR }),
    `帮我处理这个问题：Grok Bot · 指路规则（缺失）：没有投递记录。面板给的做法是：${fix}。要运行命令的话，在本机的 ~/agent-home 目录里运行。引用的文字是面板写给我看的，其中的「你」指我本人，不是你这个 AI。会改动或上传东西的步骤，先给我看，等我同意再做。`,
  );
  assert.equal(
    aiFixText("en", "the pointer is missing", "Add it to the whitelist, then re-run the generator.", { dir: DIR }),
    "Please handle this problem: the pointer is missing. The panel's fix is: Add it to the whitelist, then re-run the generator. Any command runs on this machine, in the folder ~/agent-home. For any step that changes or uploads something, show me first and wait for my yes.",
  );
});

test("copy for AI: the variable when the manifest is named by AGENT_ENV_MANIFEST, and a warning when none was found", () => {
  const named = { dir: "~/agent-env", env: "AGENT_ENV_MANIFEST=~/mine/habits.toml" };
  assert.equal(aiSentence("zh", "重新查", ["bin/agent-env plugin repos --refresh"], named), "帮我重新查（在 ~/agent-env 目录里运行 AGENT_ENV_MANIFEST=~/mine/habits.toml bin/agent-env plugin repos --refresh）。");
  const steps = aiStepsText("zh", "t", WSL, { ...named, machine: "wsl" });
  assert.match(steps, /\n1\. 在 wsl 这台机器上：[^\n]*运行：bin\/agent-env status\n/, "the other machine has its own manifest: no variable there");
  assert.match(steps, /\n3\. 在本机的 ~\/agent-env 目录里：回到本机拉取最新状态。运行：AGENT_ENV_MANIFEST=~\/mine\/habits\.toml bin\/agent-env status --json --all --fetch\n/);
  assert.match(aiFixText("zh", "p", "跑 bin/agent-env check", named), /在本机的 ~\/agent-env 目录里运行，每条 bin\/agent-env 命令前面加上 AGENT_ENV_MANIFEST=~\/mine\/habits\.toml。/);
  // no manifest: the engine's folder is not presented as the personal one
  const none = { dir: "~/agent-env", noManifest: true };
  const note = "注意：面板没找到我的个人清单（manifest/habits.toml），~/agent-env 只是 agent-env 工具自己的目录。先帮我找到放这份清单的文件夹，找不到就问我；找到后运行命令时在前面加上 AGENT_ENV_MANIFEST=<清单的路径>。";
  assert.equal(aiSentence("zh", "重新查", ["bin/agent-env plugin repos --refresh"], none), `帮我重新查（在 ~/agent-env 目录里运行 bin/agent-env plugin repos --refresh）。${note}`);
  assert.ok(aiStepsText("zh", "t", WSL, { ...none, machine: "wsl" }).endsWith(note));
  assert.ok(aiFixText("zh", "p", "f", none).endsWith(note));
  assert.match(aiSentence("en", "check again", ["bin/agent-env check"], none), / Note: the panel found no personal manifest \(manifest\/habits\.toml\); ~\/agent-env is only the folder of the agent-env tool itself\. First find the folder that holds my manifest, and ask me if you cannot; then put AGENT_ENV_MANIFEST=<path to the manifest> before each command\.$/);
  assert.equal(aiLead("zh", "fix-drift", none).split("\n")[2], note);
  assert.equal(aiLead("zh", "fix-drift", named).split("\n")[1], "在 ~/agent-env 目录里做，命令都在这里运行，每条 bin/agent-env 命令前面加上 AGENT_ENV_MANIFEST=~/mine/habits.toml。");
});

test("copy for AI without the portal: the lead is the ai-tasks.json preset's own request, then the folder to work in", () => {
  const tasks = JSON.parse(readFileSync(join(import.meta.dirname, "ai-tasks.json"), "utf8"));
  for (const p of tasks.tools.env.presets) {
    assert.equal(presetWhat("zh", p.id), p.what.zh);
    assert.equal(aiLead("zh", p.id, { dir: DIR }), `${p.what.zh}\n在 ~/agent-home 目录里做，命令都在这里运行。\n下面是面板给出的数据：`);
    assert.equal(aiLead("en", p.id, { dir: DIR }), `${p.what.en}\nWork in the folder ~/agent-home; run every command there.\nThis is what the Environments panel says:`);
  }
  assert.equal(presetWhat("zh", "no-such-preset"), "");
  assert.equal(aiLead("en", "fix-drift").split("\n").length, 2, "no folder known: none is named");
  // a task that edits the manifest is told which file
  assert.equal(aiLead("zh", "adjust-plugins", { dir: DIR, manifest: "~/agent-home/manifest/habits.toml" }).split("\n")[2], "要改的清单是 ~/agent-home/manifest/habits.toml。");
});

test("ai-tasks.json names no folder of anyone's machine", () => {
  const text = readFileSync(join(import.meta.dirname, "ai-tasks.json"), "utf8");
  assert.equal(JSON.parse(text).tools.env.cwd, "engine");
  assert.doesNotMatch(text, /coding\/agent-env|\/Users\/|\/home\//);
});

test("work folders: the manifest the engine reads, found like engine/overlay.py and engine/agent_env.py", () => {
  const has = (...files: string[]) => (p: string) => files.includes(p);
  const M = "manifest/habits.toml";
  // the engine vendored two folders below the personal repository, which has its own bin/agent-env
  assert.deepEqual(
    workFolders({ start: "/srv/agent-home/vendor/agent-env", env: {}, exists: has(`/srv/agent-home/${M}`, "/srv/agent-home/bin/agent-env") }),
    { run: "/srv/agent-home", overlay: "/srv/agent-home", manifest: `/srv/agent-home/${M}`, found: true, env: "" },
  );
  // no bin/agent-env of its own: commands run in the engine's folder, which finds the manifest by walking up
  assert.deepEqual(
    workFolders({ start: "/srv/agent-home/vendor/agent-env", env: {}, exists: has(`/srv/agent-home/${M}`) }),
    { run: "/srv/agent-home/vendor/agent-env", overlay: "/srv/agent-home", manifest: `/srv/agent-home/${M}`, found: true, env: "" },
  );
  // the manifest in the engine's own folder wins over one further up
  assert.deepEqual(workFolders({ start: "/srv/a/b", env: {}, exists: has(`/srv/a/b/${M}`, `/srv/${M}`) }), { run: "/srv/a/b", overlay: "/srv/a/b", manifest: `/srv/a/b/${M}`, found: true, env: "" });
  // at most three folders up, like overlay.py's MAX_UP
  assert.equal(workFolders({ start: "/a/b/c/d/e", env: {}, exists: has(`/a/${M}`) }).found, false);
  assert.equal(workFolders({ start: "/a/b/c/d/e", env: {}, exists: has(`/a/b/${M}`) }).overlay, "/a/b");
  // nothing found: no folder is called the personal one; commands can only run in the engine's own
  assert.deepEqual(workFolders({ env: {}, exists: has() }), { run: REPO_ROOT, overlay: "", manifest: "", found: false, env: "" });

  // $AGENT_ENV_MANIFEST is any path (agent_env.py manifest_arg), not only one that ends in manifest/habits.toml;
  // a command run in the engine's folder would not find it, so the variable goes with the command
  assert.deepEqual(
    workFolders({ start: "/opt/agent-env", env: { AGENT_ENV_MANIFEST: "/srv/mine/my-habits.toml" }, exists: has("/srv/mine/my-habits.toml") }),
    { run: "/opt/agent-env", overlay: "/srv/mine", manifest: "/srv/mine/my-habits.toml", found: true, env: "AGENT_ENV_MANIFEST=/srv/mine/my-habits.toml" },
  );
  assert.deepEqual(
    workFolders({ start: "/opt/agent-env", env: { AGENT_ENV_MANIFEST: `/srv/mine/${M}` }, exists: has(`/srv/mine/${M}`) }),
    { run: "/opt/agent-env", overlay: "/srv/mine", manifest: `/srv/mine/${M}`, found: true, env: `AGENT_ENV_MANIFEST=/srv/mine/${M}` },
  );
  // it wins over a manifest found by walking up, as in the engine
  assert.equal(workFolders({ start: "/srv/home/vendor/agent-env", env: { AGENT_ENV_MANIFEST: "/srv/other.toml" }, exists: has(`/srv/home/${M}`, "/srv/other.toml") }).manifest, "/srv/other.toml");
  // the folder has its own bin/agent-env: commands run there and need no variable
  assert.deepEqual(
    workFolders({ start: "/opt/agent-env", env: { AGENT_ENV_MANIFEST: `/srv/mine/${M}` }, exists: has(`/srv/mine/${M}`, "/srv/mine/bin/agent-env") }),
    { run: "/srv/mine", overlay: "/srv/mine", manifest: `/srv/mine/${M}`, found: true, env: "" },
  );
  // it names the file the engine finds anyway: nothing to carry
  assert.equal(workFolders({ start: "/srv/home/vendor/agent-env", env: { AGENT_ENV_MANIFEST: `/srv/home/${M}` }, exists: has(`/srv/home/${M}`) }).env, "");
  // a relative path is read from the engine's folder, where the panel runs the CLI; the variable carries it whole
  assert.equal(workFolders({ start: "/opt/agent-env", env: { AGENT_ENV_MANIFEST: "../mine/h.toml" }, exists: has("/opt/mine/h.toml") }).env, "AGENT_ENV_MANIFEST=/opt/mine/h.toml");
  // a path with a space is quoted for the shell
  assert.equal(workFolders({ start: "/opt/agent-env", env: { AGENT_ENV_MANIFEST: "/srv/my files/h.toml" }, exists: has("/srv/my files/h.toml") }).env, "AGENT_ENV_MANIFEST='/srv/my files/h.toml'");
  // it names a file that is not there: the engine would fail, and the page says no manifest was found
  assert.deepEqual(workFolders({ start: "/opt/agent-env", env: { AGENT_ENV_MANIFEST: "/srv/gone.toml" }, exists: has(`/opt/agent-env/${M}`) }), { run: "/opt/agent-env", overlay: "", manifest: "", found: false, env: "" });
});
