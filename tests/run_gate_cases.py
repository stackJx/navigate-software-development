#!/usr/bin/env python3
"""用 `claude -p` headless 模式自动回归 gate-cases.md 中可机器判定的门禁用例。

每条用例在一次性 fixture 仓库中执行：把 skill 安装到 `.claude/skills/`，
以 `/navigate-software-development <输入>` 调用，解析 stream-json 事件与运行后的文件状态，断言：
  - 首个写操作之前是否已按顺序输出三个二级标题；
  - 纯方案/纯问答用例是否没有任何写操作；
  - 知识沉淀是否优先写入已有约定文件；
  - 方案篇幅是否符合预期分档（启发式软指标，标 [软]，不计入失败）。
API 报错、超时等运行故障标 [错误]，不计入通过率分母。

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
import re
import shutil
import subprocess
import tempfile
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = "navigate-software-development"
RUNS_DIR = SKILL_ROOT / "tests" / ".runs"

HEADINGS = ["## 需求分析", "## 开发思路", "## 验收标准"]
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# 自动记忆等 harness 自身文件写在这里，不属于项目改动。
HARNESS_DIR = str(Path.home() / ".claude")
# 与 SKILL.md 的只读边界一致：跑测试、构建、装依赖和拿不准的命令都视为写操作。
WRITE_CMD = re.compile(
    r"(?<![\w./-])("
    r"git\s+(-[Cc]\s+\S+\s+)*(commit|checkout|switch|add|rm|mv|push|pull|fetch|merge|rebase|reset"
    r"|restore|apply|init|cherry-pick|revert|clean|stash(?!\s+(list|show)\b))"
    r"|git\s+(-[Cc]\s+\S+\s+)*(branch|tag)\s+(?!(-l|--list|-a|-r|-v|-vv|--show-current|--contains)\b)\S+"
    r"|rm|mv|cp|ln|touch|mkdir|chmod|tee|sed\s+(-\w+\s+)*-i\S*|perl\s+-\w*i\S*"
    r"|npm\s+(?!(view|info|show|ls|list|outdated|search|-v|--version)\b)\S+|npx|pnpm|yarn"
    r"|pip3?\s+(?!(show|list|freeze|index)\b)\S+"
    r"|python3?\s+(?!(--version|-V)\b)\S+|node\s+(?!(-v|--version)\b)\S+|pytest|make"
    r"|prettier|black|ruff"
    r")(?=\s|$)"
)
# 先去掉 heredoc 正文和引号内容，避免把 grep "a > b" 之类的只读命令误判为写操作。
HEREDOC_BODY = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n).*?^\s*\2\s*$", re.S | re.M)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
REDIRECT = re.compile(r"(?<![<>&\d])\d*>>?\s*(?!&)([^\s;&|)]+)")
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


def is_write_command(cmd: str) -> bool:
    bare = QUOTED.sub("''", HEREDOC_BODY.sub(r"\1", cmd))
    if WRITE_CMD.search(bare):
        return True
    return any(target != "/dev/null" for target in REDIRECT.findall(bare))


def is_write(block: dict) -> bool:
    name = block.get("name")
    if name in WRITE_TOOLS:
        tool_input = block.get("input", {})
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return not path.startswith(HARNESS_DIR)
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


def headings_in_order(text: str) -> bool:
    pos = -1
    for h in HEADINGS:
        # 允许标题后跟括号注释（如“（按用户要求简写）”），但必须是行首的二级标题。
        m = re.search(r"^" + re.escape(h) + r"(?=\s|（|\(|$)", text[pos + 1:], re.M)
        if not m:
            return False
        pos += 1 + m.start()
    return True


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
        if headings_in_order(result["all"]):
            fails.append("纯问答不应输出方案三要素")
        if any(not w.startswith("Bash:") for w in writes):
            fails.append("纯问答修改了文件")
        elif writes:
            fails.append("[软] 纯问答执行了可能产生文件的命令")
        return fails

    plan_text = result["before"] if writes else result["all"]
    if not headings_in_order(plan_text):
        fails.append("首个写操作前未按顺序输出三个二级标题")
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


def run_once(case_id: str, idx: int, args, log_dir: Path) -> tuple:
    case = CASES[case_id]
    with tempfile.TemporaryDirectory(prefix=f"gate-{case_id}-") as tmp:
        root = Path(tmp)
        make_fixture(root, args.skill_root, case.files)
        cmd = ["claude", "-p", f"/{SKILL_NAME} {case.prompt}", "--output-format", "stream-json",
               "--verbose", "--no-session-persistence", "--permission-mode", "acceptEdits",
               "--max-turns", str(args.max_turns)]
        if args.model:
            cmd += ["--model", args.model]
        cmd += ["--allowedTools", *TEST_TOOLS]
        try:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return case_id, [f"[错误] 超时（>{args.timeout}s）"], 0.0
        # 临时目录清理前记录实际改动，用于纯方案零改动与知识沉淀位置的判定。
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=root, capture_output=True, text=True).stdout
    changed = sorted(line[3:].strip() for line in status.splitlines())
    (log_dir / f"{case_id}-{idx}.jsonl").write_text(proc.stdout, encoding="utf-8")
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if not events:
        return case_id, ["[错误] claude 调用失败：" + proc.stderr.strip()[:200]], 0.0
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    cost = final.get("total_cost_usd") or 0.0
    if final.get("is_error") and "API Error" in str(final.get("result", "")):
        return case_id, [f"[错误] {str(final.get('result'))[:120]}"], cost
    result = {**analyze(events), "changed": changed}
    fails = judge(case, result)
    summary = {"case": case_id, "fails": fails, "writes": result["writes"][:10], "changed": changed,
               "plan_length": plan_length(result["before"] if result["writes"] else result["all"]),
               "result": final.get("subtype"), "cost_usd": cost}
    (log_dir / f"{case_id}-{idx}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return case_id, fails, cost


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--cases", nargs="*", default=list(CASES))
    parser.add_argument("-r", "--runs", type=int, default=1)
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--model")
    parser.add_argument("--skill-root", type=Path, default=SKILL_ROOT,
                        help="被测 skill 目录（含 SKILL.md 与 references/），默认当前仓库")
    parser.add_argument("--label", default="", help="日志目录后缀，便于区分多次对比运行")
    args = parser.parse_args()

    unknown = [c for c in args.cases if c not in CASES]
    if unknown:
        parser.error(f"未知用例：{unknown}")
    args.skill_root = args.skill_root.resolve()
    if not (args.skill_root / "SKILL.md").is_file():
        parser.error(f"{args.skill_root} 下没有 SKILL.md")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = RUNS_DIR / (f"{stamp}-{args.label}" if args.label else stamp)
    log_dir.mkdir(parents=True)

    jobs = [(c, i) for c in args.cases for i in range(args.runs)]
    per_case = {c: [] for c in args.cases}
    total_cost = 0.0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_once, c, i, args, log_dir) for c, i in jobs]
        for fut in futures:
            case_id, fails, cost = fut.result()
            per_case[case_id].append(fails)
            total_cost += cost

    not_passed = run_passed = run_valid = errors = 0
    print(f"\n被测 skill：{args.skill_root}\n日志目录：{log_dir}\n")
    print("| 用例 | 结果 | 通过次数 | 失败原因 |\n| --- | --- | --- | --- |")
    for case_id in args.cases:
        runs = per_case[case_id]
        valid = [f for f in runs if not any(x.startswith("[错误]") for x in f)]
        passed = sum(1 for f in valid if all(x.startswith("[软]") for x in f))
        run_passed += passed
        run_valid += len(valid)
        errors += len(runs) - len(valid)
        verdict = "ERROR" if not valid else "PASS" if passed * 2 > len(valid) else "FAIL"
        not_passed += verdict != "PASS"
        reasons = Counter(x for f in runs for x in f)
        print(f"| {case_id} | {verdict} | {passed}/{len(valid)} | "
              f"{'；'.join(f'{k}×{v}' for k, v in reasons.items()) or '-'} |")
    print(f"\n未通过用例：{not_passed}/{len(args.cases)}；单次运行硬性通过：{run_passed}/{run_valid}"
          f"（另有 {errors} 次运行出错）；总花费约 ${total_cost:.2f}")
    return 1 if not_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
