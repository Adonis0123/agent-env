# <环境> 的 MCP 开关

<!-- 复制后按实际填写，删掉这段注释。清单 manifest/habits.toml 的 [mcp.<名字>] 是全部 server 的定义；
     本文件只记这台机器上每个宿主开哪些、关哪些、为什么。改完把结果写进清单对应落点的 mcp = [...]，
     再跑 bin/agent-env doctor 核对。清单和本文件都只写变量名和 URL，不写任何密钥值。 -->

| server | 开 / 关 | 哪些宿主 | 原因 |
| --- | --- | --- | --- |
| `deepwiki` | 开 | claude-code、codex、grok | 公共 HTTP 服务，无需登录 |
| `figma` | <开 / 关> | <...> | OAuth，每个宿主自己登录；无界面机器上登录不了就关 |
| `kimi-cu` | 关 | 全部 | 需要图形桌面；远程 Linux 没有 |
| `<名字>` | <...> | <...> | <需要的环境变量名，如 FIGMA_API_KEY，值放 secrets.env> |

- compat 导入：<哪个宿主会自动读别家的 MCP 配置（如 Grok 读 ~/.claude.json），在清单写 mcp_compat_from，避免重复注册>
- 宿主自己写入、不接管的条目：<写进清单 mcp_unmanaged>
