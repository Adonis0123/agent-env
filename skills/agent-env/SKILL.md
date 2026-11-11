---
name: agent-env
description: Runbook for the agent habit engine (agent-env) on Mac, WSL or a remote machine. Runs sync, `bin/agent-env doctor`, optional behaviour eval and `bin/agent-env status --commit`, and decides when to stop or fix; onboards a new agent host or machine (rules and load order, skill dir, hooks, MCP, size limit, roles; profiles/_template); builds and publishes plugins that cloud-hosted targets such as Grok Bot pull themselves; proposes promotions (memory, permission allows, host-made skills) back to the sources. Use when the user asks to check the agent environment, run doctor or the behaviour eval, fix habit drift, commit status, roll back a generated rule file, add a host or remote machine, publish rules or sync a cloud bot, or promote what hosts learned, including 体检一下 agent 环境、跑 agent-env、跑行为抽测、修漂移、提交环境状态、回滚规则文件、接入新宿主、接一台远程机、发布规则、同步 Grok Bot、晋升、把 memory 合回规则. Not for upgrading the CLIs or account directories alone (sideby).
---

# agent-env 运行手册

本手册在任何环境都一样用。仓库根记作 `$REPO`：放清单 `manifest/habits.toml` 和 `bin/agent-env` 的个人环境仓库（引擎可以是它的一个子目录，如 `vendor/agent-env/`）。全程中文进度说明；命令、文件名用英文。

本机细节：`$REPO/docs/runbook-local.md` 存在时先读它（同步命令、各环境的运维 skill、只在这几台机器成立的做法）。

## 0. 用户怎么说 → agent 做什么

用户不写代码、不敲命令，只说一句话；命令都由你跑，结果用大白话讲。下表是日常四类请求，细节在对应小节。

| 用户说 | 你做什么 | 要用户本人做的 |
| --- | --- | --- |
| 检查一下我的环境 / 体检一下 | 第 1 节：有 `sync_command` 先同步 → `$REPO/bin/agent-env check` → `$REPO/bin/agent-env doctor` → 汇报五态计数，逐条说明漂移和缺失是什么意思。只读，不改任何文件 | 无 |
| 把有问题的修好 | 第 2 节那张表逐条对：修法是 `render ... --write` 的直接修（引擎先快照）；会覆盖已有内容的链接、会改宿主配置的 MCP 命令，先给用户看再做。修完重跑 `doctor`，修坏了 `rollback` | 同意覆盖或改配置；登录过期时自己登录 |
| 发布规则 / 同步到 bot | 第 4 节：`$REPO/bin/agent-env plugin publish --all` 先演练，把仓库、可见性、待推送内容给用户看；用户确认后加 `--yes`。云端 bot 再给出指路规则 | 确认推送；登录 GitHub；在 bot 的 app 里粘贴指路规则并说「存好了」 |
| 加一台机器 / 加一个 agent | 第 3 节：在 `$REPO` 里建 `profiles/<环境>/`，从引擎的 `profiles/_template/`（引擎在 `vendor/agent-env/` 时是 `$REPO/vendor/agent-env/profiles/_template/`）复制四个档案文件，不带 `starter/`，命令见该目录 `README.md` 第 3 步；查实新宿主那张表 → 在清单加环境和 `[[targets]]` → `check` → `doctor` → `status --commit` | 在新机器上登录；确认角色（`cli` / `bot` / `cron` / `remote`） |

第一次安装（还没有清单）：按引擎 `README.md` 的「Use it for your setup」，从 `profiles/_template/starter/` 复制出个人层，再回到上表。

面板给用户看的词和引擎内部叫法不同，看到页面用词时按下表对应：

| 面板用词 | 引擎术语 |
| --- | --- |
| 规则测试 | eval（`bin/agent-env eval`） |
| 不一致 | drift |
| 本机自动生成 | composed |

## 1. 例行流程：sync → doctor → (eval) → status --commit

按顺序做，每步看结果再决定下一步。

1. **同步源**。环境在清单里写了 `sync_command` 的，先跑它；源环境（规则源就在本机）跳过这一步。远程机按它的档案里写的同步命令跑。
2. **校验清单**：`$REPO/bin/agent-env check`。有 FAIL 就停，按每行的「修复：」改清单（`manifest/`），改完重跑，全过再继续。
3. **体检**：`$REPO/bin/agent-env doctor`。退出码 0 表示没有漂移和缺失；1 表示有，逐条看「修复：」。
4. **行为抽测（可选）**：只有用户要求，或这次改了规则正文时才跑。会调用模型，开跑前说预计时长（每个 CLI 落点 3 个用例 `--repeat 1` 约 2–5 分钟，最慢的宿主决定总时长）。
   - `$REPO/bin/agent-env eval --repeat 1`：按清单各落点的 `eval_cases` 跑；只跑一个落点加 `--target <id>`。CLI 宿主转调清单 `[sources] eval` 的运行器（`agents-md-eval.zsh`），对话 bot 走引擎内置的单轮适配（Hermes 用 `hermes -z`），判定都由脚本做。
   - 结果写进 `${XDG_STATE_HOME:-~/.local/state}/agent-env/eval/<环境>.json`，`status` 会带上，面板「行为抽测」一节显示。每条结果的 `detail` 指向保存的回复。
   - 退出码：0 全过；1 有失败；3 只有 `err`（宿主超时或崩溃，结论 `UNVERIFIED`）。`err` 不重跑第二次，在汇报里列出；失败按清单 `[sources] docs` 目录下 `agents-md-eval.md` 的「读结果」判断是规则问题还是波动（累计 ≥6 次才下结论）。
   - 不能抽测的落点在清单写 `eval_skip = "原因"`，结果记 `skip`。
5. **提交状态**：`$REPO/bin/agent-env status --commit`。它只写本地分支 `status/<环境>`，不动工作树和当前分支。要推送时，先问用户，用户同意后才跑 `$REPO/bin/agent-env status --commit --push`。
6. **汇报**：一张表列出五态计数（已同步 / 已定制 / 漂移 / 缺失 / 未知），再逐条列漂移和缺失，以及做了哪些修复。

在源环境（规则源所在、`driven_from` 指向的那台机器）上看全部环境：`$REPO/bin/agent-env status --json --all`（加 `--fetch` 先拉远端状态分支）。读不到的环境会出现在 `errors` 里。

## 2. 失败时停下还是修

| 情况 | 做法 |
| --- | --- |
| `check` 有 FAIL | 停。改清单让它通过；不确定怎么分类（新章节、新 doc 的角色和可见性）就问用户 |
| 漂移的 `fix` 是 `bin/agent-env render ... --write` | 可以直接修：写入前引擎会快照旧文件。写完重跑 doctor |
| 生成文件被宿主或人手改过（diff 里有新内容） | 先把专属改动搬进档案（`profiles/<环境>/...`），再 `render --write`；不要直接覆盖 |
| 链接漂移（`ln -s` / `trash` 修法） | 先确认被替换的文件里没有要保留的改动，再执行；删除一律用 `trash` |
| MCP 漂移（`npx -y add-mcp@...`） | 每条命令先给用户看，同意后再跑；它会改宿主配置 |
| 账号目录（sideby）漂移 | 跑 `sideby doctor --fix`；它不替换真文件，剩下的按它的提示问用户 |
| 云端托管落点缺失或漂移 | 按第 4 节走：插件过期就 `plugin build`；指路规则由你生成并显示出来（能复制到剪贴板就复制），停下等用户在 app 里粘贴，用户说存好后才 `--record`，再跑 `doctor`。插件还没写 `repo` 时先停下说明缺哪个仓库 |
| 宿主版本变了（`changed=true`） | 你来核对，不直接改登记的版本。按第 3 节那张表逐项查：联网看该宿主两个版本之间的发布说明和官方文档（只认一手来源），再在那台机器上跑 `doctor` 并用宿主的 inspect / debug 输出或日志实测。没变：把结论给用户看，同意后把清单 `host_version.recorded` 改成当前版本，重跑 `doctor`。有变：停下，报告哪一项变了、出处和清单或该环境的适配规则要改哪里 |
| `unknown` | 不算失败。汇报里写明缺什么证据 |
| 修坏了 | `$REPO/bin/agent-env rollback --target <落点>` 恢复最近一次快照；再跑一次可撤销 |

不 commit 业务分支、不 push，除非用户明确要求。状态分支的本地提交属于例行流程。

## 3. 接入新宿主清单

接一个新宿主（如 Gemini、OpenCode、新的对话 bot）时，逐项查实再填。接一台新机器（远程 Linux、新的 WSL）时，先复制引擎里的档案模板 `profiles/_template/`（引擎在 `vendor/agent-env/` 时是 `$REPO/vendor/agent-env/profiles/_template/`；只复制四个档案文件，不带 `starter/`），按它的 `README.md` 走：克隆 → 写档案 → 登记清单 → `doctor` → `status --commit --push`。每项写来源：官方文档链接、本机实测命令或源码位置。查不到的写 `UNVERIFIED`，不要猜。

| 项 | 要填什么 | 怎么查 |
| --- | --- | --- |
| 规则文件和加载顺序 | 读哪些文件、谁先谁后、是否按工作目录取 | 官方文档；本机用该宿主的 inspect / debug 输出或日志核实 |
| skill 目录 | 全局 skill 放哪，是否自动读 `~/.agents/skills` | 文档 + 放一个测试 skill 看是否被列出 |
| hook 格式 | 接线文件位置、事件名、输入输出格式 | 文档 + 接一个只打日志的 hook 实测 |
| MCP 位置 | 配置文件路径和键名（如 `mcpServers`） | 文档；`engine/mcp.py` 没有读取器时要先加 |
| compat 导入 | 会不会自动读别家的规则、MCP、skill | 文档 + 实测，避免重复注册 |
| 体积上限 | 规则文件上限和单位（字节或字符） | 文档或源码常量；查不到写 0 |
| 账号目录约定 | 多账号时各目录哪些共享、哪些必须是真文件 | sideby 的规则；`sideby doctor` |
| 角色 | `cli` / `bot` / `cron` / `remote` | 按用途和用户确认 |

填完后：

1. 在清单 `manifest/habits.toml` 加 `[[targets]]`：`id`、`host`、`env`、`roles`、`rule_limit_bytes`、`rule_limit_unit`、`verified`，有 MCP 就加 `mcp_config` 和 `mcp`。
2. 加 `host_version = { cmd = [...], recorded = "..." }`，记下核对时的版本。
3. 加 `[[targets.expect]]`：链接用 `link`，文件里含某行用 `contains`，引擎生成的用 `generated`，云端托管用 `delivered`（指路规则）、`plugins` 和 `token`。
4. 需要专属内容就写档案 `profiles/<环境>/<宿主>.md`，设 `render = "generated"`、`supplement`、`output`。
5. 加 `eval_cases = [...]`（3–5 个，挑和角色相关的），或 `eval_skip = "原因"`。
6. 跑 `bin/agent-env check` 和 `bin/agent-env doctor`，全部能解释后再提交状态。

## 4. 云端托管落点：插件和指路规则（Grok Bot 一类）

本机写不进去的落点（清单 `[environments.<名字>] transport = "cloud-hosted"`，如 `grok-bot`）自己去取插件：每个插件是一个带可见性（公开 / 个人 / 公司 / 本机）的仓库，落点按 `plugins = [...]` 的顺序拉，后面的覆盖前面的。设计见 `docs/plugins-design.md`，机器事实见 `profiles/<环境>/README.md`。

用户只说自然语言（「发布规则」「同步 Grok Bot」「看看各环境状态」），命令由你跑：

| 用户说 | 你做 |
| --- | --- |
| 发布规则 / 同步 Grok Bot | `$REPO/bin/agent-env plugin publish --all`：生成 → 隐私扫描 → 提交 → 推送，每步记在 `${XDG_STATE_HOME:-~/.local/state}/agent-env/plugins/publish-ledger.json`，中断后重跑从断点续。不加 `--yes` 只演练到推送前：列出仓库、可见性、待推送的提交和文件，退出码 1；把这份清单给用户看，用户确认后再加 `--yes` 推送。个人、公司插件绝不推到公开仓，查不到可见性也不推；从不强推 |
| 只生成看看 | `$REPO/bin/agent-env plugin build --all`（或 `--plugin <名字>`），写到 `plugins/<名字>/`；同样输入第二次报「已是最新」 |
| 看 Grok Bot 状态 | `$REPO/bin/agent-env doctor --env grok-bot` |

1. `plugin build / publish / pointer` 先跑一遍 `check`，有 FAIL 就不生成也不输出。生成先写插件目录旁边的临时目录，全部成功才整体替换；任何一步失败，旧目录（含 `.git`）原样保留。同一个插件同时只能有一个进程在生成，第二个会直接报错，等前一个结束再跑。
   写入前的隐私检查：公开和个人插件查家目录路径（含 Windows 和 `~用户名/` 写法）、邮箱、密钥；公司插件查密钥。文件按字节扫描；公开和个人插件里不是 UTF-8 文本的文件只放行图片（png / jpg / jpeg / gif / webp / ico）。自写 skill 里的凭证文件（`.env*`、`auth.json`、`*.pem`、`id_rsa*`、`.npmrc`、`.netrc`、`credentials*` 等）一律不复制，生成时会列出来。命中就停并给修法。
   公开插件还查「指向插件外的文件」：行内代码和链接里的 `docs/…`、`scripts/…`、`~/…` 这类路径，插件里没有就拒绝生成，报错给出文件、行号和路径；个人和公司插件只提醒。修法：在清单 `[plugins.<名字>]` 的 `omit` 里写上带这条路径的那半句原文（精确子串，连同前后的连接标点；每项去掉一处，源里找不到会拒绝生成），不改规则源。规则见 `docs/plugins-design.md` 的「一条规则归谁」。
2. 指路规则：`$REPO/bin/agent-env plugin pointer --target grok-bot/bot` 打印不超过 400 字符（默认上限，可用 `pointer_limit_chars` 改）的规则（写到 outbox）。插件把第三方 skill 复制进 `skills/`（清单 `vendor`）时，规则会让 Bot 整个目录复制（含 `UPSTREAM.md` 出处和 `UPSTREAM-LICENSE` 许可）；还有只记在锁文件里的第三方 skill 时，规则会让 Bot 在有 `skills-lock.json` 的仓里跑 `npx -y skills experimental_install` 再读 `.agents/skills/`，失败就用 git 按 `sourceUrl` + `skillPath` 取，并说明用了哪种。仓库还没建时里面是占位符，会提示，`--record` 会拒绝。插件落点没有整份规则：`render --target` 和 `deliver --target` 对它都会报错并指到这条命令。把文本给用户，请用户在 Grok Bot app 里粘贴，让它存进 memory；Bot 改云端设置都由用户在 app 里确认，你不替用户点。
3. 用户确认存好后跑 `$REPO/bin/agent-env plugin pointer --target grok-bot/bot --record`，记录内容哈希。以后插件列表或仓库地址变了，doctor 报漂移，提示重新粘贴。
4. Grok Bot 的 Custom Rules 是审批规则（哪些操作要用户批准），不放规则，也不往里写。
5. Grok Bot 现在只挂公开插件，不需要 token，doctor 的 token 格显示「只用公开插件，不需要 token」。云端托管落点挂私有插件时才需要：token 由用户建（fine-grained、只读那个插件仓、90 天）并经 secret card 交给 Bot；清单只写 `token = { name = "...", expires = <日期> }`：`expires` 只写日期（如 `2027-02-08`，带时间会报错），当天就算到期；token 的值不进清单，报错里也不会显示任何值。doctor 7 天内到期显示「提醒：」，过期报漂移。
6. doctor 的格：每个插件一格（生成目录比源过期就漂移，修法 `plugin build`）、指路规则一格、token 一格；落点有补丁层（排在最后的本机插件）时，它覆盖的章节或 skill 各一格「已覆盖」，写明原来来自哪个插件。

用户没有确认存入，就不要跑 `--record`。云端托管落点不能用本机插件；Grok Bot 不能用公司插件；公开插件只能带登记为 public 的章节，不能写 `rules_file`；个人和公司插件的 `rules_file` 只放自己的章节，不能和已登记的共享章节重名；公司 skill 不进公开和个人插件。这些 `bin/agent-env check` 都会拦下，生成时也会再查一遍。

## 5. 晋升：把落点上长出来的东西合回源

习惯只从源流向落点。各宿主运行时新长出来的东西（memory、权限 allow、宿主自建 skill）由 AI 提议、用户确认后，改源仓库再走正常同步，不直接改落点。

**什么时候跑**：用户要求时；例行流程里距上次提议超过约两周时提一句（`ls ${XDG_STATE_HOME:-~/.local/state}/agent-env/promotions/` 看上次时间）；升级宿主后。

1. **扫描**：`$REPO/bin/agent-env promote scan`，只读，输出确定的候选清单 JSON（同样输入两次输出相同）。来源在清单 `[promotion]` 段：各宿主 memory、`settings.json` 的 `permissions.allow` 相对 git HEAD 的新增项、Codex `default.rules`、宿主自建 skill（不在 `~/.agents/skills`、也不在宿主自带或 hub 安装清单里的）。疑似密钥的条目只记来源，原文换成「已跳过：疑似密钥」。
2. **写提议**：`$REPO/bin/agent-env promote propose` 写出草稿 `promotions/<时间>.md`，文末是全部候选。AI 读完候选，填「提议」和「不提议的类别」两节：
   - 每条写去向（`AGENTS.md` 哪一节 / 哪个 `docs/` / 共享 `settings.json` / 共享 skill 仓库）、原文摘录和候选 id、理由、建议写法。
   - 只提跨宿主成立、现有规则没覆盖的；已覆盖的、单个项目的、个人隐私的、宿主专属的归入「不提议」并写原因。
   - 发现落点上的东西和规则冲突（如某宿主放开了规则禁止的操作），单独列「反向发现」。
   - 提议文件只在本机 state 目录，不提交、不外发。
3. **请用户确认**：给出文件路径和条数，问逐条合不合；用户点名的才合，没点名的不动。
4. **合入**：只改清单 `[sources]` 指向的源（规则改 `rules_src` 目录里对应插件的文件：`public.md` 公开、`personal.md` 个人、`local.md` 本机；`docs` 目录、源仓库的共享 `settings.json`，或 `skills` 目录），按该仓库的规则走。`rules` 的 `AGENTS.md` 是生成文件，不手改：改完规则源，对本机每个合成落点运行 `$REPO/bin/agent-env render --target <落点> --write` 重新合成（有 `sync_command` 的环境不改，等源推送后跑它）。改规则先跑 `zsh <[sources] eval 指向的脚本> --static`，措辞改动按 `[sources] docs` 目录下 `agents-md-eval.md` 跑相关用例。不 commit、不 push，除非用户要求。
5. **同步**：合入后照例 sync → `bin/agent-env doctor`；生成类落点（Hermes、WorkBuddy）跑 `render --write`，云端落点重新投递。落点 memory 里已合回源的条目，提醒用户可以在该宿主里删掉，AI 不替宿主删。

## 6. 远程云主机，本机投递（remote-exec 环境）

只能从本机经命令前缀操作的主机（如云主机上的团队 bot）。清单写 `[environments.<名字>] transport = "remote-exec"`，`exec` 前缀从 stdin 读 sh 脚本在远程执行，`send` 前缀传文件；不写 `platform`，命令都加 `--env <名字>`。

- 体检：`$REPO/bin/agent-env doctor --env <名字>`。全部检查合成一次远程执行：`generated` 比远程文件 sha256 和本机生成结果，`contains`、`link` 在远程核对，`service` 看 systemd 服务是否 active，`login` 在本机跑轻量命令判断登录是否有效（失败记未知，修法是用户本人要跑的登录命令）。登录失效、超时、不可达时整个环境标错误，第一行给修法。
- 上报：`$REPO/bin/agent-env status --env <名字> --commit`，状态分支写在本机。
- 投递：`$REPO/bin/agent-env deliver --target <落点> --dry-run` 打印远程步骤（快照旧文件 → 分片发送 → 校验 sha256 → 原子替换 → 记录），给用户看；用户同意后加 `--yes`。`render --check / --write` 对远程落点会拒绝，避免写到本机同名路径。
- 具体主机的运维（登录过期、日志、重启、升级）见个人环境仓库里各自的运维 skill（`docs/runbook-local.md` 会列出）。
