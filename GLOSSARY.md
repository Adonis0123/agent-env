# Glossary

The words the code, the CLI and the docs use. The CLI prints the Chinese name, so it is given too.

## Machines and places

**Environment** (环境): one machine that runs agents: a laptop, a WSL install, a remote server, or the vendor's
machine behind a cloud-hosted bot. Declared in `[environments.<name>]`; the name is also its status branch,
`status/<name>`.
_Avoid_: platform, host

**Target** (落点): one place in an environment that receives rules and skills, usually one host (agent CLI or
chat bot) there. Declared as `[[targets]]` with `id = "<env>/<host>"`.
_Avoid_: destination

**Host** (宿主): the program that runs an agent and reads its rules: Claude Code, Codex, Grok, pi, Cursor,
Hermes, a cloud chat bot.
_Avoid_: client, tool

**Source environment** (源环境): the machine where the rules source is edited and plugins are built and
published. Cloud-hosted environments name it in `driven_from`.

**Remote-exec environment** (远程云主机，本机投递): a server this machine reaches only through a command prefix
(`exec`, for example an ssh wrapper). Checked and set up from here with `--env <name>`.

**Cloud-hosted target** (云端托管落点): a target this machine cannot write to at all, such as a vendor-hosted
bot. It fetches its plugins itself, led by a pointer rule.

## Rules and plugins

**Rules source** (规则源): where people edit rules: one Markdown file per owner (`public.md`, `personal.md`,
`local.md`) in `[sources] rules_src`. Every `## ` section is written in exactly one file and registered in
`[rule_sections]` with roles and a visibility.

**Plugin** (插件): a repository of rules (`AGENTS.md`), own skills, vendored third-party skills (copied with their upstream license) and the sources of the other third-party skills, with
one visibility. Built by `plugin build`, published by `plugin publish`.
_Avoid_: bundle, export

**Visibility** (可见性): who may read a plugin: public (anyone), personal (the owner and the cloud targets they
trust), company (colleagues and company machines), local (this machine only). Anything without one is local.

**Patch layer** (补丁层): the local plugin that comes last in a target's plugin list. What it overrides is
reported as 已覆盖 (overridden), with the plugin it came from.

**Compose** (合成): how a target on one of the user's own machines gets its rules: the target's plugins are
merged, in order, into one file (`rules_output`) on that machine. Later plugins append to sections of the same
name. The environment's own sync may do it (`sync_command`); otherwise `render --write`.

**Pull** (定时拉取): how a target on a remote server gets its plugins: `agent-env-pull` on the server, run by a
systemd user timer, fetches each plugin repository and joins their `AGENTS.md` in order.

**Pointer rule** (指路规则): a short rule stored in a cloud-hosted bot's own memory that tells it which plugin
repositories to pull and in what order.

**omit** (去掉的半句): exact half-sentences a plugin drops from the sections it takes, because they only hold on
the author's machines. A target with a patch layer keeps them.

## Checks

**Expectation** (期望): one `[[targets.expect]]` entry that doctor checks, such as a link, a line a file must
contain, a generated or composed file, a service, a login.

**Cell states** (状态): synced (已同步), customized (已定制), drift (漂移), missing (缺失), unknown (未知).

**Personal layer** (个人层): everything specific to one user: the manifest, profiles, private skills, the list of
repositories. It lives in the user's own private repository, never in this one.
