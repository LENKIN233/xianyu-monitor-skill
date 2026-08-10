# Xianyu Monitor — 闲鱼搜索与监控 Agent Skill

[![CI](https://github.com/LENKIN233/xianyu-monitor-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/LENKIN233/xianyu-monitor-skill/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Agent Skills](https://img.shields.io/badge/Agent_Skills-compatible-5B5BD6.svg)](https://agentskills.io/specification)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

用本地私有浏览器状态搜索和持续监控闲鱼 / Goofish 商品，支持关键词、价格、地区、
真实分页、持久任务和新观察商品去重。核心不依赖特定 Agent，可用于 Codex、
Claude Code、OpenClaw 或普通 CLI/调度器。

Search and monitor Xianyu/Goofish listings with private local browser state.
Works across Codex, Claude Code, OpenClaw, and CLI schedulers.

## 一眼看懂

| 能做 | 不会做 |
|---|---|
| 搜索商品，执行价格和地区过滤 | 绕过 CAPTCHA、登录挑战或平台风控 |
| 真实翻页并按商品 ID 去重 | 自动联系卖家、购买、下单或付款 |
| 保存任务，只返回新观察到的商品 | 凭空判断卖家信用、真假或维修历史 |
| 输出稳定 JSON 供任意宿主投递 | 内置 AI 商品分析或通知渠道 |

失败、登录跳转和风险控制会明确返回错误，不会伪装成“没有商品”。Cookie、代理凭据和
登录状态始终视为私密凭据；本项目不会把它们作为通知内容或提交到仓库。

## 三步快速开始

需要 Python 3.10+。强烈建议把状态文件放在仓库之外的用户私有绝对路径；若确需放入
checkout，只能使用已被整个目录忽略的私有目录。

### 1. 安装

```bash
git clone https://github.com/LENKIN233/xianyu-monitor-skill.git xianyu-monitor
cd xianyu-monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/xianyu.py doctor
```

`xianyu.py doctor` 只读检查 Python、依赖和可用浏览器，不启动浏览器、不写文件，也不回显
本地路径。若返回 `install-browser`，再执行
`.venv/bin/python -m playwright install chromium` 并重跑；已有本机 Chrome 时不会要求
下载重复浏览器。`ok: false` 时按 `next_action` 修复；如果只发现本机 Chrome，后续命令
按提示增加 `--browser-channel chrome`。

### 2. 在专用浏览器中登录

```bash
.venv/bin/python scripts/xianyu.py login \
  --confirm-in-browser \
  --output /absolute/private/path/xianyu-state.json
```

只在命令新开的专用浏览器窗口中登录。二维码消失不代表登录完成；还需要完成手机确认，
回到正常 Goofish 页面，并在本地确认页提交一次性确认码。确认页会依次显示“正在验证
并保存”和“候选状态已安全保存”，成功结果保留 5 秒后才自动关闭专用浏览器；最终 JSON
同时返回 `"exit_reason": "completed"`。这是正常安全清理，不是闪退。本地安装了 Chrome
且希望明确使用它时，增加 `--browser-channel chrome`；这仍会创建独立上下文，不复用
日常浏览器 profile。

### 3. 执行一次受控搜索

```bash
.venv/bin/python scripts/xianyu.py search \
  --keyword "iPhone 15 Pro" \
  --min-price 3500 \
  --max-price 5500 \
  --pages 1 \
  --retries 1 \
  --state /absolute/private/path/xianyu-state.json
```

成功输出的关键字段如下；`count` 仅是一次脱敏实测示例，实际结果会变化：

```json
{
  "ok": true,
  "count": 28,
  "pages_scraped": 1,
  "search_capability": {"status": "passed-for-this-run"},
  "cleanup": {"status": "complete-or-not-required"}
}
```

这只证明该浏览器状态在当次运行能够搜索，不证明账号身份或长期认证状态。完整搜索、
监控和失败语义见下文。

## 兼容与安装方式

| 运行方式 | 发现目录或入口 | 调用方式 |
|---|---|---|
| 纯 CLI / 调度器 | 当前 checkout | `.venv/bin/python scripts/xianyu.py COMMAND` |
| Codex | `~/.agents/skills/xianyu-monitor` | `$xianyu-monitor` |
| Claude Code | `~/.claude/skills/xianyu-monitor` | `/xianyu-monitor` |
| OpenClaw | `~/.agents/skills/xianyu-monitor` | `/skill xianyu-monitor` |

先预览多宿主安装，不会写入：

```bash
.venv/bin/python scripts/xianyu.py install --host all --mode symlink --dry-run
```

确认后去掉 `--dry-run`。不支持目录软链接时使用 `--mode copy`；复制模式只带运行时、
Skill references/宿主元数据和许可证，不会带仓库 README、`.git`、虚拟环境、测试缓存或
本地任务/登录文件，也不会覆盖已有路径。统一入口不会包装或改写各子命令的 JSON、TTY
输入和退出码；原来的 `scripts/*.py` 入口继续兼容。Cookie 导入和旧 CDP profile 清理
仍是 API reference 中的高级直调工具。各宿主的项目级
安装、定时任务和注意事项见
[references/host_adapters.md](references/host_adapters.md)。

Windows PowerShell 使用 `.\.venv\Scripts\python.exe` 代替
`.venv/bin/python`。完整初始化示例：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\xianyu.py doctor
```

Windows 上也只在 `next_action.code` 为 `install-browser` 时运行
`.\.venv\Scripts\python.exe -m playwright install chromium`。

## 浏览器状态

推荐使用内置登录命令。它会打开独立的可见浏览器，扫码、验证码和 CAPTCHA 必须由
用户本人完成。扫码后通常还要在手机上点击确认登录；二维码消失只表示扫码流程发生了
变化，不等于登录完成。等浏览器回到正常 Goofish 页面后，再提交最终确认：

```bash
.venv/bin/python scripts/xianyu.py login \
  --browser-channel chrome \
  --output "/absolute/private/path/xianyu-state.json"
```

`--browser-channel chrome` 只选择 Chrome 可执行文件，不会复用已经打开的普通 Chrome
会话或 profile；必须在命令新开的窗口里重新登录。默认登录等待窗口为 1800 秒，
可用 `--timeout` 显式调整。

本项目不再连接外部 Chrome 的 TCP 调试端口。Chrome 的本机 TCP CDP 没有客户端认证；
同一网络命名空间中的其他本地进程可能发现端口并接管登录上下文。旧版
`--cdp-user-data-dir` 参数仅为兼容升级而隐藏保留，传入时会在 stdout 返回结构化
`ArgumentError` 并以状态码 `2` 失败，不会建立连接。

若 Agent 沙箱不能启动浏览器，不要把 Chrome 通过 TCP 暴露给沙箱。改在浏览器所属的
可信宿主上，用 `--browser-channel chrome` 完整执行 `xianyu.py login`、`search` 和
`monitor`，并只在该宿主的任务/调度配置中引用权限为 `0600` 的状态文件。Agent 可
消费命令输出的商品 JSON，但不得接收浏览器 profile 或状态内容。

从支持 CDP 的旧版本升级时，先关闭旧专用 Chrome；若仍保留由旧版初始化的临时
profile，只把 `cdp_profile.py` 用作受保护的迁移清理工具：

```bash
.venv/bin/python scripts/cdp_profile.py \
  --directory "/absolute/private/tmp/xianyu-cdp.EXACT" \
  --cleanup
```

该命令不再初始化 profile，且 `--cleanup` 必填。它只删除带旧版哨兵的精确临时目录，
并会拒绝活动中的 Chrome、仍监听的旧调试端口、符号链接风险或无法安全递归删除的
平台。严格按“关闭 Chrome → 清理”串行执行；失败时只在系统文件管理器中处理这一个
精确目录，禁止用宽泛递归删除命令替代。

默认模式会在交互终端显示一次性 `SAVE-...` 确认词。Agent 必须暂停并等待用户本人
输入，不得代输、管道注入、猜测或复用；此模式下非交互输入、EOF 或错误确认词都会
失败。`--confirm-in-browser` 把同样的明确确认移到命令打开的本地确认页，是唯一允许命令使用
非交互输入的模式。

保存 candidate 的必要条件只有：收到用户最终确认、原页面是正常的 HTTPS Goofish 页面
而非登录/验证/风控页，以及过滤后的 Goofish 站点状态非空。只有最终确认提交后，命令
才会新开验证页，用最多 15 秒 best-effort 观察 PC 导航响应中的非空 `displayName`。
这个可选信号缺失或探测页发生普通错误时不会丢弃 candidate，而是记录为
`not-observed`；取消与清理失败仍会终止。该信号既不是认证证明，也不是身份凭据，
命令不会输出其值。保存前会删除所有非 Goofish 域的 Cookie 和 origin；剩余的站点状态仍是
账号凭证，也可能编码账号数据，禁止查看、摘要或分享。
POSIX 下文件权限为 `0600`；除非明确增加 `--force`，否则不会覆盖已有文件。输出中
的证据维度相互独立：

- `state.status: candidate-saved`：候选浏览器状态已安全保存；
- `state.status: not-saved`：已确定本次没有发布状态文件；
- `state.status: not-established`：中断或系统错误使原子发布结果无法确认；此路径必须
  继续按异常凭据保密，不得读取、使用，也不得声称文件已保存或不存在；
- `confirmation.status: interactive-token-received`：终端或本地确认页收到了确认词，
  但程序本身无法判断输入者是用户还是 Agent；
- `confirmation.actor: not-machine-verified`：程序不能验证是谁输入了确认词；
- `confirmation.channel`：`terminal` 或 `browser`；两者都不能证明实际确认者身份；
- `session.nav_display_name: present`：当前 Goofish PC 导航响应含非空展示名，
  但它不能证明具体账号身份；
- `session.nav_display_name: not-observed`：15 秒 best-effort 观察未获得该可选信号；
  candidate 仍可保存；
- `authentication.status: not-established`：程序没有证明该候选状态已认证；
- `identity.status: not-machine-verified`：仅在观察到可选展示名时表示程序仍不能机器验证
  具体账号；信号缺失时为 `not-established`；
- `search_capability.status: not-tested`：尚未搜索。
- `cleanup.status`：`failed` 会列出通用清理错误，提示专用浏览器可能未完全退出；
  否则为 `complete-or-not-required`。

只有用户本人在对话中返回准确确认词时，Agent 才能把本次确认归于用户。如果用户否认
确认，或 Agent 曾经代输确认词，必须把输出和由此产生的文件视为异常：不得使用，也
不得推断登录、身份或搜索能力。

保存 candidate 后必须立即用它执行一次真实受控搜索，并要求
`search_capability.status: passed-for-this-run`；`xianyu.py login` 成功本身不完成能力
验证。搜索通过也只能证明该浏览器上下文在当次运行能够搜索，不能证明已认证或登录的
是哪个账号。浏览器清理或本地持久化仍可能失败，因此必须分别读取 `ok` 和能力字段。
`RGV587` 也只能证明请求被拒绝，不能证明账号身份。

项目兼容其他浏览器工具导出的 Playwright storage state，以及原
`ai-goofish-monitor` 扩展的标准和增强快照；导入文件不自带用户确认记录。无论
来源，状态在传入浏览器前都会按结构校验，并只保留 Goofish 域 Cookie 与规范的
Goofish HTTPS origin，第三方凭据不会进入搜索上下文。

如果只有 Cookie Header，从标准输入安全生成：

```bash
.venv/bin/python scripts/create_state.py \
  --cookie-stdin \
  --output /absolute/private/path/xianyu-state.json
```

交互终端会隐藏输入；粘贴一行 Cookie 后按 Enter。若通过管道或重定向传入，则以
EOF 结束。无法关闭终端回显时命令会安全失败。避免使用 `--cookie "..."`，因为参数
可能进入 shell 历史。该命令只生成候选状态，不证明认证或账号身份；若输出
`state.status: not-established`，同样按上面的异常凭据规则处理。
浏览器扩展导出的文件也应设置为仅本人可读：

```bash
chmod 600 /absolute/private/path/xianyu-state.json
```

`chmod 600` 适用于 POSIX 系统；Windows 请把文件放在用户私有目录，并使用 NTFS ACL
限制访问。

## 单次搜索

```bash
.venv/bin/python scripts/xianyu.py search \
  --keyword "iPhone 15 Pro" \
  --min-price 3500 \
  --max-price 5500 \
  --location "上海" \
  --pages 2 \
  --state /absolute/private/path/xianyu-state.json
```

成功输出：

```json
{
  "ok": true,
  "keyword": "iPhone 15 Pro",
  "count": 0,
  "pages_scraped": 2,
  "items": [],
  "search_capability": {"status": "passed-for-this-run"},
  "authentication": {"status": "not-evaluated"},
  "identity": {"status": "not-evaluated"},
  "cleanup": {"status": "complete-or-not-required"}
}
```

一页一次 smoke test 还必须满足退出码为 `0`、`count == len(items)`、
`pages_scraped: 1`、搜索能力通过且清理完成；`authentication` 与 `identity` 仍只是
`not-evaluated`，不能据此声称机器已验证登录或账号身份。

观察到登录跳转或挑战、风控或网络失败会输出 `"ok": false` 并返回非零退出码。闲鱼返回
`RGV587` 等风控错误时立即停止；不要尝试绕过登录挑战、CAPTCHA 或风控。
如果已提供的候选状态进入 `/search`，但无头模式没有观察到搜索接口，只使用
`--headed` 重试一次；不要循环尝试或加入反检测绕过。
拦截请求的临时传输故障使用 `SearchTransportError`，会遵守 `--retries`；已收到但
畸形/不匹配的响应使用 `SearchCaptureError`，不会自动重试。

所有公开 CLI 的命令行解析失败（如缺少参数或类型错误）都会在 stdout 输出一条
`ArgumentError` JSON 并退出 `2`；`SIGTERM` 会进入与用户取消相同的受控清理路径，
输出取消 JSON 并退出 `130`。
价格必须是有限数值，`NaN` 和正负无穷都会被拒绝。

## 持久监控

创建任务：

```bash
.venv/bin/python scripts/xianyu.py task \
  --data-file /absolute/private/path/tasks.json \
  create "MacBook Air M2" \
  --max-price 6000 \
  --location "上海" \
  --pages 2 \
  --state /absolute/private/path/xianyu-state.json
```

任务文件按完整 schema 加载：字段类型、ID 唯一性、列表上限和有限价格都会验证。任一
条目损坏时整次操作失败且不重写原文件，不会静默丢弃无法识别的任务。
浏览器通道会随任务保存；同一任务文件中的任务可以分别使用不同通道。运行时优先级为
监控命令的 `--browser-channel` 覆盖值、任务保存值、`XIANYU_BROWSER_CHANNEL` 环境默认值，
最后才是 Playwright 默认浏览器。只有 `xianyu.py doctor` 返回
`ready-use-browser-channel` 时，才在创建命令末尾增加 `--browser-channel chrome`。

第一次运行先建立“不通知存量商品”的基线：

```bash
.venv/bin/python scripts/xianyu.py monitor \
  --tasks-file /absolute/private/path/tasks.json \
  --task-id TASK_ID \
  --baseline
```

`TASK_ID` 来自创建命令的 `result.id`。只有明确希望处理文件中所有活跃任务时，才省略
`--task-id`。基线命令仍会输出 JSON，必须确认退出码为 0 且 `"ok": true`，不能把
“不通知”理解为“不验证输出”。

之后正常运行：

```bash
.venv/bin/python scripts/xianyu.py monitor \
  --tasks-file /absolute/private/path/tasks.json \
  --task-id TASK_ID
```

脚本只返回之前没见过的商品。对于“stdout 即通知”的纯命令调度器，可增加：

```bash
.venv/bin/python scripts/xianyu.py monitor \
  --tasks-file /absolute/private/path/tasks.json \
  --quiet-if-empty
```

该参数会关闭本次运行的常规进度日志；成功且 `new_count == 0` 时也不输出最终 JSON。
失败仍会输出错误 JSON 并返回非零状态，且不能与 `--include-seen` 或 `--baseline`
同时使用。停止的任务即使通过 `--task-id` 明确选择也不会运行，需要先 `resume`。
旧任务若仍保存相对登录状态路径，升级后不会被静默改指到另一文件；运行时必须显式
传入已授权的绝对 `--state` 路径，或用绝对路径重建任务。

任务搜索成功后，取消或清理失败仍可能发生在 seen-ID 已提交之后。此时顶层会
`"ok": false` 并返回非零退出码，但对应任务会保留 `items`、`new_count` 和
`persistence.status: recorded`。通知适配器必须既持久化/投递这些条目，又报告本次
失败；下一次运行会去重，直接丢弃这份失败 JSON 可能漏通知。需要可靠投递时先写本地
原子 outbox，再独立重试发送。

如果任务文件的原子替换可能已发生、但提交核验失败，任务会保留候选 `items`，并输出
`persistence.status: not-established` 与 `possible_duplicate: true`。仍应按
at-least-once 语义进入 outbox，同时报告失败并允许后续重复；重复通知优于已提交 ID
导致的永久漏报。

任务中的 `criteria` 只是原样返回给 Agent 的自然语言分析提示，不是可执行过滤器。
纯命令模式严格执行的条件只有关键词、价格和地区。

## 定时运行

核心要求只有一个：调度器周期性执行上面的单次命令，并使用绝对路径。一个普通
`cron` 示例：

```cron
*/30 * * * * cd /absolute/path/xianyu-monitor && /absolute/path/xianyu-monitor/.venv/bin/python scripts/xianyu.py monitor --tasks-file /absolute/private/path/tasks.json --task-id TASK_ID --state /absolute/private/path/xianyu-state.json --quiet-if-empty
```

创建任务前，需要用户明确授权这个周期任务读取指定 `tasks.json` 路径和每个确切的
登录状态路径，且用途仅限闲鱼搜索。隔离式 Agent 的持久提示词应记录这条非敏感授权
声明，不能写入 Cookie 内容。

Codex、Claude Code 与 OpenClaw 的 Agent 定时提示词、发现目录和 OpenClaw
`cron create` 示例统一放在
[宿主适配文档](references/host_adapters.md)。核心 Skill 不再包含 `{baseDir}`、
`HEARTBEAT_OK`、`NO_REPLY` 或任何宿主专属调度语法。

## 常见问题

### 是否依赖 OpenClaw？

不依赖。OpenClaw 是受支持的宿主之一；Codex、Claude Code、纯 CLI 和普通系统
调度器也能使用同一套脚本。

### 是否内置通知或 AI 商品判断？

不内置。核心输出结构化 JSON，由宿主决定如何分析和投递；没有数据证据的信用、真假、
成色和维修历史必须标记为未知。

### 是否会绕过 CAPTCHA 或平台风控？

不会。检测到登录挑战、CAPTCHA、`RGV587` 或其他风险控制时会停止并明确报错。

### 是否等同于 `ai-goofish-monitor` Web 系统？

不等同。本项目是轻量 Agent Skill/CLI，没有 Web UI、数据库或 Docker 服务；兼容其
浏览器扩展导出的 Playwright 状态。

### 为什么确认后浏览器会关闭？

本地确认页会先显示验证中；真正完成原子保存后再显示“候选状态已安全保存”，保留 5 秒
才主动关闭专用浏览器并释放临时资源。最终成功 JSON 的 `exit_reason` 是 `completed`；
失败与用户取消分别是 `failed` 和 `cancelled`。是否真正可搜索仍以紧随其后的受控搜索
结果为准。

## 开发与验证

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

自动化测试使用模拟页面和接口，不会登录闲鱼或启动真实浏览器。发布前如需账号级
smoke test，必须由用户本人在本机可见浏览器中登录并输入一次性确认词；不要把凭据
加入测试夹具或 CI。

离线测试同时覆盖非 TTY fail-closed、浏览器确认、旧 raw-CDP 参数拒绝和旧 profile
迁移清理；这些用例只使用合成状态，不读取真实 profile 或网络。

## 安全说明

- 登录状态相当于账号凭证，不要上传、截图或发给他人。
- 状态与任务文件应放在 checkout 之外；确需放入仓库目录时，只使用已整体忽略的
  根目录 `private/`，不要依赖自定义文件名恰好被忽略。
- 登录命令只在 stdout 输出一个最终 JSON；`browser-opening`、
  `browser-confirmation-ready`、`browser-confirmation-accepted` 和
  `browser-confirmation-complete`（或保存后命令未完整结束时的
  `browser-confirmation-warning`）进度写入 stderr。
- 登录命令不会在 JSON 或进度中回显私有状态路径；即便如此，也不要把
  本地登录日志上传到 CI、工单或公共聊天。
- 建议监控间隔不少于 30 分钟。
- `SearchTransportError` 是可重试的临时传输错误，重试次数由 `--retries` 限制。
- 遇到 `SearchCaptureError` 不会自动重试；候选状态已进入搜索页时最多手动执行
  一次 `--headed`。
- 遇到 `SearchRejectedError` 应停止重试并报告；`RGV587` 时让请求/会话冷却，
  此时账号身份仍未知。不重新登录、不换代理，也不再用 `--headed` 尝试。
- AI 只能依据抓取字段分析；卖家信用、维修史、真伪和实际成色需要人工核验。
- 请遵守闲鱼服务条款和当地法律。本项目仅供学习与个人辅助使用。

## English

Xianyu Monitor is a host-neutral Agent Skill and CLI for browser-state-backed
Xianyu searches and recurring listing checks. Its core is independent from Codex,
Claude Code, OpenClaw, schedulers, and notification transports. It provides real
pagination, strict local filters, persistent deduplication, stable JSON, tests,
and optional host adapters. See `SKILL.md` for the agent workflow and
`references/host_adapters.md` for installation and scheduling.

## License

MIT. The project was inspired by
[Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor).
