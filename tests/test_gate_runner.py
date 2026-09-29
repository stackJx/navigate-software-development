"""离线校验门禁判定与 CLI 故障处理，不调用模型、不产生 API 费用。"""
import contextlib
from concurrent.futures import Future
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_gate_cases as gate

PLAN = "\n".join(h + "\n具体内容。" for h in gate.HEADINGS)


def assistant(*blocks, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": list(blocks)}}


def text(value=PLAN):
    return {"type": "text", "text": value}


def write(path="/tmp/project/README.md"):
    return {"type": "tool_use", "name": "Edit", "input": {"file_path": path}}


def stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def success():
    return {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.12}


class GateChecks(unittest.TestCase):
    def test_write_commands(self):
        commands = ("python3 -m unittest", "git add -A", "echo x > README.md", "make check",
                    "git branch feature", "git tag v1", "git diff --output=diff.txt", "sed -i '' s/a/b/ x",
                    "find . -delete", "find . -exec touch x \\;", "/bin/rm x", "unknown-command",
                    "rg x .\ntouch new", "ls && touch new", 'echo "$(touch x)"', "cat <<EOF\nx\nEOF",
                    'echo x > "a b.txt"', "echo x &>file", "echo x >|file", "cat <>file",
                    "rg --pre ./filter x .", "npm install", "node task.js")
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(gate.is_write_command(command))

    def test_read_commands(self):
        commands = ("git status", "git diff", 'grep "a > b" README.md', "ls 2>/dev/null",
                    "git branch", "git branch --show-current", "git tag --list v*", "git -C /tmp status",
                    "cat README.md | head -10", "git log -1 && git diff --stat", "rg x .\ncat README.md",
                    "sed -n '1,20p' README.md", "echo 'touch x'", "echo x 1>&2", "python3 --version")
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(gate.is_write_command(command))

    def test_executable_lookup_is_read_only(self):
        # 历史 A2 被误报的真实命令：python3 在这里是查询参数，不是执行命令。
        for command in ("command -v python3 && python3 --version", "which python3; python3 --version",
                        "type python3; git status", "which rm"):
            with self.subTest(command=command):
                self.assertFalse(gate.is_write_command(command))

    def test_multiline_control_operators_preserve_command_boundaries(self):
        for operator in ("&&", "||", "|"):
            with self.subTest(operator=operator):
                self.assertTrue(gate.is_write_command(f"git status {operator}\nrm README.md"))
                self.assertTrue(gate.is_write_command(f"pwd {operator}\ntee README.md"))
                self.assertFalse(gate.is_write_command(f"git status {operator}\ngit diff"))

    def test_directory_change_does_not_hide_following_command(self):
        self.assertFalse(gate.is_write_command("cd project && find . -type f"))
        self.assertFalse(gate.is_write_command("cd project; ls -la"))
        self.assertTrue(gate.is_write_command("cd project && touch y"))

    def test_xargs_checks_known_underlying_command(self):
        command = "find . -path ./.git -prune -o -path ./.claude -prune -o -type f -print | xargs wc -l"
        self.assertFalse(gate.is_write_command(command))
        for command in ("find . -type f | xargs rm", "find . -type f | xargs sh -c 'touch x'",
                        "find . -type f | xargs -I{} wc -l {}"):
            with self.subTest(command=command):
                self.assertTrue(gate.is_write_command(command))

    def test_read_command_with_write_option_is_not_read_only(self):
        for command in ("sed -n '1,20p' -i README.md", "git branch -v feature", "git tag --list -d v1",
                        "git grep --open-files-in-pager=touch x", "git diff --ext-diff"):
            with self.subTest(command=command):
                self.assertTrue(gate.is_write_command(command))

    def test_thinking_is_not_visible_plan(self):
        events = [assistant({"type": "thinking", "thinking": PLAN}, write(), text())]
        result = dict(gate.analyze(events), changed=["README.md"])
        self.assertFalse(gate.headings_in_order(result["before"]))
        self.assertTrue(gate.judge(gate.CASES["B1"], result))

    def test_child_plan_does_not_satisfy_main_gate_but_child_write_counts(self):
        events = [assistant(text(), write(), parent="task-1"), assistant(text())]
        result = dict(gate.analyze(events), changed=["README.md"])
        self.assertEqual(result["before"], "")
        self.assertEqual(len(result["writes"]), 1)
        self.assertTrue(gate.judge(gate.CASES["B1"], result))

    def test_visible_plan_before_child_write_passes(self):
        events = [assistant(text()), assistant(write(), parent="task-1")]
        result = dict(gate.analyze(events), changed=["README.md"])
        self.assertEqual(gate.judge(gate.CASES["B1"], result), [])

    def test_write_between_headings_fails(self):
        events = [assistant(text(gate.HEADINGS[0] + "\n内容"), write(), text(PLAN))]
        result = dict(gate.analyze(events), changed=["README.md"])
        self.assertTrue(gate.judge(gate.CASES["B1"], result))

    def test_code_fence_and_empty_headings_are_not_plan(self):
        for plan in ("```markdown\n" + PLAN + "\n```", "\n".join("> " + l for l in PLAN.splitlines()), "\n".join(gate.HEADINGS),
                     "## 验收标准\n内容\n## 开发思路\n内容\n## 需求分析\n内容"):
            with self.subTest(plan=plan):
                self.assertFalse(gate.headings_in_order(plan))

    def test_only_known_automatic_memory_is_ignored(self):
        home = str(Path.home())
        for path in (home + "/.claude/settings.json", home + "/.claude-evil/a",
                     home + "/.claude/projects/x/memory-evil/a", home + "/.claude/skills/x/SKILL.md"):
            with self.subTest(path=path):
                self.assertTrue(gate.is_write(write(path)))
        self.assertFalse(gate.is_write(write(home + "/.claude/projects/x/memory/MEMORY.md")))

    def test_question_cannot_write_or_have_unobserved_changes(self):
        for writes, changed in ((["Bash:touch x"], []), ([], ["x"])):
            result = {"before": "", "all": "解释", "writes": writes, "changed": changed}
            self.assertTrue(gate.judge(gate.CASES["E1"], result))

    def test_question_rejects_plan(self):
        for plan in (PLAN, "## 需求分析\n解释"):
            result = {"before": plan, "all": plan, "writes": [], "changed": []}
            self.assertTrue(gate.judge(gate.CASES["E1"], result))

    def test_plan_only_cannot_write_even_if_file_reverted(self):
        result = {"before": PLAN, "all": PLAN, "writes": ["Edit:x"], "changed": []}
        self.assertTrue(gate.judge(gate.CASES["A1"], result))

    def test_plan_only_detects_unobserved_file_changes(self):
        result = {"before": PLAN, "all": PLAN, "writes": [], "changed": ["x"]}
        self.assertTrue(gate.judge(gate.CASES["A1"], result))

    def test_implementation_requires_observed_write_and_target_change(self):
        for writes, changed in (([], []), ([], ["README.md"]), (["Edit:x"], ["unrelated.md"])):
            result = {"before": PLAN, "all": PLAN, "writes": writes, "changed": changed}
            self.assertTrue(gate.judge(gate.CASES["B1"], result))


class InvocationChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)
        self.args = SimpleNamespace(skill_root=gate.SKILL_ROOT, model=None, max_turns=3,
                                    timeout=5, max_budget_usd=1.0)

    def invoke(self, cli, status=None):
        status = status or subprocess.CompletedProcess([], 0, " M README.md\n", "")
        with patch.object(gate, "make_fixture"), patch.object(gate.subprocess, "run", side_effect=[cli, status]):
            result = gate.run_once("B1", 0, self.args, self.logs)
        self.assertTrue((self.logs / "B1-0.jsonl").is_file())
        self.assertTrue((self.logs / "B1-0.stderr.log").is_file())
        summary = json.loads((self.logs / "B1-0.summary.json").read_text())
        return result, summary

    def test_success(self):
        stdout = stream(assistant(text(), write()), success())
        result, summary = self.invoke(subprocess.CompletedProcess([], 0, stdout, "warning"))
        self.assertEqual(result, ("B1", [], 0.12))
        self.assertEqual(summary["status"], "PASS")
        self.assertTrue(summary["cost_known"])
        self.assertEqual((self.logs / "B1-0.stderr.log").read_text(), "warning")

    def test_execution_failures_never_pass_and_keep_logs(self):
        finals = [None, dict(success(), is_error=True, result="out of credits"),
                  dict(success(), subtype="error_max_turns")]
        for final in finals:
            with self.subTest(final=final):
                events = [assistant(text(), write())] + ([final] if final else [])
                result, summary = self.invoke(subprocess.CompletedProcess([], 0, stream(*events), "stderr detail"))
                self.assertTrue(result[1][0].startswith("[错误]"))
                self.assertEqual(summary["status"], "ERROR")
                self.assertEqual(summary["changed"], ["README.md"])
                self.assertEqual((self.logs / "B1-0.stderr.log").read_text(), "stderr detail")

    def test_nonzero_exit_overrides_success_event(self):
        result, summary = self.invoke(subprocess.CompletedProcess([], 1, stream(success()), "exit failure"))
        self.assertEqual(summary["returncode"], 1)
        self.assertTrue(result[1][0].startswith("[错误]"))

    def test_timeout_keeps_partial_bytes_and_changes(self):
        partial = stream(assistant(text(), write())).encode()
        result, summary = self.invoke(subprocess.TimeoutExpired("claude", 5, output=partial, stderr=b"partial error"))
        self.assertIn("超时", result[1][0])
        self.assertEqual(summary["changed"], ["README.md"])
        self.assertEqual((self.logs / "B1-0.jsonl").read_bytes(), partial)
        self.assertEqual((self.logs / "B1-0.stderr.log").read_text(), "partial error")
        self.assertIsNone(result[2])
        self.assertFalse(summary["cost_known"])
        self.assertIsNone(summary["cost_usd"])

    def test_missing_cost_is_unknown_even_with_successful_result(self):
        final = success()
        del final["total_cost_usd"]
        result, summary = self.invoke(subprocess.CompletedProcess([], 0, stream(assistant(text(), write()), final), ""))
        self.assertEqual(summary["status"], "PASS")
        self.assertFalse(summary["cost_known"])
        self.assertIsNone(result[2])

    def test_missing_cli_keeps_error_summary(self):
        result, summary = self.invoke(FileNotFoundError("claude unavailable"))
        self.assertEqual(summary["status"], "ERROR")
        self.assertIn("claude unavailable", result[1][0])

    def test_status_failure_never_passes(self):
        cli = subprocess.CompletedProcess([], 0, stream(assistant(text(), write()), success()), "")
        status = subprocess.CompletedProcess([], 128, "", "not a repository")
        result, summary = self.invoke(cli, status)
        self.assertEqual(summary["status"], "ERROR")
        self.assertTrue(result[1][0].startswith("[错误]"))

    def test_malformed_stream_never_passes(self):
        for suffix in ("\n[1,2]", "\nnot json"):
            with self.subTest(suffix=suffix):
                stdout = stream(assistant(text(), write()), success()) + suffix
                result, summary = self.invoke(subprocess.CompletedProcess([], 0, stdout, ""))
                self.assertEqual(summary["status"], "ERROR")
                self.assertTrue(result[1][0].startswith("[错误]"))

    def test_fixture_error_has_diagnostic_files(self):
        with patch.object(gate, "make_fixture", side_effect=OSError("fixture failed")), \
             patch.object(gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 128, "", "no git")):
            result = gate.run_once("B1", 0, self.args, self.logs)
        self.assertIn("fixture failed", result[1][0])
        self.assertEqual(json.loads((self.logs / "B1-0.summary.json").read_text())["status"], "ERROR")
        self.assertEqual((self.logs / "B1-0.stderr.log").read_text(), "fixture failed")

    def test_multiple_or_incomplete_success_results_rejected(self):
        for stdout in (stream(success(), success()), stream({"type": "result", "subtype": "success"})):
            result, summary = self.invoke(subprocess.CompletedProcess([], 0, stdout, ""))
            self.assertEqual(summary["status"], "ERROR")

    def test_budget_is_forwarded_to_cli(self):
        cli = subprocess.CompletedProcess([], 0, stream(assistant(text(), write()), success()), "")
        status = subprocess.CompletedProcess([], 0, " M README.md\n", "")
        with patch.object(gate, "make_fixture"), patch.object(gate.subprocess, "run", side_effect=[cli, status]) as run:
            gate.run_once("B1", 0, self.args, self.logs)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "1.0")


class MainChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)

    def main(self, arguments, side_effect):
        output = io.StringIO()
        with patch("sys.argv", ["run_gate_cases.py", *arguments]), patch.object(gate, "RUNS_DIR", self.logs), \
             patch.object(gate, "run_once", side_effect=side_effect) as run, contextlib.redirect_stdout(output):
            code = gate.main()
        return code, output.getvalue(), run

    def test_invalid_parameters_fail_before_model(self):
        arguments = (["-c"], ["-r", "0"], ["-j", "0"], ["--timeout", "0"], ["--max-turns", "-1"],
                     ["--label", "../escape"], ["-c", "MISSING"], ["--max-budget-usd", "0"],
                     ["--max-budget-usd", "nan"], ["--max-budget-usd", "inf"], ["-c", "B1", "B1"])
        for args in arguments:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with patch("sys.argv", ["run_gate_cases.py", *args]), patch.object(gate, "run_once") as run:
                    with self.assertRaises(SystemExit) as err:
                        gate.main()
                    self.assertEqual(err.exception.code, 2)
                    run.assert_not_called()

    def test_missing_references_rejected_before_model(self):
        (self.logs / "SKILL.md").write_text("skill")
        with patch("sys.argv", ["run_gate_cases.py", "--skill-root", str(self.logs)]), \
             patch.object(gate, "run_once") as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gate.main()
            run.assert_not_called()

    def test_defaults_are_serial_and_stop_at_first_error(self):
        def call(case_id, idx, args, log_dir):
            self.assertEqual(args.jobs, 1)
            return case_id, ["[错误] authentication failed"], 0.0
        code, output, run = self.main(["-c", "B1", "B2"], call)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED", output)
        self.assertTrue(list(self.logs.glob("*/B2-0.summary.json")))

    def test_success_plus_error_is_incomplete(self):
        code, output, run = self.main(["-c", "B1", "-r", "3"],
                                      [("B1", [], 0.1), ("B1", ["[错误] credits exhausted"], 0.0)])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(code, 1)
        self.assertIn("INCOMPLETE", output)
        self.assertNotIn("| B1 | PASS |", output)

    def test_all_success_passes(self):
        code, output, run = self.main(["-c", "B1"], [("B1", [], 0.1)])
        self.assertEqual(code, 0)
        self.assertIn("| B1 | PASS |", output)

    def test_unknown_cost_is_not_reported_as_zero_total(self):
        code, output, _ = self.main(["-c", "B1"], [("B1", ["[错误] 超时"], None)])
        self.assertEqual(code, 1)
        self.assertIn("1 次运行费用未知", output)
        self.assertNotIn("总花费约 $0", output)
        summary = json.loads(next(self.logs.glob("*/summary.json")).read_text())
        self.assertEqual(summary["unknown_cost_runs"], 1)
        self.assertFalse(summary["cost_known"])

    def test_parallel_error_does_not_submit_entire_batch(self):
        # 已在途的两个请求可完成，第三个及之后必须跳过。
        def submit(fn, case_id, idx, args, log_dir):
            future = Future()
            future.set_result((case_id, ["[错误] out of credits"] if case_id == "B1" else [], 0.0))
            return future
        with patch.object(gate, "ThreadPoolExecutor") as pool:
            pool.return_value.__enter__.return_value.submit.side_effect = submit
            code, output, _ = self.main(["-j", "2", "-c", "B1", "B2", "B3", "B4"], None)
            self.assertEqual(pool.return_value.__enter__.return_value.submit.call_count, 2)
        self.assertEqual(code, 1)
        self.assertIn("| B3 | SKIPPED |", output)
        self.assertIn("| B4 | SKIPPED |", output)


if __name__ == "__main__":
    unittest.main()
