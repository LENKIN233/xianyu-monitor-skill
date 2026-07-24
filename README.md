# Xianyu Monitor Skill

一个遵循 [Agent Skills 开放规范](https://agentskills.io/specification) 的闲鱼搜索与监控
Skill。核心只依赖 Python、Playwright 和闲鱼登录状态，不依赖 OpenClaw、Codex、
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
- 登录失效、风控和抓取失败会明确报错，不会伪装成“没有新商品”。
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

Codex 与新版 OpenClaw 共用 `~/.agents/skills`。不支持目录软链接时，改用
`--mode copy`；复制模式不会带上 `.git`、虚拟环境、测试缓存或本地任务/登录文件，
也不会覆盖已有路径。一次多宿主安装若中途失败，会回滚本次已创建的 Skill；已有
symlink 与请求的 copy 模式不一致时会明确失败，不会伪装成复制安装成功。

各宿主的项目级安装、定时任务和注意事项见
[references/host_adapters.md](references/host_adapters.md)。

## 登录状态

推荐使用内置登录命令。它会打开独立的可见浏览器，扫码、验证码和 CAPTCHA 必须由
用户本人完成：

```bash
python scripts/login_state.py \
  --browser-channel chrome \
  --output "/absolute/private/path/xianyu-state.json"
```

命令不会输出 Cookie，POSIX 下写入权限为 `0600`；除非明确增加 `--force`，否则不会
覆盖已有登录态。项目也兼容其他浏览器工具导出的 Playwright storage state，以及原
`ai-goofish-monitor` 扩展的标准和增强快照。
命令保存的是候选登录态：检测到登录 Cookie 不等于搜索已通过。只用一次受控搜索
验证它；验证前不要把它视为完整可用。

如果只有 Cookie Header，从标准输入安全生成：

```bash
python scripts/create_state.py \
  --cookie-stdin \
  --output /absolute/private/path/xianyu-state.json
```

交互终端会隐藏输入；粘贴一行 Cookie 后按 Enter。若通过管道或重定向传入，则以
EOF 结束。无法关闭终端回显时命令会安全失败。避免使用 `--cookie "..."`，因为参数
可能进入 shell 历史。
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
  "count": 2,
  "pages_scraped": 2,
  "items": []
}
```

登录失效、风控或网络失败会输出 `"ok": false` 并返回非零退出码。闲鱼返回
`RGV587` 等风控错误时立即停止；不要尝试绕过登录挑战、CAPTCHA 或风控。
如果有效登录态已进入 `/search`，但无头模式没有观察到搜索接口，只使用
`--headed` 重试一次；不要循环尝试或加入反检测绕过。

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

自动化测试使用模拟页面和接口，不会登录闲鱼或启动真实浏览器。发布前应在本机用专用
测试账号和私有登录状态执行一次人工 smoke test；不要把凭据加入测试夹具或 CI。

## 安全说明

- 登录状态相当于账号凭证，不要上传、截图或发给他人。
- 建议监控间隔不少于 30 分钟。
- 遇到 `SearchCaptureError` 不会自动重试；确认登录态有效后最多手动执行一次
  `--headed`。
- 遇到 `SearchRejectedError` 应停止重试并报告；`RGV587` 时等待账号冷却，
  不重新登录、不换代理，也不再用 `--headed` 尝试。
- AI 只能依据抓取字段分析；卖家信用、维修史、真伪和实际成色需要人工核验。
- 请遵守闲鱼服务条款和当地法律。本项目仅供学习与个人辅助使用。

## English

Xianyu Monitor is a host-neutral Agent Skill and CLI for authenticated Xianyu
searches and recurring listing checks. Its core is independent from Codex,
Claude Code, OpenClaw, schedulers, and notification transports. It provides real
pagination, strict local filters, persistent deduplication, stable JSON, tests,
and optional host adapters. See `SKILL.md` for the agent workflow and
`references/host_adapters.md` for installation and scheduling.

## License

MIT. The project was inspired by
[Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor).
