# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- README rewritten in the shape of sideby's, all in a light theme: an illustrated banner, a 25-second demo GIF, an architecture diagram, the paragraph to paste
  to an AI, the demo and links; a Chinese README (`README.zh-CN.md`). The setup steps, who it is for, what the user
  does themselves and the demo environments moved to `docs/guide/setup.md`; commands, layout and development to
  `docs/guide/commands.md`. Every pointer to "Use it for your setup" now names `docs/guide/setup.md`.
- `scripts/record-demo.ts` retakes `docs/assets/demo.gif` from `docs/assets/demo.html` with real command output
  (`--frames` for a quick look, `--mp4` for a video); `scripts/shoot-panel.ts` retakes the panel screenshots.
- Vendored third-party skills: `vendor = ["name"]` in a plugin copies those `skill_sources` byte for byte from the
  local copy into `skills/<name>/`, with `UPSTREAM.md` (source, URL, path, upstream and local git tree ids,
  modified) and `UPSTREAM-LICENSE` inside the folder and a `vendor.json` index at the root. They leave
  `skills-lock.json`, and the pointer rule drops the `npx` step when no lock entry remains. Upstream licenses are
  read from `[sources] licenses` (default `<state dir>/licenses`, `<owner>-<repo>.LICENSE`); a missing one fails
  `check` and the build.
- `plugin vendor-diff --plugin <p> --skill <s>`: compare a vendored skill's local copy with upstream HEAD (network),
  print the diff, and with `--write-license` save the upstream license. Updating stays manual: merge upstream into
  the local copy, then rebuild.
- `profiles/_template/starter/`: a first personal layer (one machine, one agent host) with a `bin/agent-env`
  wrapper that runs the engine cloned into `vendor/agent-env/`.
- Panel: every copy button copies a plain sentence for an AI agent ("Copy for AI") instead of a command: what to
  do, on which machine and in which folder, the command, and a request to show writes first. Multi-step items are
  numbered, and each step has its own "Copy this step for AI".
- Panel: "查看内容" / "View" next to a listed file or folder on the 关系图 and 插件 tabs shows it read-only in the
  page (this machine only, at most 256 KB, never paths that look like keys or passwords). When the hosting portal
  offers its shared "Open in" control, that button appears next to it.

### Changed

- Panel wording in Chinese: 漂移 is now 不一致, 抽测 is 规则测试, 本机按插件合成 is 本机自动生成; `skills/agent-env/SKILL.md`
  maps the page words to the engine terms.
- Panel at phone width: commands wrap between words and never after a hyphen, status icons stay on the line of
  their title, and the list of healthy agents wraps instead of being cut.
- The pointer rule now tells a cloud-hosted bot how to get third-party skills: run `npx -y skills experimental_install`
  in a plugin that has `skills-lock.json` and read `.agents/skills/`, or fetch each skill from its `sourceUrl` and
  `skillPath` with git. The default pointer limit is 400 characters (was 300).
- README opens with who it is for, the public plugin for one machine and one agent, and a paragraph to paste to
  an AI agent ("Send this to your AI"); it lists what the user does themselves, and "Use it for your setup" has
  exact commands. `skills/agent-env/SKILL.md` starts with a "what the user says → what the agent does" table.
- `doctor`, `status` and `eval` fail (exit 1) when `--env` names an environment that is neither declared in
  `[environments]` nor used by a target, and list the names that are.
- Error messages for a missing manifest, unset `[sources] docs` / `hooks`, an old Python, no environment matching
  this machine and a changed host version say what to do next.
- Panel: failure, empty and unknown states say what is wrong, what it means and what to tell the AI; jargon is
  removed from zh and en strings; the English tabs are Machines and Map and fit one row at 390px.
- Panel: `ai-tasks.json` no longer names a personal folder (`cwd` is `"engine"`). The panel finds the folder
  that holds `manifest/habits.toml` the way the engine does, including `AGENT_ENV_MANIFEST`, puts it in every AI
  fact and sentence as `workdir`, and says so when there is none.

### Fixed

- The 插件 tab no longer shows a stale repository cache: `plugin publish` marks a repository it pushed to as not
  empty, and `status --all` re-asks (within 4 seconds) only the repositories the cache lacks, such as a renamed
  one, or last answered over 24 hours ago. A host that cannot be reached leaves the cache as it was.

## [0.1.0] - 2026-11-10

First public release: the engine, split from a private setup that runs it on macOS, WSL, a Linux server and a
cloud-hosted bot.

### Added

- `bin/agent-env` with `check`, `render`, `doctor`, `status`, `rollback`, `deliver`, `eval`, `skills`, `promote`
  and `plugin` (`build`, `publish`, `pointer`, `repos`).
- Plugins with a visibility (public, personal, company, local): Claude Code plugin layout plus Cursor and pi
  manifests, own skills copied, third-party skills recorded by source, byte-stable builds, a resumable publish
  ledger, and a dry run before every push.
- Three ways a target gets its plugins: composed on the user's own machines (`rules_output`), pulled on a timer
  by a remote host (`pull`, `features/plugin-pull/`), or pulled by a cloud-hosted bot from a pointer rule.
- Privacy guard for public and personal plugins: gitleaks, the engine's own secret rules, home paths in every
  spelling, emails, credential files, and paths that point outside the plugin.
- Doctor expectations: `link`, `contains`, `generated`, `composed`, `plugins`, `pulled`, `delivered`, `token`,
  `service`, `login`, `broken-links`, `secrets`, `company-skills`, `portal`, plus MCP and host-version checks.
- The Environments panel (`panel/`): to-dos, per-machine view, matrix, relation graph and plugins tab.
- The runbook skill `skills/agent-env/`, the new-environment template `profiles/_template/`, and a demo
  personal layer `examples/demo/`.
- `engine/overlay.py`: the engine finds the personal repository that vendors it (nearest `manifest/habits.toml`).
- `[git_hosts."<host>"]` in the manifest: how to ask a host other than github.com whether a repository is public.
- `[environments.<env>] sync_command`: an environment whose own sync writes its composed rules file.

[0.1.0]: https://github.com/Adonis0123/agent-env/releases/tag/v0.1.0
