# 闲鱼搜索与上新提醒

[![CI](https://github.com/LENKIN233/xianyu-monitor-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/LENKIN233/xianyu-monitor-skill/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

搜商品、筛价格和地区，保存搜索条件，持续查看新出现的商品。标题里不想看到的词可以
直接排除，例如“保护壳”“求购”。结果可以按价格排序，也可以发送到 Bark、企业微信或 Webhook。

可以在命令行单独使用，也可以作为 Skill 交给 Codex、Claude Code 或 OpenClaw。
搜索和监控不需要 AI API Key；需要按自然语言要求挑选商品时，再接入 AI 分析。

## 先看看结果

下载或克隆项目后，在项目目录运行：

```bash
python3 scripts/xianyu.py demo --format text
```

这会显示几件模拟商品，不用登录，也不联网。想看供脚本处理的完整 JSON，去掉
`--format text` 即可。

## 安装和登录

需要 Python 3.10 或更新版本。macOS、Linux：

```bash
git clone https://github.com/LENKIN233/xianyu-monitor-skill.git
cd xianyu-monitor-skill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/xianyu.py doctor
```

Windows 请将后续的 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`；
多行命令合成一行再运行。创建个人目录等步骤见
[Windows 安装说明](references/host_adapters.md#windows-powershell-initialization)。

`doctor` 会检查依赖和浏览器。提示 `install-browser` 时，运行
`.venv/bin/python -m playwright install chromium`；提示
`ready-use-browser-channel` 时，在后面的 `setup`、`search` 和任务创建命令中加上
`--browser-channel chrome`。

接着完成登录和第一次搜索：

```bash
mkdir -p "$HOME/.local/share/xianyu"
chmod 700 "$HOME/.local/share/xianyu"
.venv/bin/python scripts/xianyu.py setup \
  --state "$HOME/.local/share/xianyu/state.json" \
  --keyword "MacBook Air M2" \
  --capture-state
```

在新开的浏览器里扫码，完成手机端确认，再按浏览器确认页的提示保存。保存成功后，
程序会搜索一页来检查能否使用。已有登录文件时会直接使用，不会重新登录或覆盖文件。

登录文件包含账号凭据，请放在个人目录，不要提交到 Git 或发给别人。
单独登录、导入已有登录文件等用法见 [登录说明](references/api_reference.md#login_statepy)。

## 搜索和筛选

例如，找上海 3000–5500 元的 MacBook Air，排除保护壳和求购信息：

```bash
.venv/bin/python scripts/xianyu.py search \
  --keyword "MacBook Air M2" \
  --min-price 3000 --max-price 5500 --location "上海" \
  --exclude "保护壳" --exclude "求购" \
  --pages 2 \
  --state "$HOME/.local/share/xianyu/state.json" > results.json

.venv/bin/python scripts/xianyu.py view \
  --input results.json --sort price-asc --limit 10
```

`view` 显示标题、价格、地区和商品链接。它只读取已有结果，不会再请求闲鱼。
没有价格的商品排在最后；`price-desc` 改为从贵到便宜，默认则保留原顺序。

排除词按标题中的文字匹配，不区分英文大小写或全角半角。它不会自动判断商品类别：
排除“配件”也会排除“送原装配件”，所以尽量使用具体的词。最多可设置 20 个排除词。

已有结果也能再筛选，或整理成 Markdown：

```bash
.venv/bin/python scripts/xianyu.py view \
  --input results.json --exclude "租赁" --format markdown > shortlist.md
```

这里的筛选只影响清单；想让监控也使用排除词，需要在监控任务中设置。
`view` 也支持 `monitor` 和 `analyze` 的输出。原始结果若包含失败，清单会保留失败提示并
返回非零退出码，不会把它当成“没有商品”。

## 持续查看新商品

创建一个任务，保存价格、地区和排除词：

```bash
.venv/bin/python scripts/xianyu.py task \
  --data-file "$HOME/.local/share/xianyu/tasks.json" \
  create "MacBook Air M2" \
  --max-price 5500 --location "上海" --exclude "保护壳" \
  --state "$HOME/.local/share/xianyu/state.json"
```

记下返回的 `result.id`，替换下面的 `TASK_ID`。新任务先记录当前商品，避免把存量商品
当成上新；已在使用的任务不需要重做这一步。

```bash
.venv/bin/python scripts/xianyu.py monitor \
  --tasks-file "$HOME/.local/share/xianyu/tasks.json" \
  --task-id TASK_ID --baseline
```

后续运行时去掉 `--baseline`，就只返回新出现的商品：

```bash
.venv/bin/python scripts/xianyu.py monitor \
  --tasks-file "$HOME/.local/share/xianyu/tasks.json" \
  --task-id TASK_ID > new-items.json

.venv/bin/python scripts/xianyu.py view --input new-items.json
```

标题命中排除词的商品不会进入新的通知队列。修改条件用 `task update`：`--exclude`
替换排除词，`--clear-excludes` 清空，先 `--preview` 再按返回的摘要 `--apply`。
修改筛选不会重发已经见过的商品，也不会删除此前待发送的通知。

定时运行由宿主或系统调度器负责，建议间隔至少 30 分钟。命令中的 Python、脚本、任务和
登录文件都用绝对路径。[定时任务与安装到各宿主](references/host_adapters.md)有完整示例。

## 发送通知

监控会把待发送的商品保存在任务文件中。通知单独发送，发送失败时可以检查剩余记录，
不用重新抓取商品。

| 通知方式 | 在运行环境中设置 |
|---|---|
| Webhook | `XIANYU_WEBHOOK_URL` |
| Bark | `XIANYU_BARK_URL` |
| 企业微信 | `XIANYU_WECOM_WEBHOOK_URL` |

先检查这一批要发什么：

```bash
.venv/bin/python scripts/xianyu.py deliver \
  --data-file "$HOME/.local/share/xianyu/tasks.json" \
  --adapter bark --task-id TASK_ID --preview
```

确认内容和目标后，将 `--preview` 换为
`--send --expected-preview-sha256 返回的摘要`。接口地址留在环境变量里，不要放进命令参数。

只有收到服务端成功回复，商品才会从待发送列表移除。超时不一定代表没送达；出现
`possible_duplicate: true` 时，先检查接收端，避免重复发送。
[详细参数](references/api_reference.md#deliverpy)。

## 用 AI 帮忙挑选

例如想找“16GB、上海自提的机器”，可以把搜索结果交给 `analyze`，它会列出匹配理由和
还需要向卖家确认的信息。先预览将要发送的商品字段，再按同一摘要调用你配置的模型。

需要配置 `OPENAI_API_KEY`；兼容接口可通过 `XIANYU_AI_BASE_URL` 设置地址。
操作示例见 [AI 分析](references/api_reference.md#analyzepy)。AI 只能分析已抓到的文字，
是否修过、真伪和实际成色仍需另行核实。

## 升级已有安装

当前版本为 **2.0.0-rc.2**，仍是候选版。发布包和校验文件在
[Releases](https://github.com/LENKIN233/xianyu-monitor-skill/releases)，
每次变化见 [CHANGELOG.md](CHANGELOG.md)。

升级前暂停定时任务，等正在运行的任务结束，备份完整任务文件并保留旧程序。
安装新版本和依赖后，运行 `version`、`doctor`、`demo --format text`，再恢复调度。

旧任务文件（schema 1–3）可以读取，首次成功写入时升为 schema 4；已有任务、去重记录和
待发送通知都会保留。RC1 或更早版本不支持新格式，回退时使用升级前的备份。
`task export` 只导出搜索条件，不包含去重历史和待发送通知，不能代替备份。

copy 安装不会覆盖已有目录；symlink 安装随其指向的项目目录更新。
用 `install --check` 查看当前安装状况，具体步骤见
[升级说明](references/host_adapters.md#upgrade-strategy)。

## 遇到问题

- **提示登录或验证**：在本机浏览器完成，不会自动绕过验证码或风控。
- **返回 `RGV587` 或 `SearchRejectedError`**：停止重试，稍后再使用。
- **没有捕获到搜索结果**：若没有登录或风控提示，可加 `--headed` 尝试一次。
- **想让空结果保持安静**：定时命令可用 `monitor --quiet-if-empty`；失败仍会输出错误。
- **想停止任务**：运行 `task --data-file 任务文件 stop TASK_ID`；恢复用 `resume`。

其余参数可运行 `scripts/xianyu.py COMMAND --help` 查看，JSON 字段和错误含义见
[CLI 参考](references/api_reference.md)。

## 开发

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python scripts/release_bundle.py self-check
```

自动化测试使用模拟数据。真实搜索、AI 和通知是否可用，需要用实际账号和所选服务验证。
[实现说明](references/architecture.md) · [后续计划](ROADMAP.md) ·
[问题报告](SECURITY.md) · [发布包说明](references/api_reference.md#release_bundlepy)

Xianyu Monitor is a local CLI and Agent Skill for searching Goofish listings and
checking for new matches. It supports title exclusions, readable result reports,
and optional AI analysis and notifications.

MIT License. 本项目参考了 [ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor)。
