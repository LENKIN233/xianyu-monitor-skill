# Xianyu Monitor Skill

一个遵循 [Agent Skills 开放规范](https://agentskills.io/specification) 的闲鱼搜索与监控
Skill。核心只依赖 Python、Playwright 和闲鱼浏览器状态，不依赖 OpenClaw、Codex、
Claude Code 或任何特定调度器。

它可以作为：

- Codex / ChatGPT 桌面端 Skill；
- Claude Code Skill；
- OpenClaw Skill；
- 纯 CLI 工具；
- `cron`、`systemd`、`launchd`、Windows Task Scheduler 等任务的一次性命令。

> 本项目只负责搜索、过滤、去重和结构化输出，不会自动购买、联系卖家或替用户做
> 真实性保证。

## 核心能力

- 精确捕获闲鱼搜索 GET/POST 接口，避免误匹配其他接口。
- 使用真实的下一页控件，不会重复抓取第一页。
- 在本地严格执行价格和地区过滤。
- 使用持久任务文件记录条件、运行结果与已见商品 ID。
- 观察到登录跳转或挑战、风控和抓取失败会明确报错，不会伪装成“没有新商品”。
- Cookie、任务文件采用原子写入；POSIX 使用 `0600`，Windows 依赖私有目录 ACL。
- 代理日志隐藏用户名和密码。
- HTTP(S) 认证代理可通过私有文件或 `XIANYU_PROXY` 注入，不必写入命令参数。
- 输出稳定 JSON，便于任意 Agent 或调度器消费。
- 成功且没有新商品时可用 `--quiet-if-empty` 保持无输出。

## 通用架构

```text
任意 Agent / CLI / 调度器
          |
          v
 scripts/monitor.py  ---> JSON / exit code ---> 任意通知渠道
          |
          +-- scripts/spider.py ---> Playwright ---> 闲鱼
          |
          +-- scripts/task_manager.py ---> tasks.json
```

调度和消息投递属于宿主适配层，不进入爬虫、任务文件或 `SKILL.md` 核心流程。因此
OpenClaw 可以继续使用，但不再是运行前提。

## 安装

需要 Python 3.10+。

```bash
git clone \
  https://github.com/LENKIN233/xianyu-monitor-skill.git \
  xianyu-monitor
cd xianyu-monitor

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Windows PowerShell：

```powershell
py -3 -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Windows 定时任务直接调用 `.\.venv\Scripts\python.exe`。运行所需的 IANA
时区数据会由 `requirements.txt` 安装，因此精简 Linux 容器和 Windows 都不依赖
系统时区包。后续示例中的反斜杠是 POSIX shell 换行；PowerShell 可写成单行或使用
反引号续行，参数本身相同。交互运行 `--cookie-stdin` 时，终端会隐藏输入；粘贴一行
Cookie 后按 Enter。管道或重定向输入仍以 EOF 结束。

只用 CLI 时，到这里即可。

### 安装到多个 Agent

先预览，不会写入：

```bash
python scripts/install_skill.py --host all --mode symlink --dry-run
```

确认后安装：

```bash
python scripts/install_skill.py --host all --mode symlink
```

这个命令把同一个 checkout 暴露到两个发现目录：

| 宿主 | 目录 | 调用方式 |
|---|---|---|
| Codex | `~/.agents/skills/xianyu-monitor` | `$xianyu-monitor` |
| Claude Code | `~/.claude/skills/xianyu-monitor` | `/xianyu-monitor` |
| OpenClaw | `~/.agents/skills/xianyu-monitor` | `/skill xianyu-monitor` |

OpenClaw 中可移植的显式调用是 `/skill xianyu-monitor`。运行时可能另外展示一个
经过规范化的原生快捷命令；其确切名称由当前版本决定，不要假定它一定是
`/xianyu-monitor`。

当前 [Codex](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)
与 [OpenClaw](https://docs.openclaw.ai/skills) 官方文档都列出
`~/.agents/skills`。不支持目录软链接时，改用
`--mode copy`；复制模式不会带上 `.git`、虚拟环境、测试缓存或本地任务/登录文件，
也不会覆盖已有路径。一次多宿主安装若中途失败，会回滚本次已创建的 Skill；已有
symlink 与请求的 copy 模式不一致时会明确失败，不会伪装成复制安装成功。
两种模式都先在同一文件系统的私有临时目录中构造，再用平台原子的
no-replace rename 一次发布；文件系统不支持该保证时会安全失败，不退回到
check-then-rename。

各宿主的项目级安装、定时任务和注意事项见
[references/host_adapters.md](references/host_adapters.md)。

## 浏览器状态

推荐使用内置登录命令。它会打开独立的可见浏览器，扫码、验证码和 CAPTCHA 必须由
用户本人完成。扫码后通常还要在手机上点击确认登录；二维码消失只表示扫码流程发生了
变化，不等于登录完成。等浏览器回到正常 Goofish 页面后，再提交最终确认：

```bash
python scripts/login_state.py \
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
可信宿主上，用 `--browser-channel chrome` 完整执行 `login_state.py`、`spider.py` 和
`monitor.py`，并只在该宿主的任务/调度配置中引用权限为 `0600` 的状态文件。Agent 可
消费命令输出的商品 JSON，但不得接收浏览器 profile 或状态内容。

从支持 CDP 的旧版本升级时，先关闭旧专用 Chrome；若仍保留由旧版初始化的临时
profile，只把 `cdp_profile.py` 用作受保护的迁移清理工具：

```bash
python scripts/cdp_profile.py \
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
`search_capability.status: passed-for-this-run`；`login_state.py` 成功本身不完成能力
验证。搜索通过也只能证明该浏览器上下文在当次运行能够搜索，不能证明已认证或登录的
是哪个账号。浏览器清理或本地持久化仍可能失败，因此必须分别读取 `ok` 和能力字段。
`RGV587` 也只能证明请求被拒绝，不能证明账号身份。

项目兼容其他浏览器工具导出的 Playwright storage state，以及原
`ai-goofish-monitor` 扩展的标准和增强快照；导入文件不自带用户确认记录。无论
来源，状态在传入浏览器前都会按结构校验，并只保留 Goofish 域 Cookie 与规范的
Goofish HTTPS origin，第三方凭据不会进入搜索上下文。

如果只有 Cookie Header，从标准输入安全生成：

```bash
python scripts/create_state.py \
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
python scripts/spider.py \
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
python scripts/task_manager.py \
  --data-file /absolute/private/path/tasks.json \
  create "MacBook Air M2" \
  --max-price 6000 \
  --location "上海" \
  --pages 2 \
  --state /absolute/private/path/xianyu-state.json
```

任务文件按完整 schema 加载：字段类型、ID 唯一性、列表上限和有限价格都会验证。任一
条目损坏时整次操作失败且不重写原文件，不会静默丢弃无法识别的任务。

第一次运行先建立“不通知存量商品”的基线：

```bash
python scripts/monitor.py \
  --tasks-file /absolute/private/path/tasks.json \
  --task-id TASK_ID \
  --baseline
```

`TASK_ID` 来自创建命令的 `result.id`。只有明确希望处理文件中所有活跃任务时，才省略
`--task-id`。基线命令仍会输出 JSON，必须确认退出码为 0 且 `"ok": true`，不能把
“不通知”理解为“不验证输出”。

之后正常运行：

```bash
python scripts/monitor.py \
  --tasks-file /absolute/private/path/tasks.json \
  --task-id TASK_ID
```

脚本只返回之前没见过的商品。对于“stdout 即通知”的纯命令调度器，可增加：

```bash
python scripts/monitor.py \
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
*/30 * * * * cd /absolute/path/xianyu-monitor && /absolute/path/xianyu-monitor/.venv/bin/python scripts/monitor.py --tasks-file /absolute/private/path/tasks.json --task-id TASK_ID --state /absolute/private/path/xianyu-state.json --quiet-if-empty
```

创建任务前，需要用户明确授权这个周期任务读取指定 `tasks.json` 路径和每个确切的
登录状态路径，且用途仅限闲鱼搜索。隔离式 Agent 的持久提示词应记录这条非敏感授权
声明，不能写入 Cookie 内容。

Codex、Claude Code 与 OpenClaw 的 Agent 定时提示词、发现目录和 OpenClaw
`cron create` 示例统一放在
[宿主适配文档](references/host_adapters.md)。核心 Skill 不再包含 `{baseDir}`、
`HEARTBEAT_OK`、`NO_REPLY` 或任何宿主专属调度语法。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
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
- 登录命令只在 stdout 输出一个最终 JSON；`browser-opening` 和可选的
  `browser-confirmation-ready` 进度写入 stderr。
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
