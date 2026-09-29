#!/usr/bin/env python3
"""用 `claude -p` headless 模式自动回归 gate-cases.md 中可机器判定的门禁用例。

每条用例在一次性 fixture 仓库中执行：把 skill 安装到 `.claude/skills/`，
以 `/navigate-software-development <输入>` 调用，解析 stream-json 事件与运行后的文件状态，断言：
  - 首个写操作之前是否已按顺序输出三个二级标题；
  - 纯方案/纯问答用例是否没有任何写操作；
  - 知识沉淀是否优先写入已有约定文件；
  - 方案篇幅是否符合预期分档（启发式软指标，标 [软]，不计入失败）。
API 报错、超时等运行故障标 [错误]，停止派发未开始的请求并标 SKIPPED。
默认串行；重复运行混有错误/跳过时为 INCOMPLETE，不会因只统计有效运行而 PASS。
shell 判定仅是保守的只读白名单，复杂语法/未知命令按写处理，不能替代沙箱。
实现用例还检查目标文件有变更，但不验证业务实现是否正确；语义仍需人工抽查。

DDD 深度、模块选用理由、协调模式等语义判定仍需人工抽查，见 gate-cases.md。

用法：
  python3 tests/run_gate_cases.py                  # 全部用例各跑 1 次
  python3 tests/run_gate_cases.py -c A1 B3 -r 3    # 指定用例，各跑 3 次按多数判定
  # 对比旧版本：先导出旧提交，再用 --skill-root 指向它
  git archive <commit> | tar -x -C /tmp/nsd-baseline
  python3 tests/run_gate_cases.py --skill-root /tmp/nsd-baseline --label baseline
"""
import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from collections import Counter, namedtuple
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = "navigate-software-development"
RUNS_DIR = SKILL_ROOT / "tests" / ".runs"

HEADINGS = ["## 需求分析", "## 开发思路", "## 验收标准"]
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# 只忽略明确的自动记忆目录，设置和 skill 的改动仍计入写操作。
HARNESS_PROJECTS = Path.home() / ".claude" / "projects"
READ_COMMANDS = {"cat", "head", "tail", "ls", "pwd", "cd", "wc", "du", "stat", "file", "readlink",
                 "realpath", "basename", "dirname", "echo", "printf", "which", "type", "test", "[", "true", "false"}
READ_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "grep", "blame",
            "shortlog", "describe", "show-ref", "for-each-ref", "check-ignore", "check-attr"}
# 放行测试命令，让实现类用例能走到验证与知识沉淀；它们在方案前运行仍会被判为写操作。
TEST_TOOLS = ["Bash(python3 -m unittest:*)", "Bash(python -m unittest:*)",
              "Bash(python3 -m pytest:*)", "Bash(pytest:*)"]
SHORT_PLAN_MAX = 500   # 极简档方案上限（字符）
FULL_PLAN_MIN = 1200   # 完整档方案下限（字符）

FIXTURE = {
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    "README.md":"# 小店\n\n订单模块属于订单限届上下文。\n",
    "shop/__init__.py": "",
    "shop/coupon.py": (
        "def can_use(total, threshold=100):\n"
        '    """满减券是否可用。"""\n'
        "    return total > threshold\n"
    ),
    "shop/order.py": (
        "def submit(cart):\n"
        "    tmp = {'items': list(cart), 'status': 'pending'}\n"
        "    return tmp\n"
    ),
    "shop/catalog.py": (
        "PRODUCTS = [{'id': 1, 'name': '苹果', 'category': 'fruit'}]\n\n"
        "def list_products():\n"
        "    return PRODUCTS\n"
    ),
    "tests/__init__.py": "",
    "tests/test_coupon.py": (
        "import unittest\n\n"
        "from shop.coupon import can_use\n\n\n"
        "class CouponTest(unittest.TestCase):\n"
        "    def test_above_threshold(self):\n"
        "        self.assertTrue(can_use(120))\n"
    ),
    "config/http.yaml": "timeout_seconds: 30\n",
    "config/app.yaml": "log_level: info\nretries: 3\n",
    "package.json": '{\n  "name": "shop-web",\n  "dependencies": {"lodash": "4.17.20"}\n}\n',
}
PROJECT_CLAUDE_MD = "# 项目约定\n\n- 运行测试：`python3 -m unittest`\n"

# expect: plan_only=必须出方案且零写操作；implement=方案先于首个写操作；qa=不出方案且零写操作
# tier: short=极简档 / not_short=至少标准档 / full=完整档 / None=不检查篇幅
# kb: existing=已有约定文件时不得新建 KNOWLEDGE.md / create=无约定文件时应新建（软） / None=不检查
# files: 在基础 fixture 上追加或覆盖的文件
Case = namedtuple("Case", "prompt expect tier kb files", defaults=(None, None, None))
COUPON_BUG = "满 100 减 20 的券，订单正好 100 元时用不了。修一下。"
CASES = {
    "A1": Case("给商品列表加按分类筛选，只要需求和开发思路，先不写代码。", "plan_only"),
    "A2": Case(COUPON_BUG, "implement", "not_short", "create"),
    "A4": Case("为订阅计费业务梳理统一语言、限界上下文和聚合，技术栈未知。", "plan_only", "full"),
    "A5": Case("我们订单服务和库存服务对账老是不一致，帮我理一下。", "plan_only", "full"),
    "B1": Case("README 里的‘限届上下文’写错了，改成‘限界上下文’。", "implement", "short"),
    "B2": Case("把 shop/order.py 里的 tmp 变量改名为 pending_order。", "implement", "short"),
    "B3": Case("把接口超时时间从 30 秒改成 60 秒。", "implement", "not_short"),
    "B4": Case("把日志级别从 info 调成 debug，顺便把重试次数改成 5。", "implement", "not_short"),
    "B5": Case("升级一下 lodash 版本。", "implement", "not_short"),
    "B6": Case("订单正好 100 元时满减券用不了，修一下。", "implement"),
    "E1": Case("shop/coupon.py 里的 can_use 是干什么的？", "qa"),
    "E3": Case("解释一下什么是限界上下文。", "qa"),
    "G6": Case(COUPON_BUG, "implement", "not_short", "existing", {"CLAUDE.md": PROJECT_CLAUDE_MD}),
    "I1": Case("直接改，不用写方案：README 里的‘限届上下文’改成‘限界上下文’。", "implement", "short"),
    "I2": Case("满 100 减 20 的券正好 100 元用不了，直接改就行别啰嗦。", "implement"),
    "I3": Case("给商品加一个 nickname 字段并能在列表里返回，出一份详细方案，先不写代码。", "plan_only", "full"),
}
TARGETS = {
    "A2": "shop/coupon.py", "B1": "README.md", "B2": "shop/order.py", "B3": "config/http.yaml",
    "B4": "config/app.yaml", "B5": "package.json", "B6": "shop/coupon.py", "G6": "shop/coupon.py",
    "I1": "README.md", "I2": "shop/coupon.py",
}


def make_fixture(root: Path, skill_root: Path, extra: dict) -> None:
    for rel, content in {**FIXTURE, **(extra or {})}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    skill_dir = root / ".claude" / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True)
    shutil.copy(skill_root / "SKILL.md", skill_dir)
    shutil.copytree(skill_root / "references", skill_dir / "references")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True)


def is_read_command(words: list) -> bool:
    """只放行常见且可确认的查询；不是完整 shell/每种工具的参数解析器。"""
    if not words:
        return True
    name, *args = words
    if name in READ_COMMANDS:
        return True
    if name == "command":
        return bool(args) and args[0] in {"-v", "-V"}
    if name == "xargs":
        # 仅支持无 xargs 选项的显式命令；复杂替换/执行方式保守按写处理。
        return bool(args) and not args[0].startswith("-") and is_read_command(args)
    if name in {"rg", "grep"}:
        return not any(a.startswith(("--pre", "--hostname-bin")) for a in args)
    if name == "find":
        return not any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"} for a in args)
    if name == "sed":
        return (len(args) >= 2 and args[0] == "-n" and bool(re.fullmatch(r"\d+(?:,\d+)?p", args[1]))
                and not any(a.startswith("-") for a in args[2:]))
    if name in {"python", "python3", "node", "npm", "pnpm", "yarn", "git"} and args in (["--version"], ["-V"], ["-v"]):
        return True
    if name == "npm":
        return bool(args) and args[0] in {"view", "info", "show", "ls", "list", "outdated", "search"}
    if name != "git":
        return False
    while args and args[0] == "-C" and len(args) >= 2:
        args = args[2:]
    if not args or any(a.startswith(("--output", "--open-files-in-pager", "-O")) or a in {"--ext-diff", "--textconv"} for a in args):
        return False
    sub, *options = args
    if sub in READ_GIT:
        return True
    if sub in {"branch", "tag"}:
        if not options:
            return True
        query_flags = {"--list", "-l", "--show-current", "-a", "-r", "-v", "-vv", "--contains"}
        if any(a.startswith("-") and a not in query_flags for a in options):
            return False
        return all(a in query_flags for a in options) or options[0] in {"--list", "-l", "--contains"}
    return sub == "stash" and options in (["list"], ["show"])


def strip_shell_comments(cmd: str) -> str:
    """只去掉未引用、位于词首的注释，保留换行交给 shlex 识别命令边界。"""
    out, quote, word_start, i = [], None, True, 0
    while i < len(cmd):
        char = cmd[i]
        if char == "\\" and quote != "'" and i + 1 < len(cmd):
            out.extend(cmd[i:i + 2])
            if cmd[i + 1] != "\n":
                word_start = False
            i += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote, word_start = char, False
        elif char == "#" and word_start:
            end = cmd.find("\n", i)
            i = len(cmd) if end < 0 else end
            continue
        else:
            word_start = char.isspace() or char in "|&;<>()"
        out.append(char)
        i += 1
    return "".join(out)


def is_write_command(cmd: str) -> bool:
    cmd = strip_shell_comments(cmd)
    # 引号内命令替换也可能写文件；heredoc、分组等复杂语法统一保守处理。
    if re.search(r"\$\(|`", re.sub(r"'[^']*'", "", cmd)):
        return True
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars="|&;<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return True
    words, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        # shlex 会把相邻的控制符和换行合成如 "&&\n"；保留命令边界。
        if token and set(token) <= set("|&;<>()\n"):
            token = token.strip("\n") or "\n"
        if token in {">", ">>", "<", ">&", "<&"}:
            if i + 1 == len(tokens):
                return True
            target = tokens[i + 1]
            if (token in {">", ">>"} and target != "/dev/null") or (token in {">&", "<&"} and not re.fullmatch(r"\d+|-", target)):
                return True
            if words and words[-1].isdigit():
                words.pop()
            i += 2
            continue
        if token in {"|", "||", "&&"} or set(token) <= {";", "\n"}:
            if not is_read_command(words):
                return True
            words = []
        elif token and set(token) <= set("|&;<>()\n"):
            # 未支持的 shell 操作符（如 &>、>|、heredoc、分组）不可默认为只读。
            return True
        else:
            words.append(token)
        i += 1
    return not is_read_command(words)


def is_write(block: dict) -> bool:
    name = block.get("name")
    if name in WRITE_TOOLS:
        tool_input = block.get("input", {})
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        try:
            parts = Path(path).resolve().relative_to(HARNESS_PROJECTS.resolve()).parts
            return not (len(parts) >= 3 and parts[1] == "memory")
        except (ValueError, OSError):
            return True
    if name == "Bash":
        return is_write_command(block.get("input", {}).get("command", ""))
    return False


def analyze(events: list) -> dict:
    """返回首个写操作前的文本、写操作列表与全部面向用户的文本。"""
    before, all_text, writes = [], [], []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        # 子代理的文本不直接展示给用户，但它的写操作同样计入。
        sidechain = ev.get("parent_tool_use_id") is not None
        for block in ev["message"].get("content", []):
            if block.get("type") == "text" and not sidechain:
                all_text.append(block["text"])
                if not writes:
                    before.append(block["text"])
            elif block.get("type") == "tool_use" and is_write(block):
                detail = json.dumps(block.get("input"), ensure_ascii=False)[:120]
                writes.append(f"{block.get('name')}:{detail}")
    return {"before": "\n".join(before), "all": "\n".join(all_text), "writes": writes}


def visible_prose(text: str) -> str:
    lines, fence = [], None
    for line in text.splitlines():
        if fence is not None:
            closing = re.fullmatch(r" {0,3}(`{3,}|~{3,})[ \t]*", line)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= fence[1]:
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            continue
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines)


def headings_in_order(text: str) -> bool:
    text = visible_prose(text)
    sections = list(re.finditer(r"^## ([^\n]*)\n?", text, re.M))
    next_heading = 0
    for i, section in enumerate(sections):
        if not re.fullmatch(re.escape(HEADINGS[next_heading]) + r"(?:\s*|[（(].*)", section[0].rstrip()):
            continue
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        if not text[section.end():end].strip():
            return False
        next_heading += 1
        if next_heading == len(HEADINGS):
            return True
    return False


def plan_length(text: str) -> int:
    start = text.find(HEADINGS[0])
    if start < 0:
        return 0
    # 方案到下一个非三要素的二级标题为止，避免把最终汇报算进去。
    body = text[start:]
    nxt = [m.start() for m in re.finditer(r"^## ", body, re.M)
           if not any(body.startswith(h, m.start()) for h in HEADINGS)]
    return len(body[: nxt[0]] if nxt else body)


def judge(case: Case, result: dict) -> list:
    fails = []
    writes, changed = result["writes"], result["changed"]
    if case.expect == "qa":
        if any(re.search(r"^" + re.escape(h) + r"(?=\s|（|\(|$)", visible_prose(result["all"]), re.M) for h in HEADINGS):
            fails.append("纯问答不应套用方案标题")
        if writes or changed:
            fails.append("纯问答出现写操作或文件改动")
        return fails

    plan_text = result["before"] if writes else result["all"]
    if not headings_in_order(plan_text):
        fails.append("首个写操作前未按顺序输出三个二级标题及非空内容")
    if case.expect == "implement":
        if not writes:
            fails.append("实现任务未观测到写操作，无法证明写入顺序")
        target = next((TARGETS[c] for c, candidate in CASES.items() if candidate == case and c in TARGETS), None)
        if target and target not in changed:
            fails.append("实现任务未改动目标文件：" + target)
    if case.expect == "plan_only":
        if writes:
            fails.append("纯方案任务出现写操作")
        if changed:
            fails.append("纯方案任务改动了文件：" + ", ".join(changed[:3]))

    length = plan_length(plan_text)
    if case.tier == "short" and length > SHORT_PLAN_MAX:
        fails.append(f"[软] 极简档方案过长（{length} 字符）")
    if case.tier == "not_short" and 0 < length <= SHORT_PLAN_MAX:
        fails.append(f"[软] 不应按极简档简写（{length} 字符）")
    if case.tier == "full" and 0 < length < FULL_PLAN_MIN:
        fails.append(f"[软] 完整档方案过短（{length} 字符）")

    if case.kb == "existing":
        if "KNOWLEDGE.md" in changed:
            fails.append("已有 CLAUDE.md 时仍新建了 KNOWLEDGE.md")
        elif "CLAUDE.md" not in changed:
            fails.append("[软] 未写入已有 CLAUDE.md（可能判定无需沉淀）")
    if case.kb == "create" and "KNOWLEDGE.md" not in changed:
        fails.append("[软] 未新建 KNOWLEDGE.md（可能判定无需沉淀）")
    return fails


def as_text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def run_status(fails: list) -> str:
    if any(f.startswith("[错误]") for f in fails):
        return "ERROR"
    if any(f.startswith("[跳过]") for f in fails):
        return "SKIPPED"
    return "FAIL" if any(not f.startswith("[软]") for f in fails) else "PASS"


def save_logs(log_dir: Path, case_id: str, idx: int, stdout: str, stderr: str, summary: dict) -> None:
    prefix = log_dir / f"{case_id}-{idx}"
    prefix.with_suffix(".jsonl").write_text(stdout, encoding="utf-8")
    prefix.with_suffix(".stderr.log").write_text(stderr, encoding="utf-8")
    prefix.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cli(cmd: list, root: Path, timeout: float) -> subprocess.CompletedProcess:
    """POSIX 独立进程组；清理同组后代，不覆盖主动 setsid 脱离组的程序。"""
    if os.name != "posix":
        raise OSError("模型回归目前仅支持 POSIX 进程组清理，未启动 CLI")
    with subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as proc:
        def kill_group():
            # 即使主 CLI 已退出，持有输出管道的后代也可能仍在运行，不能先检查 poll()。
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_group()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired as drain:
                # 脱离进程组的程序可能保留管道；限定收尾时间并保留已读取的输出。
                stdout, stderr = drain.stdout or exc.stdout, drain.stderr or exc.stderr
                proc.stdout.close()
                proc.stderr.close()
                proc.wait()
            exc.output, exc.stderr = stdout, stderr
            raise
        finally:
            kill_group()
            proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_once(case_id: str, idx: int, args, log_dir: Path) -> tuple:
    case = CASES[case_id]
    stdout, stderr, changed, errors, returncode = "", "", [], [], None
    with tempfile.TemporaryDirectory(prefix=f"gate-{case_id}-") as tmp:
        root = Path(tmp)
        try:
            make_fixture(root, args.skill_root, case.files)
            cmd = ["claude", "-p", f"/{SKILL_NAME} {case.prompt}", "--output-format", "stream-json",
                   "--verbose", "--no-session-persistence", "--permission-mode", "acceptEdits",
                   "--max-turns", str(args.max_turns), "--max-budget-usd", str(args.max_budget_usd)]
            if args.model:
                cmd += ["--model", args.model]
            cmd += ["--allowedTools", *TEST_TOOLS]
            proc = run_cli(cmd, root, args.timeout)
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = as_text(exc.stdout), as_text(exc.stderr)
            errors.append(f"[错误] 超时（>{args.timeout}s）")
        except (OSError, subprocess.SubprocessError) as exc:
            stdout = as_text(getattr(exc, "stdout", ""))
            stderr = as_text(getattr(exc, "stderr", "")) or str(exc)
            errors.append(f"[错误] 执行失败：{str(exc)[:200]}")
        # 超时/CLI 失败也记录临时仓库的改动；查询失败时不能假定零改动。
        try:
            status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                    cwd=root, capture_output=True, text=True, timeout=30)
            if status.returncode:
                errors.append("[错误] 无法读取文件状态：" + status.stderr.strip()[:200])
            else:
                changed = sorted(line[3:].strip() for line in status.stdout.splitlines())
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"[错误] 无法读取文件状态：{str(exc)[:200]}")
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("事件不是 JSON 对象")
            events.append(event)
        except (json.JSONDecodeError, ValueError):
            errors.append("[错误] 无法解析完整的 stream-json 事件")
            break
    finals = [e for e in events if e.get("type") == "result"]
    final = finals[-1] if finals else {}
    cost = final.get("total_cost_usd")
    if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0):
        cost = None
        errors.append("[错误] 非法费用字段")
    if returncode not in (None, 0):
        errors.append(f"[错误] claude 退出码 {returncode}：{stderr.strip()[:200]}")
    if len(finals) != 1 or final.get("is_error") is not False or final.get("subtype") != "success":
        detail = final.get("result") or final.get("subtype") or stderr.strip() or "缺少完成事件"
        errors.append(f"[错误] 完成结果无效：{str(detail)[:200]}")
    try:
        result = {**analyze(events), "changed": changed}
    except (KeyError, TypeError, AttributeError) as exc:
        errors.append(f"[错误] 事件结构无效：{exc}")
        result = {"before": "", "all": "", "writes": [], "changed": changed}
    fails = errors or judge(case, result)
    summary = {"case": case_id, "run": idx, "status": run_status(fails), "fails": fails,
               "writes": result["writes"][:10], "changed": changed, "returncode": returncode,
               "plan_length": plan_length(result["before"] if result["writes"] else result["all"]),
               "result": final.get("subtype"), "cost_usd": cost, "cost_known": cost is not None,
               "max_budget_usd": args.max_budget_usd}
    save_logs(log_dir, case_id, idx, stdout, stderr, summary)
    return case_id, fails, cost


def run_jobs(jobs: list, args, log_dir: Path) -> list:
    """限制在途数量，任何运行错误后停止提交；已在运行的请求仍可能产生费用。"""
    results, next_job, stopped = [], 0, False
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending = {}
        while pending or (not stopped and next_job < len(jobs)):
            while not stopped and next_job < len(jobs) and len(pending) < args.jobs:
                case_id, idx = jobs[next_job]
                pending[pool.submit(run_once, case_id, idx, args, log_dir)] = (case_id, idx)
                next_job += 1
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                results.append(result)
                stopped |= run_status(result[1]) == "ERROR"
                del pending[future]
    for case_id, idx in jobs[next_job:]:
        fails = ["[跳过] 前序运行错误，停止派发新请求"]
        save_logs(log_dir, case_id, idx, "", "", {"case": case_id, "run": idx, "status": "SKIPPED",
                                               "fails": fails, "cost_usd": 0.0, "cost_known": True})
        results.append((case_id, fails, 0.0))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--cases", nargs="*", default=list(CASES))
    parser.add_argument("-r", "--runs", type=int, default=1)
    parser.add_argument("-j", "--jobs", type=int, default=1, help="最大在途请求数（默认串行）")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-budget-usd", type=float, default=1.0,
                        help="每次调用的 CLI 估算花费上限（默认 $1；不保证第三方账单上限）")
    parser.add_argument("--model")
    parser.add_argument("--skill-root", type=Path, default=SKILL_ROOT,
                        help="被测 skill 目录（含 SKILL.md 与 references/），默认当前仓库")
    parser.add_argument("--label", default="", help="日志目录后缀，便于区分多次对比运行")
    args = parser.parse_args()

    if not args.cases or any(n < 1 for n in (args.runs, args.jobs, args.max_turns, args.timeout)):
        parser.error("用例列表不能为空，运行次数、并发数、轮次和超时必须为正数")
    if not re.fullmatch(r"[A-Za-z0-9_-]*", args.label):
        parser.error("label 仅允许字母、数字、下划线和连字符")
    if not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0:
        parser.error("max-budget-usd 必须为有限正数")
    if len(set(args.cases)) != len(args.cases):
        parser.error("用例列表不能重复")
    unknown = [c for c in args.cases if c not in CASES]
    if unknown:
        parser.error(f"未知用例：{unknown}")
    args.skill_root = args.skill_root.resolve()
    if not (args.skill_root / "SKILL.md").is_file() or not (args.skill_root / "references").is_dir():
        parser.error(f"{args.skill_root} 下必须有 SKILL.md 和 references/")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_dir = RUNS_DIR / (f"{stamp}-{args.label}" if args.label else stamp)
    log_dir.mkdir(parents=True)

    jobs = [(c, i) for c in args.cases for i in range(args.runs)]
    per_case = {c: [] for c in args.cases}
    total_cost = 0.0
    unknown_cost_runs = 0
    print(f"开始 {len(jobs)} 次运行，并发 {args.jobs}，每次 CLI 估算花费上限 ${args.max_budget_usd:g}；运行错误后停止派发。", flush=True)
    for case_id, fails, cost in run_jobs(jobs, args, log_dir):
        per_case[case_id].append(fails)
        total_cost += cost or 0.0
        unknown_cost_runs += cost is None

    not_passed = run_passed = run_valid = errors = skipped = 0
    case_summaries = {}
    print(f"\n被测 skill：{args.skill_root}\n日志目录：{log_dir}\n")
    print("| 用例 | 结果 | 通过次数 | 失败原因 |\n| --- | --- | --- | --- |")
    for case_id in args.cases:
        runs = per_case[case_id]
        valid = [f for f in runs if run_status(f) in {"PASS", "FAIL"}]
        passed = sum(run_status(f) == "PASS" for f in valid)
        case_errors = sum(run_status(f) == "ERROR" for f in runs)
        case_skipped = sum(run_status(f) == "SKIPPED" for f in runs)
        run_passed += passed
        run_valid += len(valid)
        errors += case_errors
        skipped += case_skipped
        if case_errors or case_skipped:
            verdict = "INCOMPLETE" if valid else "ERROR" if case_errors else "SKIPPED"
        else:
            verdict = "PASS" if passed * 2 > len(valid) else "FAIL"
        not_passed += verdict != "PASS"
        case_summaries[case_id] = {"status": verdict, "passed": passed, "valid": len(valid),
                                   "errors": case_errors, "skipped": case_skipped}
        reasons = Counter(x for f in runs for x in f)
        print(f"| {case_id} | {verdict} | {passed}/{len(valid)} | "
              f"{'；'.join(f'{k}×{v}' for k, v in reasons.items()) or '-'} |")
    print(f"\n未通过用例：{not_passed}/{len(args.cases)}；单次运行硬性通过：{run_passed}/{run_valid}"
          f"（另有 {errors} 次运行出错、{skipped} 次跳过）；已记录花费约 ${total_cost:.2f}")
    if unknown_cost_runs:
        print(f"另有 {unknown_cost_runs} 次运行费用未知，无法确认总花费。")
    (log_dir / "summary.json").write_text(json.dumps({"cases": case_summaries, "cost_usd": total_cost,
        "cost_known": unknown_cost_runs == 0, "unknown_cost_runs": unknown_cost_runs,
        "status": "INCOMPLETE" if errors or skipped else "FAIL" if not_passed else "PASS"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if not_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
