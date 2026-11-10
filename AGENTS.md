# AGENTS.md

Rules for agents and people changing this repository. Terms: `GLOSSARY.md`. Design: `docs/plugins-design.md`.

- The engine never holds personal data: no absolute home paths, machine names, account names, company names,
  hostnames or ports of a real setup. Paths come from the manifest and are written with `~`; examples use the
  demo's fictional names (`examples/demo/`). A personal path in engine code is a bug.
- Every error the engine prints says how to fix it (`修复：…`), and `check` never crashes: a malformed manifest
  becomes FAIL lines.
- Generated output is byte-stable: the same inputs give the same bytes, with no timestamps in file contents.
- Writes are dry runs first. Anything that pushes, installs on a remote host or replaces a file says what it will
  do and needs `--yes` (or `--write`); files are snapshotted before they are replaced.
- Behaviour changes come with tests. Run `python3 -m unittest discover -s tests` and `npm --prefix panel test`;
  both must pass.
- Before a release: `gitleaks dir .` is clean with `.gitleaks.toml`. A real false positive gets a narrow line in
  `.gitleaks.toml` or `.privacy-allow`, never a global allow.
- Commit messages: emoji + Conventional Commits, in English.
