#!/usr/bin/env python3
"""
Research Report 独立后端服务
========================
独立 Python 服务，通过 subprocess 调用 hermes chat，以 SSE 流式推送进度给 HTML。
不依赖 hermes-web-ui，不修改任何 Hermes 文件。

用法：
    python3 server.py              # 默认 8649
    python3 server.py 8650         # 自定义端口

HTML 端点填 http://127.0.0.1:8649

API：
    POST /api/run   {query, profile?, instructions?}  → {task_id}
    GET  /api/stream/{task_id}                        → SSE 流（event: progress/done/error）
    POST /api/stop/{task_id}                          → {ok}
"""
import sys
import os
import re
import json
import time
import asyncio
import uuid
from aiohttp import web


def _load_env(path):
    """极简 .env 加载器：解析 KEY=VALUE 写入 os.environ（已存在的真实环境变量优先，不覆盖）。无第三方依赖。"""
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]  # 去掉两侧成对引号
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass
    except Exception:
        pass


# 启动时加载同目录 .env（若存在）；真实环境变量 / 命令行参数优先级仍最高
_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# hermes 可执行文件（.env 的 HERMES_BIN 或环境变量；默认 ~/.hermes 标准位置；~ 会展开）
HERMES_BIN = os.path.expanduser(os.environ.get("HERMES_BIN") or "~/.hermes/hermes-agent/venv/bin/hermes")
# 默认 Agent profile（.env 的 DEFAULT_PROFILE 或环境变量）
DEFAULT_PROFILE = os.environ.get("DEFAULT_PROFILE") or "hermes-research-report-agent"
# 报告产物目录（Agent 生成的 PDF/HTML/JSON 都写到这里，供 /report 端点浏览/预览）
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")
# 任务历史持久化（服务重启后侧栏仍能看到最近任务）
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks_state.json")

# 任务存储：task_id → { proc, queue, status, output, session_id, query, created_at, finished_at }
TASKS: dict[str, dict] = {}


def _task_meta(task_id: str, task: dict) -> dict:
    """提取可持久化的任务元信息（不含 queue/proc 等运行时对象）。"""
    return {
        "task_id": task_id,
        "query": task.get("query", ""),
        "status": task.get("status", "unknown"),
        "profile": task.get("profile", ""),
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
        "session_id": task.get("session_id", ""),
        "output": task.get("output", ""),
        "progress": task.get("progress", []),
    }


def _save_state():
    """把已终结的任务落盘（最近 50 条），供重启后侧栏展示。"""
    try:
        items = [_task_meta(t, task) for t, task in TASKS.items()
                 if task.get("status") in ("completed", "error", "cancelled")]
        items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        items = items[:50]
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _load_state():
    """启动时从磁盘恢复历史任务（标记为无活动队列）。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return
    for it in items:
        tid = it.get("task_id")
        if tid and tid not in TASKS:
            TASKS[tid] = {
                "queue": None, "proc": None,
                "status": it.get("status", "completed"),
                "output": it.get("output", ""),
                "session_id": it.get("session_id", ""),
                "query": it.get("query", ""),
                "created_at": it.get("created_at"),
                "finished_at": it.get("finished_at"),
                "progress": it.get("progress", []),
            }

# ANSI 转义序列清洗（颜色码、光标移动等）
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")
# spinner / 装饰行过滤（┊ 开头带动画的）
SPINNER_RE = re.compile(r"^[┊╭╰─│┃┆┈━]\s*[✍🔍💻📦📝🔧✨🎯📊]+\s*\r?$")


def clean_line(raw: str) -> str:
    """清洗一行 stdout：去 ANSI、去回车、去首尾空格、去 ┊ spinner 前缀。"""
    line = ANSI_RE.sub("", raw).strip()
    # 去掉 spinner 框前缀（┊ 开头的进度行）
    line = re.sub(r"^[┊│┃]\s*", "", line)
    return line


def is_meaningful(line: str, query: str = "", instructions: str = "") -> bool:
    """判断这行是否值得推给前端（过滤纯装饰、空行、banner、框线、query 回显）。"""
    if not line:
        return False
    # 过滤 banner / 元信息
    if line.startswith(("───", "Initializing agent", "Query:", "Resume this session")):
        return False
    if line.startswith(("Session:", "Duration:", "Messages:")):
        return False
    # 过滤最终回复框线（╭─ ╰─）
    if line.startswith(("╭─", "╰─")):
        return False
    # 过滤 hermes --resume 提示行
    if line.startswith("hermes --resume"):
        return False
    # 过滤 query 回显（hermes 把 query 原样打一遍，可能带 instructions 前缀）
    if query:
        q_snippet = query[:30].strip()
        if q_snippet and q_snippet in line:
            return False
    return True


def classify_line(line: str) -> str:
    """给进度行分类：tool（工具调用）、diff（代码变更）、text（思考/回复文本）。"""
    if any(k in line for k in ("✍️", "preparing", "🔍", "💻", "📦", "🔧", "write", "read", "search", "terminal")):
        return "tool"
    if line.startswith(("+", "-", "@@")) or "diff" in line.lower() or "→ b/" in line:
        return "diff"
    return "text"


def parse_final_response(lines: list[str]) -> str:
    """从完整 stdout 提取最终回复文本（Hermes 用 ╭─ ⚕ ── 框包裹最终回复）。"""
    in_box = False
    collected = []
    for line in lines:
        clean = clean_line(line)
        if clean.startswith("╭─"):
            in_box = True
            continue
        if in_box and clean.startswith("╰─"):
            in_box = False
            continue
        if in_box:
            collected.append(clean)
    return "\n".join(x for x in collected if x).strip()


def parse_session_id(lines: list[str]) -> str:
    """从 stdout 提取 session_id（Resume this session with: hermes --resume XXXX）。"""
    for line in lines:
        m = re.search(r"--resume\s+(\S+)", line)
        if m:
            return m.group(1)
    return ""


async def run_hermes(task_id: str, query: str, profile: str, instructions: str | None, resume_session_id: str | None = None):
    """启动 hermes chat 子进程，逐行读 stdout，记入任务历史。resume_session_id 非空时用 --resume 恢复会话。"""
    cmd = [HERMES_BIN, "-p", profile, "chat", "--yolo"]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += ["-q", query]
    # 注入额外指令（拼接进 query）；恢复会话时无需注入（会话已有上下文）
    if instructions and not resume_session_id:
        cmd[-1] = f"{instructions}\n\n{query}"

    all_lines: list[str] = []

    async def emit(data: dict):
        """记一条 progress 到任务历史；前端通过 /api/stream 游标轮询读取（支持刷新重连）。"""
        TASKS[task_id]["progress"].append(data)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "FORCE_COLOR": "0", "NO_COLOR": "1"},
        )
        TASKS[task_id]["proc"] = proc
        TASKS[task_id]["status"] = "running"

        await emit({"text": "🔄 已恢复会话，继续执行…" if resume_session_id else "🚀 已启动 Agent，正在思考…"})

        # 逐行读 stdout
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            all_lines.append(raw.decode("utf-8", errors="replace"))
            line = clean_line(raw.decode("utf-8", errors="replace"))
            if is_meaningful(line, query, instructions):
                kind = classify_line(line)
                # 工具调用行加图标
                if kind == "tool":
                    await emit({"text": line, "kind": "tool"})
                elif kind == "diff":
                    await emit({"text": line, "kind": "diff"})
                else:
                    await emit({"text": line, "kind": "text"})

        await proc.wait()

        # 进程结束，解析最终结果；状态供 /api/stream 轮询判定 done/error
        final_text = parse_final_response(all_lines)
        session_id = parse_session_id(all_lines)
        TASKS[task_id]["output"] = final_text
        if session_id:
            TASKS[task_id]["session_id"] = session_id  # 仅当解析到才更新，避免恢复运行清空原 session_id
        TASKS[task_id]["status"] = "completed" if proc.returncode == 0 else "error"

    except asyncio.CancelledError:
        TASKS[task_id]["status"] = "cancelled"
    except Exception as e:
        TASKS[task_id]["status"] = "error"
        TASKS[task_id]["output"] = f"❌ 服务异常: {e}"
    finally:
        # 即便失败也尝试捕获 session_id（hermes 在会话创建早期就打印），供"继续会话"恢复
        if not TASKS[task_id].get("session_id") and all_lines:
            TASKS[task_id]["session_id"] = parse_session_id(all_lines)
        TASKS[task_id]["finished_at"] = time.time()
        _save_state()


# ===== HTTP handlers =====

async def handle_index(request: web.Request):
    """返回 index.html（同源访问，彻底无 CORS 问题）。"""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    return web.FileResponse(html_path)


async def handle_run(request: web.Request):
    """POST /api/run — 启动 hermes 子进程。"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    query = (body.get("query") or "").strip()
    if not query:
        return web.json_response({"error": "query 不能为空"}, status=400)

    profile = body.get("profile") or DEFAULT_PROFILE
    instructions = body.get("instructions")

    task_id = uuid.uuid4().hex[:12]
    # 为本任务创建独立报告子目录 report/<task_id>/，便于在 /report/ 下按任务区分
    task_report_dir = os.path.join(REPORT_DIR, task_id)
    try:
        os.makedirs(task_report_dir, exist_ok=True)
    except Exception:
        pass
    # 注入指令：要求 Agent 把所有产物写入该子目录（task_id 即子目录名）
    instr_extra = f"所有报告产物（HTML / PDF / 数据 JSON 等）必须写入目录 {task_report_dir}/ 下，文件名自拟。"
    instructions = ((instructions + "\n" if instructions else "") + instr_extra).strip()

    TASKS[task_id] = {"status": "pending", "output": "", "session_id": "", "proc": None, "query": query, "profile": profile, "created_at": time.time(), "finished_at": None, "progress": []}

    # 启动后台任务
    asyncio.create_task(run_hermes(task_id, query, profile, instructions))

    return web.json_response({"task_id": task_id})


async def handle_stream(request: web.Request):
    """GET /api/stream/{task_id}?since=N — 游标轮询 progress 日志 + 任务状态，SSE 推送。
    后端任务在后端持续运行，本端点只是只读视图；支持刷新重连：since=已渲染条数，
    只推送后续新增的 progress，不重复、不丢失。"""
    task_id = request.match_info["task_id"]
    task = TASKS.get(task_id)
    if not task:
        return web.json_response({"error": "task not found"}, status=404)
    try:
        since = max(0, int(request.query.get("since", "0")))
    except ValueError:
        since = 0

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            # CORS 头必须在 prepare() 前写入——中间件在 handler 返回后才设头，
            # 而流式响应那时早已把头发出去，导致 SSE 接口缺 CORS 头报错。
            "Access-Control-Allow-Origin": request.headers.get("Origin", "*"),
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )
    await resp.prepare(request)

    async def sse(event: str, data: dict):
        await resp.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))

    cursor = since
    try:
        while True:
            prog = task.get("progress") or []
            while cursor < len(prog):
                await sse("progress", prog[cursor])
                cursor += 1
            status = task.get("status")
            if status == "completed":
                await sse("done", {"text": task.get("output", ""), "session_id": task.get("session_id", "")})
                break
            if status in ("error", "cancelled"):
                msg = "⏹ 已取消" if status == "cancelled" else (task.get("output") or "❌ Agent 执行出错")
                data = {"text": msg}
                sid = task.get("session_id") or ""
                if status == "error" and sid:
                    data["recoverable"] = True
                    data["session_id"] = sid
                await sse("error", data)
                break
            await asyncio.sleep(0.25)  # 未结束：轮询等待新进度
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def handle_stop(request: web.Request):
    """POST /api/stop/{task_id} — 终止任务。"""
    task_id = request.match_info["task_id"]
    task = TASKS.get(task_id)
    if not task:
        return web.json_response({"error": "task not found"}, status=404)
    proc = task.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    task["status"] = "cancelled"
    return web.json_response({"ok": True})


async def handle_resume(request: web.Request):
    """POST /api/resume/{task_id} — 失败后用 hermes --resume 恢复会话，继续未完成的任务。"""
    task_id = request.match_info["task_id"]
    task = TASKS.get(task_id)
    if not task:
        return web.json_response({"error": "task not found"}, status=404)
    sid = task.get("session_id") or ""
    if not sid:
        return web.json_response({"error": "no session_id to resume"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    prompt = (body.get("prompt") or "继续完成刚才未完成的任务，从上次中断的地方接着做。").strip()
    profile = task.get("profile") or DEFAULT_PROFILE
    # 复位为运行态：保留历史 progress（继续往里追加），清空旧的错误输出
    task["status"] = "running"
    task["output"] = ""
    task["proc"] = None
    task["finished_at"] = None
    since = len(task.get("progress") or [])  # 此刻进度长度，前端据此游标重连只取新增
    asyncio.create_task(run_hermes(task_id, prompt, profile, None, resume_session_id=sid))
    return web.json_response({"ok": True, "session_id": sid, "since": since})


async def handle_tasks(request: web.Request):
    """GET /api/tasks — 最近任务列表（侧栏用，按创建时间倒序，最多 50 条）。"""
    items = []
    for t, task in TASKS.items():
        it = _task_meta(t, task)
        it["query"] = (it.get("query") or "")[:80]
        it["has_output"] = bool(it.get("output"))
        it["has_progress"] = bool(it.get("progress"))
        it.pop("output", None)
        it.pop("progress", None)
        items.append(it)
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return web.json_response({"tasks": items[:50]})


async def handle_task_detail(request: web.Request):
    """GET /api/task/{task_id} — 单个任务详情（含完整输出，供点击回看）。"""
    task_id = request.match_info["task_id"]
    task = TASKS.get(task_id)
    if not task:
        return web.json_response({"error": "task not found"}, status=404)
    return web.json_response(_task_meta(task_id, task))


async def handle_health(request: web.Request):
    return web.json_response({"ok": True, "hermes_bin": os.path.exists(HERMES_BIN)})


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _dir_index_html(abs_dir: str, rel_dir: str, labels: dict | None = None) -> str:
    """生成报告目录的 HTML 索引：子目录（按任务）在前、文件在后，均可点击预览。"""
    labels = labels or {}
    try:
        names = sorted(os.listdir(abs_dir))
    except OSError:
        names = []
    subdirs = [n for n in names if os.path.isdir(os.path.join(abs_dir, n)) and not n.startswith(".")]
    files = [n for n in names if os.path.isfile(os.path.join(abs_dir, n))]
    prefix = (rel_dir.rstrip("/") + "/") if rel_dir else ""
    rows = []
    if rel_dir:  # 上一级目录
        parent_rel = os.path.dirname(rel_dir.rstrip("/"))
        parent = "/report/" + (parent_rel + "/" if parent_rel else "")
        rows.append('<li><a href="' + parent + '"><span class="fi">⬆️</span><span class="fn">..</span><span class="fs">上级目录</span></a></li>')
    for d in subdirs:
        label = labels.get(d)
        label_html = ('<span class="fl">' + _esc(label) + '</span>') if label else '<span class="fs">子目录</span>'
        rows.append('<li><a href="/report/' + prefix + d + '/" target="_blank"><span class="fi">📁</span><span class="fn">' + _esc(d) + '</span>' + label_html + '</a></li>')
    for name in files:
        lower = name.lower()
        icon = "📕" if lower.endswith(".pdf") else \
               "🌐" if lower.endswith((".html", ".htm")) else \
               "🧾" if lower.endswith(".json") else \
               "🖼️" if lower.endswith((".png", ".jpg", ".jpeg", ".svg", ".gif")) else "📄"
        size = os.path.getsize(os.path.join(abs_dir, name))
        size_str = f"{size/1024:.0f} KB" if size < 1024 * 1024 else f"{size/1024/1024:.1f} MB"
        rows.append(
            f'<li><a href="/report/{prefix}{name}" target="_blank">'
            f'<span class="fi">{icon}</span><span class="fn">{_esc(name)}</span>'
            f'<span class="fs">{size_str}</span></a></li>'
        )
    body = "\n".join(rows) if rows else '<li class="empty">暂无报告文件</li>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%235b8cff'/><stop offset='.5' stop-color='%238b5cf6'/><stop offset='1' stop-color='%2322d3ee'/></linearGradient></defs><rect width='64' height='64' rx='14' fill='url(%23g)'/><rect x='13' y='34' width='8' height='16' rx='2' fill='%23ffffff'/><rect x='28' y='24' width='8' height='26' rx='2' fill='%23ffffff'/><rect x='43' y='14' width='8' height='36' rx='2' fill='%23ffffff'/></svg>">
<title>报告目录 · Research Report</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#0a0e1a;color:#e6ecf5;margin:0;padding:36px}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font-size:19px;margin:0 0 6px;font-weight:700}}
.sub{{color:#8a94ad;font-size:12px;margin-bottom:20px;font-family:monospace}}
ul{{list-style:none;padding:0}}
li a{{display:flex;align-items:center;gap:14px;padding:13px 16px;border:1px solid rgba(99,118,200,.18);border-radius:11px;margin-bottom:8px;text-decoration:none;color:#e6ecf5;background:rgba(22,28,46,.6);transition:.15s}}
li a:hover{{border-color:#5b8cff;background:rgba(91,140,255,.12);transform:translateX(2px)}}
.fi{{font-size:24px}}.fn{{flex:1;word-break:break-all;color:#cdd6e6}}.fs{{color:#5a6480;font-size:11px;font-family:monospace;white-space:nowrap}}.fl{{color:#5b8cff;font-size:11px;background:rgba(91,140,255,.12);padding:2px 8px;border-radius:6px;white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis}}
li.empty{{color:#5a6480;padding:24px;text-align:center;list-style:none}}
.back{{display:inline-block;margin-top:18px;color:#5b8cff;text-decoration:none;font-size:12px}}
</style></head><body><div class="wrap">
<h1>📁 报告目录</h1><div class="sub">点击任意文件在浏览器中预览 · /{rel_dir}</div>
<ul>{body}</ul>
<a class="back" href="/">← 返回 Research Report</a>
</div></body></html>"""


async def handle_report(request: web.Request):
    """GET /report/{filename} — 浏览/预览报告产物（PDF 内联预览、HTML 渲染、目录索引）。"""
    rel = request.match_info.get("filename", "") or ""
    root = os.path.realpath(REPORT_DIR)
    target = os.path.realpath(os.path.join(root, rel)) if rel else root
    # 路径穿越防护：只允许访问 REPORT_DIR 内部
    if target != root and not target.startswith(root + os.sep):
        return web.json_response({"error": "forbidden"}, status=403)
    if os.path.isdir(target):
        labels = {tid: t.get("query", "") for tid, t in TASKS.items()}
        return web.Response(text=_dir_index_html(target, rel, labels), content_type="text/html", charset="utf-8")
    if not os.path.isfile(target):
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(target)


# CORS（以防用户从 file:// 访问）
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT") or "8649")
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/report", handle_report)
    app.router.add_get("/report/{filename:.*}", handle_report)
    app.router.add_post("/api/run", handle_run)
    app.router.add_get("/api/tasks", handle_tasks)
    app.router.add_get("/api/task/{task_id}", handle_task_detail)
    app.router.add_get("/api/stream/{task_id}", handle_stream)
    app.router.add_post("/api/stop/{task_id}", handle_stop)
    app.router.add_post("/api/resume/{task_id}", handle_resume)

    print(f"┌─────────────────────────────────────────────┐")
    print(f"│  Research Report 独立服务                        │")
    print(f"│  访问: http://127.0.0.1:{port}               │")
    print(f"│  Hermes: {HERMES_BIN}")
    print(f"│  Profile: {DEFAULT_PROFILE}                   │")
    print(f"│  Ctrl+C 退出                                 │")
    print(f"└─────────────────────────────────────────────┘")

    _load_state()  # 恢复历史任务到 TASKS，侧栏重启后仍可见
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
