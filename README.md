# Xianyu Monitor Skill

一个轻量、可审计的闲鱼搜索与监控 Skill。通过 Playwright 使用用户自己的登录状态，
支持真实翻页、价格/地区过滤、任务持久化、跨次去重，以及 OpenClaw 定时运行。

> 本项目只负责搜索、去重和结构化输出，不会自动购买、联系卖家或替用户做真实性保证。

## 这次重构解决了什么

- 精确捕获搜索 POST 接口，不再误匹配 `.search.shade`。
- 使用页面的下一页按钮，不再重复请求第一页。
- 价格和地区在本地严格过滤，不依赖闲鱼忽略的 URL 参数。
- 新增 `monitor.py`，把任务、执行记录和新商品去重串成完整闭环。
- Cookie 与任务文件使用原子写入和 `0600` 权限。
- 代理日志自动隐藏用户名和密码。
- 移除 `--no-sandbox`、`--disable-web-security` 和不稳定 Canvas 噪声。
- 更新到当前 OpenClaw `--every/--cron + --message` 调度方式。
- 增加 pytest、Ruff 和 GitHub Actions。

## 安装

需要 Python 3.10+。

```bash
mkdir -p ~/.openclaw/workspace/skills
git clone \
  https://github.com/LENKIN233/xianyu-monitor-skill.git \
  ~/.openclaw/workspace/skills/xianyu-monitor
cd ~/.openclaw/workspace/skills/xianyu-monitor

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
openclaw skills list
```

如果只使用 CLI，可以克隆到任意目录。作为 OpenClaw Skill 使用时，应放在已配置的
skills root 下；安装后新开会话或重启 Gateway，确保隔离任务能够解析
`$xianyu-monitor`。

## 登录状态

推荐使用浏览器扩展导出的 Playwright storage state。项目同时兼容原
`ai-goofish-monitor` 扩展的标准和增强快照。

如果只有 Cookie Header，可从标准输入安全生成：

```bash
python scripts/create_state.py \
  --cookie-stdin \
  --output /absolute/private/path/xianyu-state.json
```

粘贴 Cookie 后发送 EOF。避免使用 `--cookie "..."`，因为参数可能进入 shell 历史。
浏览器扩展导出的文件也应执行 `chmod 600 /absolute/path/to/state.json`。

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

登录失效、风控或网络失败会输出 `"ok": false` 并返回非零退出码，不会伪装成
“0 个商品”。

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

运行所有活跃任务：

```bash
python scripts/monitor.py \
  --tasks-file /absolute/private/path/tasks.json \
  --baseline
```

先用 `--baseline` 静默记录当前存量商品。之后去掉该参数运行，脚本只输出之前没见过的
商品 ID。

## OpenClaw 定时任务

```bash
openclaw cron add \
  --name "xianyu-monitor" \
  --every 2h \
  --session isolated \
  --message 'Use $xianyu-monitor to run all active tasks from /absolute/private/path/tasks.json. Analyze and report only newly observed listings. If new_count is zero, return HEARTBEAT_OK with no prose. Report failures plainly.' \
  --announce
```

`--announce` 需要已有可用投递目标；没有“最近会话”或存在多个渠道时，按 OpenClaw
配置补充 `--channel CHANNEL --to TARGET`。

固定时间可改用：

```bash
openclaw cron add \
  --name "xianyu-daily" \
  --cron "0 9,21 * * *" \
  --tz "Asia/Shanghai" \
  --session isolated \
  --message 'Use $xianyu-monitor to run all active tasks and report only new listings. If new_count is zero, return HEARTBEAT_OK with no prose.' \
  --announce
```

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
```

## 安全说明

- 登录状态相当于账号凭证，不要上传、截图或发给他人。
- 建议监控间隔不少于 30 分钟。
- 遇到 `SearchRejectedError` 应停止重试并等待账号冷却。
- AI 只能依据抓取字段分析；卖家信用、维修史、真伪和实际成色需要人工核验。
- 请遵守闲鱼服务条款和当地法律。本项目仅供学习与个人辅助使用。

## English

Xianyu Monitor is a small Playwright-based skill for one-time Xianyu searches and
recurring listing alerts. It supports authenticated browser state, real
pagination, strict local filters, persistent deduplication, current OpenClaw
scheduling, tests, and CI. See `SKILL.md` for the agent workflow and
`references/api_reference.md` for CLI contracts.

## License

MIT. The project was inspired by
[Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor).
