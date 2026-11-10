// "Copy for AI": the people who read this panel do not type commands, they tell an AI agent what to do. Wherever the
// page shows a command, its copy button copies plain text that says what to do, with the command inside it for the
// agent. Each text is correct on its own: it says where every command runs, keeps the order of several steps, asks
// to be shown anything that writes, and says who "you" is when it quotes the engine. Pure text, no DOM; the same
// inputs give the same text.
import tasks from "./ai-tasks.json" with { type: "json" };
import type { Lang } from "./i18n.ts";

/** Where commands run on this machine. */
export interface Where {
  /** the folder `bin/agent-env` runs in on this machine, as shown to the user (`~/…`) */
  dir?: string;
  /** `AGENT_ENV_MANIFEST=<path>`: goes before every `bin/agent-env` command (the manifest is not where the engine
   *  looks by itself, see source.ts workFolders) */
  env?: string;
  /** no personal manifest was found: `dir` is only the engine's own folder, and the text says so */
  noManifest?: boolean;
  /** the manifest file, for a task that edits it (aiLead) */
  manifest?: string;
}

export interface SentenceOptions extends Where {
  /** ask the AI to show it first; default: whenever a command is not read-only */
  careful?: boolean;
}

/** One step of a thing to do (model.ts TodoStep, in the page's language). */
export interface AiStep {
  /** "ai": the AI, on this machine; "other_env": on the other machine; "user": the person, wherever the text says */
  who: "ai" | "user" | "other_env";
  text: string;
  command: string | null;
  confirm: boolean;
}

export interface StepsOptions extends Where {
  /** the other machine's name, for steps with who "other_env" */
  machine: string;
}

const strip = (s: string) => s.trim().replace(/[。.；;：:\s]+$/u, "");

/** Commands that only look things up. Every other command may write, so its text asks the AI to show it first. */
export function readsOnly(cmd: string): boolean {
  const c = cmd.trim();
  if (/\s--(yes|write|fix|record|commit|push)\b/.test(c)) return false;
  if (/\s--(dry-run|plan|check)\b/.test(c)) return true;
  if (/^ssh\s+\S+\s+true$/.test(c)) return true;
  // eval re-runs a spot-check: it asks the agent and reads the answer, nothing of the user's changes
  return /^bin\/agent-env\s+(status|check|doctor|eval|plugin\s+repos)\b/.test(c);
}

const withEnv = (cmd: string, o: Where) => (o.env && /^bin\/agent-env(\s|$)/.test(cmd) ? `${o.env} ${cmd}` : cmd);
const CAREFUL: Record<Lang, string> = {
  zh: "会改动或上传东西的步骤，先给我看，等我同意再做。",
  en: "For any step that changes or uploads something, show me first and wait for my yes.",
};
/** The engine writes its steps and fixes to the person; quoted to an AI, "you" would turn into the AI. */
const saysYou = (text: string) => /你|\byou\b|\byour\b/i.test(text);
const YOU: Record<Lang, string> = {
  zh: "引用的文字是面板写给我看的，其中的「你」指我本人，不是你这个 AI。",
  en: "The quoted text was written by the panel for me: “you” (“你”) in it means me, the person, not you, the AI.",
};

function noManifestNote(lang: Lang, o: Where): string {
  if (!o.noManifest) return "";
  if (lang === "zh") return `注意：面板没找到我的个人清单（manifest/habits.toml）${o.dir ? `，${o.dir} 只是 agent-env 工具自己的目录` : ""}。先帮我找到放这份清单的文件夹，找不到就问我；找到后运行命令时在前面加上 AGENT_ENV_MANIFEST=<清单的路径>。`;
  return `Note: the panel found no personal manifest (manifest/habits.toml)${o.dir ? `; ${o.dir} is only the folder of the agent-env tool itself` : ""}. First find the folder that holds my manifest, and ask me if you cannot; then put AGENT_ENV_MANIFEST=<path to the manifest> before each command.`;
}

/** Sentences after the first one: Chinese ones follow each other directly, English ones with a space. */
const tail = (lang: Lang, notes: string[]) => notes.filter(Boolean).map((n) => (lang === "zh" ? n : ` ${n}`)).join("");

/** 「帮我<what>（在 <dir> 目录里运行 <command>）。」 / "Please <what> (in the folder <dir>, run <command>)."
 *  `what` continues the sentence after 帮我 / Please. For one command, or several that all run in this machine's
 *  folder; steps on different machines go through aiStepsText. */
export function aiSentence(lang: Lang, what: string, commands: string[] = [], o: SentenceOptions = {}): string {
  const cmds = commands.map((c) => c.trim()).filter(Boolean);
  const zh = lang === "zh";
  let run = "";
  if (cmds.length) {
    const shown = cmds.map((c) => withEnv(c, o));
    const place = o.dir ? (zh ? `在 ${o.dir} 目录里` : `in the folder ${o.dir}, `) : "";
    const list = shown.length === 1 ? (zh ? `运行 ${shown[0]}` : `run ${shown[0]}`) : zh ? `依次运行：${shown.join("；")}` : `run these in order: ${shown.join("; ")}`;
    run = zh ? `（${place}${list}）` : ` (${place}${list})`;
  }
  const careful = o.careful ?? cmds.some((c) => !readsOnly(c));
  const notes = tail(lang, [careful ? CAREFUL[lang] : "", cmds.length ? noManifestNote(lang, o) : ""]);
  return zh ? `帮我${strip(what)}${run}。${notes}` : `Please ${strip(what)}${run}.${notes}`;
}

const here = (lang: Lang, o: Where) => (lang === "zh" ? (o.dir ? `本机的 ${o.dir} 目录里` : "本机上") : o.dir ? `this machine, in the folder ${o.dir}` : "this machine");

/** One step: where it runs (from its own `who`), its text, its command, and whether it waits for a yes. A step of
 *  the person's names no place: the engine does not say where the person does it, the step's text does. A write
 *  step carries its dry-run form (model.ts TodoStep.command), so that command is named as the dry run. */
function stepLine(lang: Lang, s: AiStep, o: StepsOptions): string {
  const zh = lang === "zh";
  const text = strip(s.text);
  const cmd = s.command?.trim() ?? "";
  const place = s.who === "other_env"
    ? zh ? `在 ${o.machine} 这台机器上` : `On the machine ${o.machine}`
    : s.who === "ai"
      ? zh ? `在${here(lang, o)}` : `On ${here(lang, o)}`
      : zh ? "这一步由我来做" : "This step is mine";
  const shown = cmd && s.who === "ai" ? withEnv(cmd, o) : cmd;
  const dry = s.confirm && /\s--dry-run\b/.test(cmd);
  const sep = text ? (zh ? "。" : ". ") : "";
  let run = "";
  if (cmd && !text.includes(cmd)) {
    if (dry) run = zh ? `${sep}演练用：${shown}；正式执行前先给我看演练结果，等我同意` : `${sep}Its dry run: ${shown}; show me the result and wait for my yes before the real run`;
    else if (s.who === "user") run = zh ? `${sep}命令：${shown}` : `${sep}Command: ${shown}`;
    else run = zh ? `${sep}运行：${shown}` : `${sep}Run: ${shown}`;
  }
  const wait = !dry && (s.confirm || (cmd && !readsOnly(cmd))) ? (zh ? "（先给我看，等我同意再做）" : " (show me first and wait for my yes)") : "";
  return zh ? `${place}：${text}${run}${wait}` : `${place}: ${text}${run}${wait}`;
}

/** What follows the steps: who "you" is, and what to do at the person's steps and at the other machine's. A step
 *  that writes says "show me first" itself (stepLine). */
function stepNotes(lang: Lang, steps: AiStep[], o: StepsOptions): string[] {
  const zh = lang === "zh";
  const local = steps.some((s) => s.who === "ai" && s.command);
  return [
    steps.some((s) => saysYou(s.text)) ? YOU[lang] : "",
    steps.some((s) => s.who === "user") ? (zh ? "轮到我做的步骤，停下来告诉我具体做什么，等我说做完了再继续。" : "At a step that is mine, stop, tell me exactly what to do, and go on when I say it is done.") : "",
    steps.some((s) => s.who === "other_env") ? (zh ? `${o.machine} 上的步骤，你连不上那台机器的话，把要运行的内容给我，我去那台机器上做。` : `For the steps on ${o.machine}: if you cannot reach that machine, give me what to run and I will do it there.`) : "",
    local ? noManifestNote(lang, o) : "",
  ];
}

/** A whole thing to do, for one button: every step in order and numbered, each with the place it runs in; a step
 *  without a command carries its text. */
export function aiStepsText(lang: Lang, title: string, steps: AiStep[], o: StepsOptions): string {
  const zh = lang === "zh";
  const head = zh ? `帮我处理这个问题：${strip(title)}。按下面的顺序做，一步做完再做下一步：` : `Please handle this problem: ${strip(title)}. Do these steps in order, one after the other:`;
  const lines = steps.map((s, i) => `${i + 1}. ${stepLine(lang, s, o)}`);
  const notes = stepNotes(lang, steps, o).filter(Boolean).join(zh ? "" : " ");
  return [head, ...lines, notes].filter(Boolean).join("\n");
}

/** One step of a thing to do, for the step's own button: which step of how many, and not before the earlier ones. */
export function aiStepText(lang: Lang, title: string, steps: AiStep[], index: number, o: StepsOptions): string {
  const zh = lang === "zh";
  const s = steps[index];
  const n = index + 1;
  const before = index > 0 ? (zh ? "；前面的步骤还没做完的话，先告诉我，不要直接做这一步" : "; if the earlier steps are not done yet, tell me first instead of doing this one") : "";
  const head = zh ? `帮我做「${strip(title)}」的第 ${n} 步（共 ${steps.length} 步${before}）。` : `Please do step ${n} of ${steps.length} of “${strip(title)}”${before}.`;
  const line = `${stepLine(lang, s, o)}${zh ? "。" : "."}`;
  return `${head}${zh ? "" : " "}${line}${tail(lang, stepNotes(lang, [s], o))}`;
}

/** A fix the engine wrote as a sentence (it may name several commands, in an order that matters): quoted whole. */
export function aiFixText(lang: Lang, problem: string, fix: string, o: Where = {}): string {
  const zh = lang === "zh";
  const where = zh
    ? `要运行命令的话，在${here(lang, o)}运行${o.env ? `，每条 bin/agent-env 命令前面加上 ${o.env}` : ""}。`
    : `Any command runs on ${here(lang, o)}${o.env ? `; put ${o.env} before each bin/agent-env command` : ""}.`;
  const notes = tail(lang, [where, saysYou(fix) ? YOU[lang] : "", CAREFUL[lang], noManifestNote(lang, o)]);
  return zh ? `帮我处理这个问题：${strip(problem)}。面板给的做法是：${strip(fix)}。${notes}` : `Please handle this problem: ${strip(problem)}. The panel's fix is: ${strip(fix)}.${notes}`;
}

interface Preset {
  id: string;
  what: Record<Lang, string>;
}

/** What the AI is asked to do by one ai-tasks.json preset, in the page's language; "" for an unknown preset. */
export function presetWhat(lang: Lang, preset: string): string {
  const p = (tasks.tools.env.presets as Preset[]).find((x) => x.id === preset);
  return p ? p.what[lang] : "";
}

/** The lines that go above the problem data when there is no portal to hand the task to and the user copies it to
 *  an AI instead: the preset's own request, then where to work. The data (JSON) follows on the next line. */
export function aiLead(lang: Lang, preset: string, o: Where = {}): string {
  const zh = lang === "zh";
  const where = o.dir
    ? zh
      ? `在 ${o.dir} 目录里做，命令都在这里运行${o.env ? `，每条 bin/agent-env 命令前面加上 ${o.env}` : ""}。`
      : `Work in the folder ${o.dir}; run every command there${o.env ? `, with ${o.env} before each bin/agent-env command` : ""}.`
    : "";
  const file = o.manifest && !o.noManifest ? (zh ? `要改的清单是 ${o.manifest}。` : `The manifest to edit is ${o.manifest}.`) : "";
  return [presetWhat(lang, preset), where, file, noManifestNote(lang, o), zh ? "下面是面板给出的数据：" : "This is what the Environments panel says:"].filter(Boolean).join("\n");
}
