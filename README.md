# Xianyu Monitor - AI Native Edition

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Playwright](https://img.shields.io/badge/playwright-1.40+-green.svg)](https://playwright.dev/)

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

### Overview

**Xianyu Monitor** is an AI-native intelligent monitoring robot for Xianyu (Goofish), China's largest second-hand trading platform. Unlike traditional monitoring tools that require complex Web UI and database setup, this skill allows you to monitor products through natural language conversations with your AI assistant.

**Key Features:**
- 🤖 **AI-Native**: No Web UI, no database, just talk to your AI
- 🔒 **Login State Support**: Bypass anti-scraping with real browser login state
- ⏰ **Scheduled Monitoring**: Set up cron jobs for automatic monitoring
- 🛡️ **Anti-Detection**: Built-in multiple anti-scraping measures
- 📱 **Mobile Simulation**: Mimics real mobile devices to avoid detection

### Original Project

This skill is inspired by and based on the architecture of [ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) by Usagi-org. The original project is a full-featured monitoring system with Web UI, database, and Docker support.

**Differences from Original:**

| Feature | Original Project | This Skill |
|---------|-----------------|------------|
| Web UI | ✅ Yes | ❌ No (AI-controlled) |
| Database | ✅ Yes | ❌ No (JSON files) |
| Deployment | Complex (Docker) | **Simple** (Python only) |
| AI Role | External API | **Direct Control** |
| Interaction | Web interface | **Natural language** |
| Scheduling | APScheduler | **OpenClaw Cron** |

### Prerequisites

#### System Requirements
- Python 3.8 or higher
- Windows/macOS/Linux

#### Python Dependencies
```bash
pip install playwright requests
playwright install chromium
```

#### Chrome Extension (Required for Login State)

You need to install the Chrome extension from the original project to extract login state:

1. **Download Extension**
   ```bash
   git clone https://github.com/Usagi-org/ai-goofish-monitor.git
   cd ai-goofish-monitor/chrome-extension
   ```

2. **Install in Chrome**
   - Open Chrome and navigate to `chrome://extensions/`
   - Enable "Developer mode" (top right corner)
   - Click "Load unpacked"
   - Select the `chrome-extension` folder

3. **Extract Login State**
   - Go to [https://www.goofish.com](https://www.goofish.com) and log in
   - Click the extension icon in Chrome toolbar
   - Click "Extract Login State"
   - Copy the JSON and save as `state.json`

**Alternative: Manual Cookie Method**

If you prefer not to use the extension:
```bash
python scripts/create_state.py --cookie "your_cookie_string" --output state.json
```

### Quick Start

#### 1. Clone This Repository
```bash
git clone https://github.com/YOUR_USERNAME/xianyu-monitor-skill.git
cd xianyu-monitor-skill
```

#### 2. Install Dependencies
```bash
pip install playwright requests
playwright install chromium
```

#### 3. Prepare Login State
Place your `state.json` (extracted using Chrome extension) in the project root.

#### 4. Start Monitoring

**Method 1: Direct Conversation with AI**
```
User: "帮我监控 Surface Laptop Studio，预算6000元"
AI:  [Executes spider.py and analyzes results]
```

**Method 2: Command Line**
```bash
# Basic search
python scripts/spider.py \
  --keyword "iPhone 14 Pro" \
  --max-price 5000 \
  --state ./state.json

# With proxy (recommended for frequent use)
python scripts/spider.py \
  --keyword "MacBook Air" \
  --max-price 6000 \
  --state ./state.json \
  --proxy "http://127.0.0.1:7890"
```

### Scheduled Monitoring

Set up automatic monitoring using OpenClaw Cron:

```bash
# Check every 2 hours
openclaw cron add \
  --name "xianyu-monitor" \
  --schedule "every 2h" \
  --command "监控 Surface Laptop，预算6000，登录状态在 ./state.json"

# Daily at 9 AM and 9 PM
openclaw cron add \
  --name "xianyu-daily" \
  --schedule "0 9,21 * * *" \
  --command "检查 iPhone 的新商品"
```

### Anti-Detection Measures

This skill includes multiple anti-scraping protections:

| Measure | Description |
|---------|-------------|
| Random User-Agent | Rotates 5 real device UAs |
| Random Viewport | Simulates different screen sizes |
| Request Delays | 5-10s random intervals |
| Canvas Fingerprinting | Adds noise to prevent tracking |
| Retry Mechanism | Exponential backoff on failures |
| Proxy Support | HTTP/HTTPS/SOCKS5 compatible |

### File Structure

```
xianyu-monitor/
├── SKILL.md                    # Detailed documentation
├── README.md                   # This file
├── scripts/
│   ├── spider.py              # Core scraper (300 lines)
│   ├── task_manager.py        # Task management
│   ├── create_state.py        # Login state generator
│   └── state_example.json     # Example login state
├── references/
│   ├── architecture.md        # Architecture documentation
│   └── api_reference.md       # API documentation
├── state.json                 # Your login state (user data)
└── tasks.json                 # Your tasks (user data)
```

### Troubleshooting

#### Getting 0 Results
**Cause**: Login state expired  
**Solution**: Re-extract login state using Chrome extension

#### HTTP 403/429 Errors
**Cause**: Rate limited by Xianyu  
**Solution**: 
- Wait 24 hours
- Use a proxy: `--proxy "http://proxy:port"`
- Reduce frequency to 30+ minutes

#### Playwright Not Found
```bash
pip install playwright
playwright install chromium
```

### Acknowledgments

- Original project: [ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) by Usagi-org
- Chrome extension for login state extraction from original project
- Built for [OpenClaw](https://github.com/openclaw/openclaw) AI assistant platform

### License

MIT License - See [LICENSE](LICENSE) file

---

<a name="中文"></a>
## 中文

### 项目简介

**闲鱼监控助手**（Xianyu Monitor）是一个 AI 原生的闲鱼智能监控机器人。不同于传统的监控工具需要复杂的 Web UI 和数据库配置，本 Skill 让你可以通过与 AI 对话来完成商品监控任务。

**核心特性：**
- 🤖 **AI 原生**：无需 Web UI，无需数据库，直接对话即可
- 🔒 **登录状态支持**：使用真实浏览器登录状态绕过反爬
- ⏰ **定时监控**：支持 Cron 定时任务自动监控
- 🛡️ **反反爬**：内置多层反检测机制
- 📱 **移动模拟**：模拟真实手机设备避免检测

### 原项目

本 Skill 基于 [ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) 的架构设计，感谢 Usagi-org 的开源贡献。原项目是一个功能完整的监控系统，包含 Web UI、数据库和 Docker 支持。

**与原项目的区别：**

| 特性 | 原项目 | 本 Skill |
|-----|-------|---------|
| Web UI | ✅ 有 | ❌ 无（AI控制） |
| 数据库 | ✅ 有 | ❌ 无（JSON文件） |
| 部署方式 | 复杂（Docker） | **简单**（仅Python） |
| AI 角色 | 外部 API | **直接控制** |
| 交互方式 | 网页界面 | **自然语言对话** |
| 定时监控 | APScheduler | **OpenClaw Cron** |

### 环境要求

#### 系统要求
- Python 3.8 或更高版本
- Windows/macOS/Linux

#### Python 依赖
```bash
pip install playwright requests
playwright install chromium
```

#### Chrome 扩展（必需，用于获取登录状态）

你需要安装原项目提供的 Chrome 扩展来提取登录状态：

1. **下载扩展**
   ```bash
   git clone https://github.com/Usagi-org/ai-goofish-monitor.git
   cd ai-goofish-monitor/chrome-extension
   ```

2. **安装到 Chrome**
   - 打开 Chrome，访问 `chrome://extensions/`
   - 开启右上角"开发者模式"
   - 点击"加载已解压的扩展程序"
   - 选择 `chrome-extension` 文件夹

3. **提取登录状态**
   - 访问 [https://www.goofish.com](https://www.goofish.com) 并登录
   - 点击 Chrome 工具栏上的扩展图标
   - 点击"提取登录状态"
   - 复制 JSON 并保存为 `state.json`

**替代方案：手动 Cookie 方法**

如果不想使用扩展：
```bash
python scripts/create_state.py --cookie "your_cookie_string" --output state.json
```

### 快速开始

#### 1. 克隆仓库
```bash
git clone https://github.com/YOUR_USERNAME/xianyu-monitor-skill.git
cd xianyu-monitor-skill
```

#### 2. 安装依赖
```bash
pip install playwright requests
playwright install chromium
```

#### 3. 准备登录状态
将用 Chrome 扩展提取的 `state.json` 放到项目根目录。

#### 4. 开始监控

**方式一：直接与 AI 对话**
```
用户："帮我监控 Surface Laptop Studio，预算6000元"
AI：  [执行 spider.py 并分析结果]
```

**方式二：命令行**
```bash
# 基础搜索
python scripts/spider.py \
  --keyword "iPhone 14 Pro" \
  --max-price 5000 \
  --state ./state.json

# 使用代理（频繁使用时推荐）
python scripts/spider.py \
  --keyword "MacBook Air" \
  --max-price 6000 \
  --state ./state.json \
  --proxy "http://127.0.0.1:7890"
```

### 定时监控

使用 OpenClaw Cron 设置自动监控：

```bash
# 每2小时检查一次
openclaw cron add \
  --name "xianyu-monitor" \
  --schedule "every 2h" \
  --command "监控 Surface Laptop，预算6000，登录状态在 ./state.json"

# 每天上午9点和晚上9点检查
openclaw cron add \
  --name "xianyu-daily" \
  --schedule "0 9,21 * * *" \
  --command "检查 iPhone 的新商品"
```

### 反反爬措施

本 Skill 包含多种反爬虫保护：

| 措施 | 说明 |
|-----|------|
| 随机 User-Agent | 轮换5种真实设备 UA |
| 随机视口 | 模拟不同屏幕尺寸 |
| 请求延迟 | 5-10秒随机间隔 |
| Canvas 混淆 | 添加噪声防止追踪 |
| 重试机制 | 失败后指数退避 |
| 代理支持 | 支持 HTTP/HTTPS/SOCKS5 |

### 文件结构

```
xianyu-monitor/
├── SKILL.md                    # 详细文档
├── README.md                   # 本文件
├── scripts/
│   ├── spider.py              # 核心爬虫（300行）
│   ├── task_manager.py        # 任务管理
│   ├── create_state.py        # 登录状态生成
│   └── state_example.json     # 登录状态示例
├── references/
│   ├── architecture.md        # 架构文档
│   └── api_reference.md       # API文档
├── state.json                 # 你的登录状态（用户数据）
└── tasks.json                 # 你的任务（用户数据）
```

### 常见问题

#### 返回 0 个结果
**原因**：登录状态过期  
**解决**：使用 Chrome 扩展重新提取 state.json

#### HTTP 403/429 错误
**原因**：被闲鱼限流  
**解决**：
- 等待 24 小时
- 使用代理：`--proxy "http://proxy:port"`
- 降低频率到 30 分钟以上

#### 找不到 Playwright
```bash
pip install playwright
playwright install chromium
```

### 致谢

- 原项目：[ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) by Usagi-org
- 用于提取登录状态的 Chrome 扩展来自原项目
- 为 [OpenClaw](https://github.com/openclaw/openclaw) AI 助手平台构建

### 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

---

**Disclaimer**: This tool is for educational purposes only. Please comply with Xianyu's Terms of Service and use responsibly.

**免责声明**：本工具仅供学习交流使用，请遵守闲鱼服务条款，合理使用。
