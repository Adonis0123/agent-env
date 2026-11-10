# One engine for every environment, the personal layer kept apart

Numbers follow the original decision log; decisions 0001 and 0002 were about one person's WSL setup and are not
part of this engine.

The engine started as the bootstrap of one backup machine: a per-file classification, environment profiles and
an assembler. Instead of a separate script set per environment (a chat bot on the laptop, a WSL box, remote
agents later), the same classification, manifest, assembler, doctor and panel serve them all. So the engine can
be public, it hard-codes no path, machine or account: the personal layer (manifest, profiles, role tables, MCP
lists, private skills) lives in the user's own private repository, which runs this engine from a checkout or
vendors it (`engine/overlay.py`).

## Considered options

- A script set per environment: classification and assembly written twice, drift hard to see.
- skillshare, rulesync and similar tools as they are: they distribute along one axis (the host); they have no
  per-environment profiles and no role-based rule selection, chat bots included.

## Consequences

- A personal path in engine code is a bug; a privacy scan and a path check run before every release.
- Shared habits that are meant to be public (rules, docs, hook scripts) are not hand-written templates: the
  engine builds them as a public plugin from the sources the author actually uses (ADR 0005).
