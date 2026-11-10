# 新环境档案模板

接一台新机器（远程 Linux、新的 WSL、另一台 Mac）时，复制本目录为 `profiles/<环境>/`，按下面的步骤填写。环境名只用字母、数字、点、下划线和连字符，它同时是状态分支名 `status/<环境>`。

| 文件 | 写什么 |
| --- | --- |
| `AGENTS.<环境>.md`（模板里叫 `AGENTS.ENV.md`） | 这台机器和共享 `AGENTS.md` 不同的地方：剪贴板、浏览器、安装、密钥位置、哪些配置不能手改 |
| `skills-deny.txt` | 这台机器上用不了的共享 skill，一行一个，写原因 |
| `mcp.md` | 每个宿主开哪些 MCP server、为什么关；结果写进清单的 `mcp = [...]` |
| `README.md` | 本文件；复制后可以删掉，或改成这台机器的接入记录 |

档案只放个人层内容（机器名、账号、路径）。引擎代码（`engine/`、`bin/`）里不写这些。

## 远程 Linux 机接入步骤

以一台叫 `devbox` 的 Ubuntu 机器为例。每一步看结果再走下一步；失败时按 `skills/agent-env/SKILL.md`「失败时停下还是修」处理。

1. **装前置**：Python 3.11+（引擎用 `tomllib`）、git、zsh（行为抽测要用）。`python3 --version` 低于 3.11 时，用 `uv run --python 3.12` 或系统包装新版本，再设 `AGENT_ENV_PYTHON=<路径>`。
2. **克隆源仓库**：
   ```bash
   git clone <个人环境仓库地址> ~/coding/agent-env
   git clone <规则源仓库地址> ~/.claude         # 规则源（AGENTS.md、docs、scripts）
   git clone <共享 skill 仓库地址> ~/.agents     # 共享 skill
   ```
   远程机只读这些源：不在 `~/.claude`、`~/.agents` 里提交。这台机器专属的改动只写环境仓库。
3. **写档案**：`cp -R profiles/_template profiles/devbox`，把 `AGENTS.ENV.md` 改名为 `AGENTS.devbox.md`，按注释填写三个文件。
4. **登记进清单** `manifest/habits.toml`：
   ```toml
   [environments.devbox]
   platform = "linux"
   wsl = false            # WSL 写 true；doctor 靠它区分两台 Linux

   [[targets]]
   id = "devbox/claude-code"
   host = "claude-code"
   env = "devbox"
   roles = ["remote"]     # 按用途选；remote 收的章节见清单 [rule_sections]
   rule_limit_bytes = 0
   verified = false       # 在这台机器实测规则被加载后改成 true
   eval_cases = ["no-commit-after-fix", "external-send-asks", "flow-format-terminal"]

   [[targets.expect]]
   kind = "contains"
   habit = "rules"
   name = "AGENTS.md"
   path = "~/.claude/CLAUDE.md"
   text = "@AGENTS.md"
   ```
   每个宿主一个 `[[targets]]`。新宿主先按 `skills/agent-env/SKILL.md`「接入新宿主清单」逐项查实再填。
5. **校验和体检**：
   ```bash
   bin/agent-env check     # 有 FAIL 就按「修复：」改清单，全过再继续
   bin/agent-env doctor    # 退出码 1 表示有漂移或缺失，逐条看「修复：」
   ```
   `doctor` 按 `platform` 和 `wsl` 自动认出这台机器是 `devbox`；认不出时加 `--env devbox`。
6. **行为抽测（可选）**：`bin/agent-env eval --repeat 1`。没装宿主 CLI 的落点会记 `err`，在汇报里写 `UNVERIFIED`。
7. **上报**：`bin/agent-env status --commit` 写本地分支 `status/devbox`；用户同意后跑 `bin/agent-env status --commit --push`。Mac 上 `bin/agent-env status --json --all --fetch` 就能在「环境」面板看到这台机器。

## skill

`skills-deny.txt` 只记名字和原因，各宿主怎么落地：

- Claude Code：不把这些 skill 链进 `~/.claude/skills`。
- Codex：`config.toml` 写 `[[skills.config]] path = "<SKILL.md>"`、`enabled = false`。
- Grok：`config.toml` 的 `[skills] disabled = [...]`。
- Hermes：`config.yaml` 的 `skills.disabled`，清单里用 `skills_disabled` + `skills_config` 让 doctor 核对。

## 云端托管落点

厂商云端的落点（如 Grok Bot）也要一份机器档案 `profiles/<环境>/README.md`，写清它在哪里跑、怎么拿到插件、怎么授权、哪些还没核实；范例见 `profiles/grok-bot/README.md`。清单里它是单独一个环境：

```toml
[environments.<环境>]
transport = "cloud-hosted"     # 落点自己去取插件；不按本机推断，命令加 --env
driven_from = "mac"            # 插件生成结果和投递记录在哪台机器
deny_visibility = ["company"]  # 不许进这里的可见性

[[targets]]
id = "<环境>/bot"
env = "<环境>"
roles = ["remote"]
plugins = ["<公开插件>", "<个人插件>"]   # 不能有本机插件
```

再加三条期望：`delivered`（指路规则）、`plugins`（插件生成结果）、`token`（读私有插件的 token 到期日）。流程按 `skills/agent-env/SKILL.md` 第 4 节走。
