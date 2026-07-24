# Research Report · 自定义 Web UI

一个单文件 HTML 前端 + 独立 Python 后端服务，通过 subprocess 调用 `hermes chat`，以 **SSE 流式**实时推送 Agent 进度给浏览器。

<img width="1328" height="1007" alt="截图 2026-07-24 15-25-48" src="https://github.com/user-attachments/assets/f4ee0347-5003-4ed0-880e-f881caf34a2c" />

## 文件

| 文件 | 说明 |
|------|------|
| `index.html` | 前端（单文件，含全部 HTML/CSS/JS，用 EventSource 接 SSE 流） |
| `server.py` | 独立后端（aiohttp，subprocess 调 `hermes chat`，SSE 推进度） |
| `preview.png` | 视觉预览截图 |

## 架构

```
浏览器 (index.html)
   │
   │  POST /api/run   {query, profile, instructions}   → 启动任务，返回 task_id
   │  GET  /api/stream/{task_id}                        → SSE 流（实时进度 + 最终结果）
   │  POST /api/stop/{task_id}                          → 终止任务
   ▼
server.py (独立 Python 服务, 127.0.0.1:8649)
   │
   │  subprocess: hermes -p hermes-research-report-agent chat --yolo -q "..."
   │  逐行读 stdout（工具调用进度、diff、最终回复）
   ▼
Hermes Agent (hermes-research-report-agent profile)
   │  --yolo 跳过所有命令审批（生成 PDF 全程自动）
   │  执行 7 阶段工作流（采集→打分→PDF）
```

## 快速开始

### 将 [hermes-research-report-agent](https://github.com/Samge0/hermes-research-report-agent) 安装到hermes
```
hermes profile install github.com/Samge0/hermes-research-report-agent --alias
```

### 配置指定环境变量
```bash
cp .env.example .env
```

### 第一步：启动后端服务

```bash
# 1) 进入本项目目录（即存放 server.py / index.html 的目录）
cd /path/to/custom-research-agent

# 2) 用 hermes 自带的 venv 启动后端（该 venv 已含 aiohttp 依赖）
~/.hermes/hermes-agent/venv/bin/python3 server.py

如果是windows系统:
C:/Users/<username>/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe server.py

#    若 hermes 安装在非默认位置，用环境变量覆盖可执行文件路径：
#    HERMES_BIN=/path/to/hermes ~/.hermes/hermes-agent/venv/bin/python3 server.py
```

看到如下输出即成功：
```
┌─────────────────────────────────────────────┐
│  Research Report 独立服务                        │
│  访问: http://127.0.0.1:8649               │
│  Hermes: ~/.hermes/hermes-agent/venv/bin/hermes （可用 HERMES_BIN 环境变量覆盖）
│  Profile: hermes-research-report-agent                        │
│  Ctrl+C 退出                                 │
└─────────────────────────────────────────────┘
```

自定义端口：`server.py 8650`（默认 8649）。

### 第二步：浏览器访问

**直接访问服务地址**（服务自带 `GET /` 返回 index.html，同源无 CORS）：

```
http://127.0.0.1:8649
```

### 第三步：使用

- ⚙️ 设置里默认已配好（服务地址 8649、profile `hermes-research-report-agent`），通常无需修改
- 底部输入主题，例如：`做一份关于「人形机器人行业」的研究报告，全球视角，输出 PDF`
- 按 Enter 发送，右侧实时显示 Agent 工具调用进度，完成后渲染最终报告

## 流式体验说明

本方案是**进度流式**（不是 token 流式）：
- ✅ **实时可见**：Agent 每次工具调用（搜索、读文件、写文件、生成 PDF）都会即时推送到浏览器，长任务不再"干等"
- ✅ **diff 预览**：Agent 写文件时能看到 diff 内容
- ⚠️ **最终回复一次性出现**：Hermes CLI 内核不外暴 token 级流式，最终文本在 Agent 跑完后整段渲染（而非逐字）

这是 Hermes CLI 的固有限制——真正的 token 流式只在底层 LLM adapter 内部，不暴露给外部进程。进度流式已能很好覆盖"实时感知 Agent 在做什么"的需求。

## 功能特性

- **SSE 实时进度**：Agent 工具调用、diff、思考过程实时推送，带图标分类（🔧 工具 / 📝 diff / 💬 文本）
- **Markdown 渲染**：最终报告自动渲染（含表格、代码块、列表）
- **PDF 链接识别**：Agent 返回的本地 `/abs/path.pdf` 路径自动变成可点击卡片
- **任务停止**：执行中可点 ⏹ 终止当前 Agent
- **免审批**：subprocess 用 `--yolo`，生成 PDF 的 shell 命令全程自动执行，无需人工批准
- **免交互**：默认开启，把 clarify 的 5 要素写进 prompt，跳过交互问答
- **任务历史**：左侧栏列出最近 30 个任务（存本地），点击可查看摘要
- **设置持久化**：配置优先存 localStorage；不可用时回退到内存

## 已知限制

1. **需先启动 server.py**：服务常驻运行，浏览器才能访问。
2. **任务历史是摘要式**：本地只存「查询 + 回复前 100 字预览」，不存完整多轮。
3. **文件上传不支持**：如需 Agent 处理本地文件，把文件路径写进 prompt。
4. **并发限制**：每个任务启动一个 hermes 子进程，同时跑多个长任务会消耗较多资源。

## 自定义

- **改默认 profile**：改 `index.html` 的 `defaultCfg.profile`，或运行时在 ⚙️ 设置里改
- **改配色**：编辑 `index.html` `:root` 里的 CSS 变量
- **加快捷指令**：在欢迎屏 `.examples` 区加 `.ex-card`
- **改 hermes 命令参数**：编辑 `server.py` 的 `run_hermes` 函数里的 `cmd` 列表（如加 `--max-turns`、改 model 等）
- **用其他 profile**：⚙️ 设置里改 Agent Profile（如 `default`）

## 其他截图

<img width="1355" height="845" alt="image" src="https://github.com/user-attachments/assets/eaab7efe-8e1d-406d-8ea0-693c953846e0" />

<img width="1328" height="1007" alt="截图 2026-07-24 15-48-14" src="https://github.com/user-attachments/assets/b33c998f-2ba9-46b9-81b0-e69ee273e9ed" />

<img width="1328" height="1007" alt="截图 2026-07-24 15-48-54" src="https://github.com/user-attachments/assets/5c8a0723-5486-4b8b-85ed-9963d9513254" />

<img width="1328" height="1007" alt="截图 2026-07-24 15-51-58" src="https://github.com/user-attachments/assets/a6a2139e-13de-4bff-aa0f-3af00d2fc9ef" />
