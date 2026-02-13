---
name: xianyu-monitor
description: 闲鱼智能监控机器人 Skill - AI 原生版。通过文本对话完成闲鱼商品监控、AI 分析、推送通知。支持登录状态绕过反爬，无需 Web UI。
---

# Xianyu Monitor - 闲鱼智能监控机器人（AI 原生版）

AI 直接作为监控大脑，通过对话完成任务。支持登录状态绕过反爬。

## 核心设计

### 传统方案 vs AI 原生方案

**传统方案：**
```
用户 → Web UI → 数据库 → 爬虫 → OpenAI API → 通知
```

**AI 原生方案（本 Skill）：**
```
用户对话 → AI → Python 脚本 → 爬虫（带登录状态）→ AI 分析 → 通知
```

## 关键：登录状态

闲鱼反爬很强，**必须使用真实登录状态**：

### 获取登录状态

**方式1：Chrome 扩展（推荐）**
1. 安装原项目的 Chrome 扩展
2. 登录闲鱼网页版
3. 点击扩展提取登录状态
4. 保存为 `state.json`

**方式2：手动复制 Cookie**
```bash
python scripts/create_state.py --cookie "your_cookie_string" --output state.json
```

### Cookie 获取方法

1. 浏览器登录 https://www.goofish.com
2. F12 打开开发者工具 → Application/Storage → Cookies
3. 复制关键 cookie（_tb_token_, cookie2 等）
4. 用 create_state.py 生成 state.json

## 使用方法

### 1. 安装依赖

```bash
cd ~/.openclaw/workspace/skills/xianyu-monitor
pip install playwright requests
playwright install chromium
```

### 2. 创建监控任务

直接告诉我：

> "帮我监控闲鱼的 iPhone 14 Pro，预算 5000 以内，登录状态文件在 ./state.json"

我会：
1. 创建任务配置
2. 调用 spider.py --state ./state.json 抓取
3. 分析每个商品
4. 好货立即通知你

### 3. 命令行直接使用

```bash
# 基础搜索（需要登录状态）
python scripts/spider.py \
  --keyword "iPhone 14 Pro" \
  --max-price 5000 \
  --state ./state.json \
  --pages 1

# 使用代理（推荐，降低被封风险）
python scripts/spider.py \
  --keyword "MacBook Air" \
  --max-price 6000 \
  --state ./state.json \
  --proxy "http://127.0.0.1:7890"

# 增加重试次数
python scripts/spider.py \
  --keyword "iPad Pro" \
  --state ./state.json \
  --retries 5
```

### 4. AI 分析流程

```
1. 你提出需求
2. 我调用 spider.py 抓取（使用你的登录状态）
3. 我查看返回的 JSON 商品数据
4. 我分析判断：价格是否合理、卖家是否可信、商品是否值得买
5. 好货 → 立即推送通知
```

## 技术实现

### 爬虫模块 (`scripts/spider.py`)

**关键特性：**
- 加载登录状态文件（`--state` 参数）
- **随机 UA 和视口**（模拟多种设备）
- **请求频率限制**（5-10秒随机间隔）
- **智能重试机制**（指数退避）
- **代理支持**（`--proxy` 参数）
- **Canvas 指纹混淆**（反检测）
- 拦截 API 响应（不是解析 HTML）

**示例输出：**
```json
{
  "keyword": "iPhone 14 Pro",
  "count": 15,
  "items": [
    {
      "id": "123456789",
      "title": "iPhone 14 Pro 256G 暗紫色",
      "price": 4800,
      "url": "https://www.goofish.com/item/123456789",
      "image": "...",
      "location": "上海",
      "seller": {
        "name": "数码玩家",
        "id": "user_xxx"
      },
      "raw_data": { ... }
    }
  ]
}
```

### 登录状态格式

`state.json`:
```json
{
  "cookies": [
    {
      "name": "_tb_token_",
      "value": "xxx",
      "domain": ".goofish.com",
      "path": "/"
    }
  ],
  "origins": []
}
```

## 工作流程示例

**用户：** 帮我找 MacBook Air M2，预算 6000 以内，登录状态在 ./my_state.json

**AI：**
> 好的，已创建监控任务：
> - 关键词：MacBook Air M2
> - 预算：≤6000元
> - 登录状态：./my_state.json
> 
> 正在抓取...（使用你的登录状态）

[等待 10-30 秒]

**AI：**
> ✅ 抓取完成！发现 12 个商品
> 
> 正在分析...
> 
> ---
> 
> ❌ **商品1** - ¥5800
> 卖家：商家（有大量同类商品）
> 风险：疑似翻新机，不推荐
> 
> ✅ **商品2** - ¥5900 ⭐推荐
> 卖家：个人用户（信用极好）
> 描述：自用一手，电池 95%，箱说全
> 判断：价格合理，卖家可信
> [链接]
> 
> ❌ **商品3** - ¥5500
> 卖家：新注册账号
> 风险：账号可疑，建议谨慎
> 
> ---
> 
> 找到 1 个好货！已推送通知。

## 定时监控（自动检测）

除了手动查询，还可以设置定时任务自动监控。

### 使用 OpenClaw Cron

推荐方式：利用 OpenClaw 内置的定时任务系统。

#### 设置定时任务

```bash
# 每2小时检查一次
openclaw cron add \
  --name "xianyu-surface-monitor" \
  --schedule "every 2h" \
  --command "监控 Surface Laptop Studio 2，预算6000元，登录状态在 ./state.json，发现新商品立即通知"

# 每天上午9点和晚上9点检查
openclaw cron add \
  --name "xianyu-iphone-monitor" \
  --schedule "0 9,21 * * *" \
  --command "检查 iPhone 15 Pro 的新商品，预算5000-7000"
```

#### 常用 Cron 表达式

| 频率 | 表达式 |
|-----|-------|
| 每30分钟 | `*/30 * * * *` |
| 每2小时 | `0 */2 * * *` |
| 每天早9点 | `0 9 * * *` |
| 每周一9点 | `0 9 * * 1` |

#### 管理定时任务

```bash
# 查看所有任务
openclaw cron list

# 暂停任务
openclaw cron pause xianyu-surface-monitor

# 恢复任务
openclaw cron resume xianyu-surface-monitor

# 删除任务
openclaw cron remove xianyu-surface-monitor
```

### 定时任务执行流程

```
Cron触发 → 唤醒AI → 调用spider.py抓取 → AI分析 → 有新好货则推送通知
```

### 建议频率

- **热门商品**：每1-2小时（如 iPhone、MacBook）
- **普通商品**：每4-6小时
- **稀有商品**：每天1-2次即可

⚠️ **注意**：过于频繁的抓取可能导致登录状态被风控，建议最短间隔30分钟。

## 反反爬配置

### 已实现的反检测措施

本 Skill 已内置多层反反爬机制：

| 措施 | 说明 |
|-----|------|
| **随机 UA** | 每次请求随机选择 5 种真实设备 UA |
| **随机视口** | 模拟不同手机屏幕尺寸 |
| **请求间隔** | 5-10 秒随机延迟，模拟人类行为 |
| **Canvas 混淆** | 添加随机噪声，防止指纹识别 |
| **重试机制** | 失败时指数退避重试（5s, 10s, 15s）|
| **代理支持** | 支持 HTTP/HTTPS/SOCKS5 代理 |

### 使用代理（强烈推荐）

如果已被风控，建议使用代理：

```bash
# Clash/V2Ray 本地代理
python scripts/spider.py --proxy "http://127.0.0.1:7890"

# 私密代理
python scripts/spider.py --proxy "http://user:pass@proxy.example.com:8080"

# SOCKS5 代理
python scripts/spider.py --proxy "socks5://127.0.0.1:1080"
```

**推荐代理服务商：**
- 快代理、站大爷、阿布云（国内）
- Bright Data、Oxylabs（国际）

### 风控规避建议

1. **降低频率**：每次抓取间隔至少 5 分钟
2. **使用代理**：每次请求更换 IP
3. **分散时间**：不要集中在同一时间段抓取
4. **更新登录状态**：定期刷新 state.json
5. **减少关键词**：每次只搜索 1-2 个关键词

### 被封后处理

如果触发风控（返回空结果或验证码）：

1. **暂停 24 小时** - 让账号冷却
2. **更换 IP** - 使用代理或更换网络
3. **重新登录** - 用 Chrome 扩展重新提取 state.json
4. **降低频率** - 延长抓取间隔到 30 分钟以上

## 注意事项

1. **登录状态会过期** - 通常几天到几周，过期后需要重新获取
2. **Cookie 隐私** - 登录状态文件包含敏感信息，不要分享给他人
3. **请求频率** - 不要过于频繁抓取，避免触发风控
4. **IP 限制** - 如果频繁抓取，可能需要使用代理

## 文件结构

```
xianyu-monitor/
├── SKILL.md                    # 使用说明
├── scripts/
│   ├── spider.py              # 爬虫（需登录状态）
│   ├── task_manager.py        # 任务管理
│   ├── create_state.py        # 创建登录状态文件
│   └── state_example.json     # 登录状态示例
├── state.json                 # 你的登录状态（由Chrome扩展生成）
├── tasks.json                 # 监控任务列表
└── references/
    └── ...
```

**注意**：本 Skill 是独立完整的，不依赖 `ai-goofish-monitor` 目录（那是原项目备份，可删除）。

## 与原项目的区别

| 特性 | 原项目 | 本 Skill |
|-----|-------|---------|
| Web UI | ✅ 有 | ❌ 无 |
| 数据库 | ✅ 有 | ❌ 无 |
| 部署复杂度 | 高 | **极简** |
| AI 角色 | 外部 API | **直接控制** |
| 交互方式 | 网页 | **文本对话** |
| 定时监控 | ✅ APScheduler | ✅ **OpenClaw Cron** |
| 登录状态 | ✅ 支持 | ✅ 支持 |
| 反爬策略 | API 拦截 | **API 拦截** |
