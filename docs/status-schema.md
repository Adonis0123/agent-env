# `agent-env status --json` 格式（schema 1）

`doctor` 产出、状态分支保存、Local Tools「环境」面板读取的都是这一份格式。面板只读它，不自己去探测落点。

## 命令

- `bin/agent-env status --json`：当前环境实时体检的结果，即一个「环境状态」。
- `bin/agent-env status --json --all`：当前环境的实时结果，加上各 `status/<环境>` 分支上最近一次上报的结果，合成一个「全部状态」。

## 全部状态

```json
{
  "schema": 1,
  "generated_at": "2026-11-09T08:00:00Z",
  "environments": [ <环境状态>, ... ],
  "errors": [ { "env": "wsl", "message": "读不到 status/wsl 分支", "fix": "在 WSL 跑 bin/agent-env status --commit" } ]
}
```

## 环境状态

```json
{
  "schema": 1,
  "env": "mac",
  "source": "live | branch",
  "generated_at": "2026-11-09T08:00:00Z",
  "engine_rev": "ae84677",
  "summary": { "synced": 30, "customized": 4, "drift": 1, "missing": 0, "unknown": 2 },
  "check": { "pass": 51, "warn": 1, "fail": 0 },
  "targets": [ <落点状态>, ... ],
  "eval": { "ran_at": null, "results": [] }
}
```

- `source`：`live` 表示刚在本机跑出来的结果，`branch` 表示从状态分支读到的结果。
- `checked_from`（可选）：云端托管环境（`transport = "cloud-hosted"`，如 `grok-bot`）的状态才有，值是做这次检查的那台机器的环境名；`source` 为 `live` 才是这次实时检查，`branch` 是那台机器上次上报的结果；这类环境的检查只读那台机器的状态（插件生成结果、指路规则记录、token 到期日），`status --all` 在它的 `driven_from` 机器上会直接带上它。
- `error`（可选）：远程环境（`transport = "remote-exec"`）整体没检查成功时才有，形如 `{ "message": "远程执行超时（180 秒没有结束）", "fix": "..." }`，比如登录失效、主机不可达、超时。此时该环境的远程检查格都是 `unknown`，`--all` 的 `errors` 里也会多一条同样的内容，`doctor` 退出码为 1。
- `generated_at` 用 UTC ISO 8601 格式。面板拿它和当前时间比，超过 7 天的环境标黄，并显示最后上报时间。
- `check` 是 `habits_check` 的计数。
- `eval` 是 P5 的行为抽测。没跑过时，`ran_at` 为 `null`。`results` 每项：

  ```json
  { "target": "mac/codex", "case": "external-send-asks", "state": "pass | fail | err | skip", "runs": 3, "passed": 3, "detail": "" }
  ```

  `err` 表示宿主超时或崩溃（结论 `UNVERIFIED`），`skip` 表示该用例不适用该落点。

  - 来源：`bin/agent-env eval` 写的 `${XDG_STATE_HOME:-~/.local/state}/agent-env/eval/<环境>.json`，`status` 只读它。没跑过时是上面的空形态。
  - `runs` 是实际跑的次数（超时重跑也算），`passed` 是通过次数。运行器缓存命中（输入没变、上次 PASS）记 `pass`，`runs` 为 0，`detail` 写明走了缓存。
  - 落点写了 `eval_skip` 时只有一条结果，`case` 为 `"-"`，`state` 为 `skip`，`detail` 是原因。
  - 结果按落点自己的环境存：`eval --target grok-bot/bot` 在 Mac 上跑也写进 `eval/grok-bot.json`。
  - `ignored_targets`（可选）：记录文件里落点已不在清单该环境的结果（落点改名或搬走后的旧记录）不进 `results`，落点 id 列在这里；记录文件本身不改。
  - 运行器退出码 3（只有 ERR）对应的组合都是 `err`。

## 落点状态

```json
{
  "id": "mac/hermes",
  "host": "hermes",
  "env": "mac",
  "roles": ["bot", "cron"],
  "verified": true,
  "host_version": { "recorded": "0.9.1", "current": "0.9.3", "changed": true },
  "habits": [ <习惯格>, ... ]
}
```

- `host_version`：`recorded` 是核对分类表时登记的宿主版本，`current` 是当前探测到的版本，探测不到时为 `null`。`changed` 为 true 时，面板提示重新核对分类表。

## 习惯格

矩阵里的每一格就是一条习惯格：

```json
{
  "kind": "rules | skills | hooks | mcp",
  "name": "AGENTS.md",
  "state": "synced | customized | drift | missing | unknown",
  "layer": "safety | env | role | host",
  "detail": "生成文件与清单一致",
  "fix": "bin/agent-env render --target mac/hermes --write"
}
```

`state` 的五种取值：

| 值 | 含义 |
| --- | --- |
| `synced` | 和源一致 |
| `customized` | 按档案或角色做了预期内的定制，`layer` 写明是哪一层 |
| `drift` | 和期望不一致 |
| `missing` | 应该有但没有 |
| `unknown` | 无法核实，比如云端托管落点或未核实的落点。`detail` 写明缺什么证据；云端落点写最后一次投递的内容哈希和时间 |

- `layer` 取冲突顺序里的层级，见设计文档。
- `drift` 和 `missing` 必须带 `fix`，可以是一条命令，也可以是一句修法。其余状态的 `fix` 可以为空字符串。
- `warn`（可选，`true`）：状态仍是 `synced`，但快到期要提醒，如插件 token 7 天内到期；`fix` 写提前要做的事，`doctor` 显示为「提醒：」。
- `expect_kind`（schema 1 新增）：产出这一格的检查，取 `[[targets.expect]]` 的 kind（如 `link`、`generated`、`company-skills`），MCP 比对格为 `mcp`，账号目录格为 `accounts`；旧报告和清单里无法识别的期望没有这个字段。

## 稳定性

- 字段只增不改。删除或改名字段时，`schema` 加 1。
- 输出里不出现密钥值。MCP 只写变量名和 URL 主机名。

## 待办（`issues`，schema 1 新增，面板 v5 起使用）

`status --json --all` 的「全部状态」新增顶层 `issues`：引擎把所有要处理的事算成一张列表，面板三个标签（待办 / 按机器 / 总览）都只从它取标题、处理人和计数，不自己推断。单环境 `status --json` 也带 `issues`（只含本环境）。

```json
{
  "id": "grok-bot/bot:rules:指路规则",
  "env": "grok-bot",
  "target": "grok-bot/bot",
  "category": "habit | eval | eval_error | host_version | env_unreadable | env_stale",
  "state": "drift | missing | fail | err | changed | unreadable | stale",
  "actor": "ai | user | other_env | notice",
  "title_zh": "Grok Bot 的指路规则还没投递",
  "impact_zh": "Grok Bot 还没收到这份规则，新加的规则它不知道",
  "steps": [
    { "who": "ai", "text_zh": "生成指路规则，写到待投递文件", "command": "bin/agent-env plugin pointer --target grok-bot/bot", "confirm": false },
    { "who": "user", "text_zh": "在 Grok Bot app 里粘贴发送，让它存进 memory，等它回复存好", "command": null, "confirm": false }
  ],
  "ai_task": "fix | prepare | logs | null",
  "ref": { "kind": "rules", "name": "指路规则", "case": null }
}
```

- `id`：稳定键，`<target 或 env>:<类别细分>:<名称>`，同一问题每次体检相同。
- `actor`：`ai` AI 能自己修；`user` 要用户亲手做或确认；`other_env` 要去另一台机器做；`notice` 只是提醒（如抽测运行器自己出错、`err`）。
- `title_zh` 一句人话标题（不含 kebab-case 用例名等原始术语，原词放 `ref`）；`impact_zh` 一句影响。英文界面用 `title_en` / `impact_en`（可选，缺省时面板显示中文）。
- `steps`：按顺序。`command` 单独复制；写操作（投递 `--yes`、重启、改远程）那一步 `confirm: true`，命令给演练版（`--dry-run` 或不带 `--yes`），说明里写「先看演练再确认」。
- `ai_task`：面板「让 AI 修 / 准备 / 查日志」按钮用哪个预设。面板每条待办的主按钮都是 AI 按钮（`other_env` 除外，主按钮是复制命令），步骤收在「…」里：`actor: ai` 用「修复」；`actor: user` 且 `ai_task: fix` 也用「修复」，AI 做完它能做的，停在 `who: user` 的那一步，用户做完后接着做（云端落点粘贴指路规则、远程投递前确认）；其余 `user` 和 `other_env` 用「准备」，收尾归用户（服务重启、密钥、token）；`notice` 用「查日志」。
- 宿主升级（`host_version`）归 `ai` / `fix`：步骤是查发布说明、在那台机器实测、有变就停下报告、没变经用户确认后改 `host_version.recorded`（`confirm: true`，没有可复制的命令）。
- 排序：`env_unreadable`、`env_stale`、`missing`、`drift`、`fail`、`host_version`、`eval_error`；同一落点同类合并（如抽测出错 ×2 合成一条，`ref.case` 为逗号分隔）。
- `customized`、`synced`、`unknown` 不产生待办。
- 计数口径：待办总数 = `len(issues)`；按机器卡片的「要处理」= 该 env 的 issues 数；总览「只看有问题」= 有 issue 的落点数（另显示 issues 总数）。

### 抽测用例中文名

清单 `[eval_case_names]` 一处定义（如 `"external-send-asks" = "对外发送前先问你"`），引擎把它写进 `eval.results[].case_zh`，`issues` 的标题也用它。没定义的用例退回原名。

### 术语表

`status --json --all` 顶层新增 `glossary`：`{ "<键>": { "label_zh": "...", "explain_zh": "..." } }`，覆盖五态、四类习惯（规则 / Skills / Hooks / MCP）、actor 四种、抽测结果四种、定制层。面板的图例和术语表只读它，一处定义。

补充（引擎实现时新增，向后兼容）：`ref.detail` 保留原始说明；清单 `[env_names]`、`[host_names]` 和落点 `display_name` 给标题用的显示名；抽测没过按落点合并成一条；环境 `error` 只在顶层 `errors` 出现一次，同时生成一条 `env_unreadable` 待办，不重复。`--all` 的每个环境状态里也带该环境的 `issues` 子集。

## 关系（`relations`，schema 1 新增，面板「关系图」使用）

`status --json --all` 的「全部状态」顶层新增只读字段 `relations`，并给每个习惯格加一个 `origin`。两者都在读状态时按当前清单算出，不写进 `status/<环境>` 分支，单环境 `status --json` 和 `--commit` 存的内容不变。旧报告没有这两个字段，面板就不画来源那一列。

```json
"relations": {
  "sources": { "rules": "~/.claude/AGENTS.md", "docs": "~/agent-rules/docs", "portal": "~/.config/local-tools/portal.json", "manifest": "~/.../manifest/habits.toml" },
  "env_sources": { "mac": { "portal": "~/.config/local-tools/portal.json" } },
  "env_names": { "mac": "Mac", "wsl": "WSL", "vps-helper": "小 helper 的云主机" },
  "targets": [ { "id": "wsl/codex", "env": "wsl", "host": "codex", "name": "Codex", "roles": ["cli"] } ]
}
```

- `sources`：来源键 → 路径（`~/` 开头）。键是清单 `[sources]` 里的 `rules`、`docs`、`hooks`、`skills`、`company_skills`（`eval` 是抽测运行器，不算习惯来源），加上 `portal`（各 `portal` 期望读的门户工具清单，多份用「；」连接）和 `manifest`（清单本身登记的 MCP 和 `skills.disabled`）。顺序即面板来源列的顺序。
- `env_sources`：环境 → {来源键 → 路径}，只放各机器不同的来源。现在只有 `portal`：每个环境取它自己落点的门户清单路径。面板显示某台机器的格子时先查这里，查不到再用 `sources`，免得 Mac 的门户工具显示出 WSL 的路径。
- `env_names`：清单 `[environments]` 里每个环境的显示名（`[env_names]`，没有就用原名）。
- `targets`：清单为每个环境声明的全部落点，包括从没上报的环境。`name` 取落点 `display_name`，没有就取 `[host_names]`，再没有就用 host 原名。
- 习惯格 `origin`：这一格来自哪个来源，取值是 `sources` 的键；`null` 表示是那台机器自身的事实，比如登录、服务、断链、只在那台机器上的设置、账号目录。算法见 `engine/issues.py` 的 `cell_origin`。

## 插件视图（`plugins_view`，schema 1 新增，面板「插件」标签使用）

`status --json --all` 的「全部状态」顶层新增可选字段 `plugins_view`，由 `engine/plugins_view.py` 的 `view` 在读状态时按清单、`repos.json` 和本机状态算出，不写进 `status/<环境>` 分支。它不联网：仓库是公开还是私有只读 `bin/agent-env plugin repos --refresh` 写的缓存（`${XDG_STATE_HOME:-~/.local/state}/agent-env/repo-visibility.json`）；`bin/agent-env plugin repos` 只打印缓存。旧报告没有这个字段，面板的「插件」标签显示空状态，不报错。

```json
"plugins_view": {
  "refresh_command": "bin/agent-env plugin repos --refresh",
  "checked_at": "2026-11-10T08:55:29Z",
  "repos": [ { "key": "github.com/example-user/agent-plugin-public", "name": "agent-plugin-public", "url": "https://github.com/...", "host": "github.com",
               "visibility": "public | private | unknown", "empty": false, "checked_at": "...", "detail": "", "fix": "" } ],
  "sources": [ { "name": "agent-rules", "repo": "github.com/example-user/agent-rules", "path": "~/.claude", "provides": ["rules", "docs", "hooks"] } ],
  "generator": { "name": "agent-env", "repo": "github.com/example-user/my-agent-env", "path": "~/coding/agent-env", "provides": ["company_skills"], "output": "~/.local/state/agent-env/plugins" },
  "plugin_repos": [ { "visibility": "public", "visibility_zh": "公开", "repo": "github.com/...", "plugins": ["demo-public"],
                      "notes": [ { "code": "not_pushed", "state": "drift", "text_zh": "未推送", "fix": "bin/agent-env plugin publish --all" } ] } ],
  "plugins": [ { "name": "demo-public", "visibility": "public", "visibility_zh": "公开", "description": "...", "repo": "https://...", "repo_key": "github.com/...",
                 "build": { "state": "synced | drift | missing | unknown", "detail": "...", "fix": "" },
                 "publish": { "state": "pushed | stale | not_pushed | local", "detail": "...", "fix": "...", "pushed_at": null, "steps": { "build": "..." } } } ],
  "environments": [ { "env": "grok-bot", "name": "Grok Bot", "transport": "local | remote-exec | cloud-hosted", "mode": "plugins | legacy | others",
                      "phase": null, "now_zh": null, "deny": ["company"], "targets": [ { "id": "grok-bot/bot", "name": "Grok Bot", "plugins": ["demo-public"] } ],
                      "gets": { "public": { "state": "gets | none | denied | legacy | own", "plugins": ["demo-public"] }, "personal": {}, "company": {}, "local": {} },
                      "credential": { "state": "own | none | token | expiring | expired | missing | unknown | legacy", "name": null, "expires": null, "detail": "..." },
                      "notes": [ { "code": "pointer_missing | pull_missing | pull_drift", "state": "missing", "text_zh": "指路规则未投递", "fix": "..." } ] } ]
}
```

- 只看插件体系的仓库：`[sources]` 所在的仓、本仓（生成方）、插件仓（`[plugins.<名字>].repo`；某个可见性还没有插件写 repo 时用 `[plugin_repos].<可见性>`）。`repos.json` 里其他工作仓不查。
- `repos[].visibility` 为 `unknown` 时 `detail` 写原因（没查过、没装 gh 或 [git_hosts] 声明的命令、没登录、超时），`fix` 写怎么办；刷新时某个仓这次没查到，保留上次的结果。
- `environments` 按清单 `[environments]` 的顺序，最后多一行 `env: null`、`mode: "others"`：别人的机器，只能拿到公开插件。`mode: "legacy"` 是还没改成插件的环境，`phase` 是 `docs/plugins-design.md`「迁移阶段」里把它改过来的阶段，四列都是 `legacy`。
- 远程云主机（`remote-exec`）改成插件后自己定时拉取：`credential.state` 是 `own`（用那台机器已有的 git 凭证）；它的 `pulled` 格不是已同步时多一条 `pull_missing` 或 `pull_drift` 的 note。
- `notes[].state` 取 `fail | missing | drift | planned`；`fail`（如公开仓里放了非公开插件、挂了禁止的可见性）和 `missing`、`drift` 面板标色，`planned` 不标色。
- `view` 自己出错时整个字段是 `{ "refresh_command": "...", "error": { "message": "...", "fix": "..." } }`，`status` 照常返回，面板在标签里显示这条错误。
