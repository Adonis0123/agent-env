# External tools only translate formats; MCP does not go through a gateway

MCP servers, hooks and permissions have a different format in every host; translating them is left to existing
tools such as rulesync or add-mcp. Classification, assembly and role-based rule selection stay in this engine:
those tools know nothing of environment profiles or of how a chat bot loads its rules, and handing everything to
them would mean splitting `AGENTS.md` into many files and giving up direct links.

MCP does not go through a gateway (1mcp, MetaMCP and the like): with a handful of servers, OAuth that each host
must complete on its own, and servers that only work on the desktop machine, a gateway would be one more thing
that takes every host down at once. Revisit when there are more than about ten servers or several people share
them.
