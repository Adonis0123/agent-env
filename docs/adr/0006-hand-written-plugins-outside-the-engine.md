# Hand-written plugins live in a separate marketplace; the engine only checks them

The engine builds one rules bundle per visibility, one repository each, and targets pull it (ADR 0005). People also
want to install single behaviours, such as one hook or one skill, and combine them freely. Those plugins are written
by hand and published from one separate multi-plugin marketplace repository (`plugins/<name>/`, listed by relative
path). The engine does not generate, version or install them. It only checks that a target's host settings point at
them. One source per plugin means nothing is copied from one pipeline into the other.

## Considered options

- Teach the generator to emit hooks and many plugins: the generator mirrors its output repository, so hand-written
  plugins there would be deleted. A repository also has one visibility, which fits bundles, not a catalogue.
- List the generated rules bundle in the people-facing marketplace: a bundle carries one person's working rules for
  their own targets, and people browsing for single behaviours would install them by mistake.
- One repository per hand-written plugin: every plugin then needs its own CI and its own `marketplace add`.

## Consequences

- The author's machines load the hand-written plugins in place from a local clone (`CLAUDE_CODE_PLUGIN_DIRS`, or
  hook files pointing at the clone). One edit reaches every host in the next session, with no release step.
- Hand-written plugins carry no `version`, like generated ones. Installs follow commits, and CI runs
  `claude plugin validate --strict` and each plugin's tests.
- When a hook script moves from the hooks source into a plugin, remove its `[hooks]` entry in the same change, or
  `check` fails on the missing file. A `contains` expectation on the host settings then checks the plugin path.
- A skill published both ways is a duplicate to remove from the bundle. The bundle should reference the
  marketplace copy instead.

## Sources

- Claude Code plugin marketplaces: https://code.claude.com/docs/en/plugin-marketplaces
- Multi-plugin marketplace layout: https://github.com/anthropics/claude-plugins-official
