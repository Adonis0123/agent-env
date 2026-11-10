# Targets fetch their plugins; this machine pushes nothing into a target

Every target is an ordered list of plugins plus a patch layer. Each plugin is a git repository with a visibility
(public, personal, company, local). Targets fetch the plugins themselves; this machine only builds, scans and
commits them, and pushes after the user confirms. The trigger was a cloud-hosted bot: this machine cannot write
its settings, and a 3,000-character rules text pasted into its memory was cut short. The model borrows from the
profile bundles of DeepSeek Harness (ordered bundles plus a user patch) and from Hermes Agent's profile
distribution (git repositories as the channel; credentials and memory never travel).

## Considered options

- Push the rules into every target (file transfer, pasting): cloud-hosted targets cannot be written to; every new
  kind of target needs its own push; the only check is reading the text before it is cut.
- Let a bot read the whole environment repository: a token cannot be limited to a directory, so a read-only token
  still reads the whole private repository, and prompt injection that leaks a token is a known attack.
- A visibility per section: markers everywhere and rules for combining them; with the visibility on the plugin,
  what a target gets is just its plugin list.

## Consequences

- A cloud-hosted target's token reads only the plugin repositories it installs; a leak exposes what it already
  reads. A target with public plugins only needs no token.
- A target that is offline or fails to pull uses its last pull and says it may be stale.
- Plugins use the Claude Code plugin layout (Claude Code, Codex, Grok Build and Copilot install it directly) with
  thin manifests for other hosts. Host plugin formats cannot carry always-on rules, so rules sit in the plugin's
  root `AGENTS.md` and the engine delivers them to each host.
- Exception: the author's own machines do not pull the plugin repositories. They compose the rules file locally
  from the target's plugins (`rules_output`, `engine/compose.py`): the sources are already there, and the patch
  layer keeps the half-sentences the published public plugin leaves out.

## Sources

- DeepSeek Harness: https://github.com/deepseek-ai/deepseek-harness
- Hermes Agent profile distribution: https://github.com/NousResearch/hermes-agent
- Claude Code plugins: https://code.claude.com/docs/en/plugins-reference
- Agent Skills specification: https://agentskills.io/specification
- `npx skills`: https://github.com/vercel-labs/skills
