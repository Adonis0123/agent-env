# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Vendored third-party skills: `vendor = ["name"]` in a plugin copies those `skill_sources` byte for byte from the
  local copy into `skills/<name>/`, with `UPSTREAM.md` (source, URL, path, upstream and local git tree ids,
  modified) and `UPSTREAM-LICENSE` inside the folder and a `vendor.json` index at the root. They leave
  `skills-lock.json`, and the pointer rule drops the `npx` step when no lock entry remains. Upstream licenses are
  read from `[sources] licenses` (default `<state dir>/licenses`, `<owner>-<repo>.LICENSE`); a missing one fails
  `check` and the build.
- `plugin vendor-diff --plugin <p> --skill <s>`: compare a vendored skill's local copy with upstream HEAD (network),
  print the diff, and with `--write-license` save the upstream license. Updating stays manual: merge upstream into
  the local copy, then rebuild.

### Changed

- The pointer rule now tells a cloud-hosted bot how to get third-party skills: run `npx -y skills experimental_install`
  in a plugin that has `skills-lock.json` and read `.agents/skills/`, or fetch each skill from its `sourceUrl` and
  `skillPath` with git. The default pointer limit is 400 characters (was 300).

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
