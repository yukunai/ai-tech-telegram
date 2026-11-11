# AI / 科技 Telegram 资讯机器人

收集 AI 网页资讯与 GitHub 项目，按批次配额发布中文摘要。

## 安装

需要 Python 3。实际发布需要 Telegram bot、目标 chat ID 和 DeepSeek API Key。

克隆仓库后，在项目根目录执行以下步骤。

```sh
python3 --version
```

## 使用

在环境中设置 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID、DEEPSEEK_API_KEY 和可写的 AI_TECH_STATE_DIR。先运行 python3 ai_tech_telegram.py scan 检查采集，再运行 python3 ai_tech_telegram.py publish 向测试频道发布。验证后可配置定时任务。

## 检查

```sh
python -m py_compile ai_tech_telegram.py
```

## 说明

仓库包含源码与启动所需的示例。模型、平台或推送服务的凭据由使用者在本机配置。
