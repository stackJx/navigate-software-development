"""离线校验门禁判定与 CLI 故障处理，不调用模型、不产生 API 费用。"""
import contextlib
from concurrent.futures import Future, ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
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

    def test_shell_comments_preserve_following_command_boundaries(self):
        for command in ("pwd # inspect project\nmkdir scratch", "cat README.md # inspect\nrm README.md",
                        "# inspect first\npwd # directory\ntouch scratch", "echo word#literal > scratch",
                        "echo \\#literal > scratch", "echo '#literal' > scratch"):
            with self.subTest(command=command):
                self.assertTrue(gate.is_write_command(command))
        for command in ("pwd # inspect project\nls", "pwd # touch scratch\nls",
                        "echo '# not a comment'\nls", 'echo "# not a comment"\nls',
                        "echo \\#literal\nls", "echo word#literal\nls"):
            with self.subTest(command=command):
                self.assertFalse(gate.is_write_command(command))

    def test_comment_cannot_hide_write_from_plan_only_gate(self):
        events = [assistant({"type": "tool_use", "name": "Bash",
                             "input": {"command": "pwd # inspect\nmkdir scratch"}}), assistant(text())]
        result = dict(gate.analyze(events), changed=[])
        self.assertIn("纯方案任务出现写操作", gate.judge(gate.CASES["A1"], result))

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

    def test_no_plan_implementation_case_preserves_user_instruction(self):
        self.assertEqual(gate.CASES["I1"].prompt,
                         "直接改，不用写方案：README 里的‘限届上下文’改成‘限界上下文’。")
        self.assertEqual(gate.CASES["I1"].expect, "implement_no_plan")
        self.assertEqual(gate.CASES["I2"].expect, "implement")

    def test_no_plan_implementation_accepts_target_write_without_plan(self):
        result = dict(gate.analyze([assistant(write())]), changed=["README.md"])
        self.assertEqual(gate.judge(gate.CASES["I1"], result), [])

    def test_no_plan_implementation_still_requires_write_and_target_change(self):
        for blocks, changed in (([], []), ([], ["README.md"]), ([write()], []),
                                ([write("unrelated.md")], ["unrelated.md"])):
            with self.subTest(changed=changed, blocks=blocks):
                result = dict(gate.analyze([assistant(*blocks)]), changed=changed)
                self.assertTrue(gate.judge(gate.CASES["I1"], result))

    def test_no_plan_implementation_rejects_visible_plan_before_or_after_write(self):
        for plan in (PLAN, "## 需求分析\n解释"):
            for blocks in ((text(plan), write()), (write(), text(plan))):
                with self.subTest(plan=plan, blocks=blocks):
                    result = dict(gate.analyze([assistant(*blocks)]), changed=["README.md"])
                    self.assertTrue(gate.judge(gate.CASES["I1"], result))

    def test_shortening_request_still_requires_plan(self):
        result = dict(gate.analyze([assistant(write("shop/coupon.py"))]), changed=["shop/coupon.py"])
        self.assertTrue(gate.judge(gate.CASES["I2"], result))
        result = dict(gate.analyze([assistant(text(), write("shop/coupon.py"))]), changed=["shop/coupon.py"])
        self.assertEqual(gate.judge(gate.CASES["I2"], result), [])

    def test_html_comments_do_not_supply_visible_plan_or_section_content(self):
        hidden_plans = ("<!--\n" + PLAN + "\n-->", "<!--\n" + PLAN,
                        "\n".join(h + "\n<!-- 隐藏正文 -->" for h in gate.HEADINGS),
                        "\n".join(h + "\n<!-- 隐藏\n正文 -->" for h in gate.HEADINGS))
        for plan in hidden_plans:
            with self.subTest(plan=plan):
                self.assertFalse(gate.headings_in_order(plan))
                result = dict(gate.analyze([assistant(text(plan), write())]), changed=["README.md"])
                self.assertTrue(gate.judge(gate.CASES["B1"], result))

    def test_comment_and_fence_state_do_not_hide_later_visible_plan(self):
        prefixes = ("```html\n<!-- 未闭合的字面注释\n```\n",
                    "```html <!-- 字面注释\n```\n",
                    "<!--\n``` 隐藏的围栏\n-->\n",
                    "<!-- 第一条 --> <!-- 第二条\n结束 -->\n",
                    "代码标记 `<!--` 是字面量。\n",
                    "> <!-- 引用中的未闭合注释\n")
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertTrue(gate.headings_in_order(prefix + PLAN))

    def test_inline_html_comments_preserve_surrounding_visible_content(self):
        plan = "\n".join(h + "\n可见<!-- 隐藏 -->内容。" for h in gate.HEADINGS)
        self.assertEqual(gate.visible_prose(plan), "\n".join(h + "\n可见内容。" for h in gate.HEADINGS))
        result = dict(gate.analyze([assistant(text(plan), write())]), changed=["README.md"])
        self.assertEqual(gate.judge(gate.CASES["B1"], result), [])

    def test_separate_replies_cannot_combine_into_plan(self):
        replies = [assistant(text(h + "\n具体内容。")) for h in gate.HEADINGS]
        for case_id, events, changed in (("B1", [*replies, assistant(write())], ["README.md"]),
                                        ("A1", replies, [])):
            with self.subTest(case_id=case_id):
                result = dict(gate.analyze(events), changed=changed)
                self.assertTrue(gate.headings_in_order(result["before"]))
                self.assertTrue(gate.judge(gate.CASES[case_id], result))
        for case_id, changed, blocks in (("B1", ["README.md"], [write()]), ("A1", [], [])):
            with self.subTest(complete_second_reply=case_id):
                events = [assistant(text("<!--")), assistant(text(), *blocks)]
                result = dict(gate.analyze(events), changed=changed)
                self.assertEqual(gate.judge(gate.CASES[case_id], result), [])

    def test_text_blocks_in_one_reply_can_supply_complete_plan(self):
        blocks = [text(h + "\n具体内容。") for h in gate.HEADINGS]
        result = dict(gate.analyze([assistant(*blocks, write())]), changed=["README.md"])
        self.assertEqual(result["before_messages"], [PLAN])
        self.assertEqual(gate.judge(gate.CASES["B1"], result), [])
        result = dict(gate.analyze([assistant(*blocks)]), changed=[])
        self.assertEqual(gate.judge(gate.CASES["A1"], result), [])

    def test_main_reply_boundaries_exclude_child_and_post_write_text(self):
        events = [assistant(text(), parent="task-1"),
                  assistant(text("只读进度"), write(), text())]
        result = dict(gate.analyze(events), changed=["README.md"])
        self.assertEqual(result["before_messages"], ["只读进度"])
        self.assertTrue(gate.judge(gate.CASES["B1"], result))

    def test_missing_reply_boundaries_do_not_fall_back_to_joined_text(self):
        result = {"before": PLAN, "all": PLAN, "writes": ["Edit:README.md"], "changed": ["README.md"]}
        self.assertTrue(gate.judge(gate.CASES["B1"], result))
        result.update(writes=[], changed=[])
        self.assertTrue(gate.judge(gate.CASES["A1"], result))

    def test_no_plan_cases_check_each_visible_reply_independently(self):
        replies = [assistant(text("<!--")), assistant(text())]
        for case_id, events, changed in (("I1", [*replies, assistant(write())], ["README.md"]),
                                        ("E1", replies, [])):
            with self.subTest(case_id=case_id):
                result = dict(gate.analyze(events), changed=changed)
                self.assertTrue(gate.judge(gate.CASES[case_id], result))

    def test_no_plan_cases_accept_only_hidden_comment_headings(self):
        hidden = "<!--\n" + PLAN + "\n-->"
        for case_id, events, changed in (("I1", [assistant(text(hidden), write())], ["README.md"]),
                                        ("E1", [assistant(text(hidden), text("函数解释。"))], [])):
            with self.subTest(case_id=case_id):
                result = dict(gate.analyze(events), changed=changed)
                self.assertEqual(gate.judge(gate.CASES[case_id], result), [])

    def test_missing_reply_boundaries_cannot_prove_no_plan(self):
        for case_id, writes, changed in (("I1", ["Edit:README.md"], ["README.md"]), ("E1", [], [])):
            with self.subTest(case_id=case_id):
                result = {"before": "", "all": "简短说明。", "writes": writes, "changed": changed}
                self.assertTrue(gate.judge(gate.CASES[case_id], result))

    def test_code_fence_and_empty_headings_are_not_plan(self):
        for plan in ("```markdown\n" + PLAN + "\n```", "\n".join("> " + l for l in PLAN.splitlines()), "\n".join(gate.HEADINGS),
                     "## 验收标准\n内容\n## 开发思路\n内容\n## 需求分析\n内容"):
            with self.subTest(plan=plan):
                self.assertFalse(gate.headings_in_order(plan))

    def test_fence_close_requires_matching_length_and_only_whitespace(self):
        for opening, false_close in (("````markdown", "```"), ("~~~~text", "~~~"),
                                     ("```markdown", "``` still code"), ("~~~text", "~~~ still code"),
                                     ("```markdown", "~~~")):
            with self.subTest(opening=opening, false_close=false_close):
                plan = opening + "\n" + false_close + "\n" + PLAN
                self.assertFalse(gate.headings_in_order(plan))
                result = {"before": plan, "all": plan, "before_messages": [plan], "all_messages": [plan],
                          "writes": ["Edit:README.md"], "changed": ["README.md"]}
                self.assertTrue(gate.judge(gate.CASES["B1"], result))

    def test_visible_plan_after_valid_fence_still_passes(self):
        for opening, closing in (("```markdown", "```"), ("~~~~text", "~~~~~  "),
                                 ("````markdown", "`````\t")):
            with self.subTest(opening=opening, closing=closing):
                plan = opening + "\ncode\n" + closing + "\n" + PLAN
                self.assertTrue(gate.headings_in_order(plan))

    def test_only_known_automatic_memory_is_ignored(self):
        home = str(Path.home())
        for path in (home + "/.claude/settings.json", home + "/.claude-evil/a",
                     home + "/.claude/projects/x/memory-evil/a", home + "/.claude/skills/x/SKILL.md"):
            with self.subTest(path=path):
                self.assertTrue(gate.is_write(write(path)))
        self.assertFalse(gate.is_write(write(home + "/.claude/projects/x/memory/MEMORY.md")))

    def test_question_cannot_write_or_have_unobserved_changes(self):
        for writes, changed in ((["Bash:touch x"], []), ([], ["x"])):
            result = {"before": "", "all": "解释", "before_messages": [], "all_messages": ["解释"],
                      "writes": writes, "changed": changed}
            self.assertTrue(gate.judge(gate.CASES["E1"], result))

    def test_question_rejects_plan(self):
        for plan in (PLAN, "## 需求分析\n解释"):
            result = {"before": plan, "all": plan, "before_messages": [plan], "all_messages": [plan],
                      "writes": [], "changed": []}
            self.assertTrue(gate.judge(gate.CASES["E1"], result))

    def test_plan_only_cannot_write_even_if_file_reverted(self):
        result = {"before": PLAN, "all": PLAN, "before_messages": [PLAN], "all_messages": [PLAN],
                  "writes": ["Edit:x"], "changed": []}
        self.assertTrue(gate.judge(gate.CASES["A1"], result))

    def test_plan_only_detects_unobserved_file_changes(self):
        result = {"before": PLAN, "all": PLAN, "before_messages": [PLAN], "all_messages": [PLAN],
                  "writes": [], "changed": ["x"]}
        self.assertTrue(gate.judge(gate.CASES["A1"], result))

    def test_implementation_requires_observed_write_and_target_change(self):
        for writes, changed in (([], []), ([], ["README.md"]), (["Edit:x"], ["unrelated.md"])):
            result = {"before": PLAN, "all": PLAN, "before_messages": [PLAN], "all_messages": [PLAN],
                      "writes": writes, "changed": changed}
            self.assertTrue(gate.judge(gate.CASES["B1"], result))


class DocumentScopeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = "docs/specs/coupon.md"
        self.document = self.root / self.target
        self.document.parent.mkdir(parents=True)
        self.document.write_text("# 满减券规格\n\n待实现。\n", encoding="utf-8")

    def check(self, *blocks, changed=None, case_id="J2", plan=PLAN):
        result = dict(gate.analyze([assistant(text(plan), *blocks)]),
                      changed=[self.target] if changed is None else changed,
                      fixture_root=self.root)
        return gate.judge(gate.CASES[case_id], result)

    def bash(self, command):
        return {"type": "tool_use", "name": "Bash", "input": {"command": command}}

    def test_j1_consultation_stays_read_only(self):
        result = dict(gate.analyze([assistant(text())]), changed=[])
        self.assertEqual(gate.judge(gate.CASES["J1"], result), [])
        result = dict(gate.analyze([assistant(text(), write(self.target))]), changed=[])
        self.assertTrue(gate.judge(gate.CASES["J1"], result))

    def test_document_scope_accepts_normalized_paths_and_known_reads(self):
        reads = [{"type": "tool_use", "name": name, "input": {}} for name in ("Read", "Grep", "Glob", "Skill")]
        for path in (self.target, "./" + self.target, str(self.document)):
            with self.subTest(path=path):
                self.assertEqual(self.check(*reads, self.bash("git status && cat README.md"), write(path)), [])

    def test_document_scope_accepts_cd_as_read_command_argument(self):
        for command in ("grep cd README.md", "echo cd", "git grep cd", "cat cd", "git -C cd status",
                        "which cd", "command -v cd", "xargs grep cd", "grep cd README.md && echo cd",
                        "grep cd README.md | head -1", "echo cd\ncat README.md", "pwd # cd\nls"):
            with self.subTest(command=command):
                self.assertEqual(self.check(self.bash(command), write(self.target)), [])

    def test_document_scope_rejects_executed_cd_in_every_command_segment(self):
        for command in ("cd", "cd docs", "cd docs && cat specs/coupon.md", "pwd && cd docs",
                        "pwd||cd docs", "pwd;cd docs", "pwd | cd docs", "pwd\ncd docs",
                        "pwd &&\ncd docs", "pwd # inspect\ncd docs", '"cd" docs', "c\\d docs",
                        "xargs cd", "xargs xargs cd"):
            with self.subTest(command=command):
                # 旧用例允许只读 Shell 调整目录，仅规格模式需要拒绝它。
                self.assertFalse(gate.is_write_command(command))
                self.assertTrue(self.check(self.bash(command), write(self.target)))

    def test_document_scope_accepts_only_explicit_parent_directory_mkdir(self):
        for command in ("mkdir -p docs/specs", "mkdir -p docs docs/specs", "mkdir -- docs/specs",
                        "mkdir -p " + str(self.document.parent)):
            with self.subTest(command=command):
                self.assertEqual(self.check(self.bash(command), write(self.target)), [])
        for command in ("mkdir -p scratch", "mkdir -p docs/specs/unrelated", "mkdir -m 777 docs/specs",
                        "mkdir -p docs/specs && touch shop/new.py", "mkdir -p $TARGET", "mkdir -p docs/*"):
            with self.subTest(command=command):
                self.assertTrue(self.check(self.bash(command), write(self.target)))

    def test_doc_only_rejects_code_writes_even_if_restored(self):
        self.assertTrue(self.check(write("shop/coupon.py"), write("shop/coupon.py"), write(self.target)))

    def test_doc_only_rejects_test_build_git_and_unparsed_shell_writes(self):
        for command in ("python3 -m unittest", "pytest", "npm run build", "git add -A", "git commit -m docs",
                        "git switch -c spec", "git status # query\nrm shop/coupon.py", "echo x > " + self.target,
                        "python3 -c 'pass'", "cd docs && cat specs/coupon.md"):
            with self.subTest(command=command):
                self.assertTrue(self.check(self.bash(command), write(self.target)))

    def test_doc_only_rejects_unknown_tools_and_missing_paths(self):
        for block in (write(""), {"type": "tool_use", "name": "Edit", "input": {}},
                      {"type": "tool_use", "name": "NewWriteTool", "input": {"file_path": self.target}}):
            with self.subTest(block=block):
                self.assertTrue(self.check(block, write(self.target)))

    def test_doc_only_preserves_only_explicit_automatic_memory_exemption(self):
        automatic_memory = str(gate.HARNESS_PROJECTS / "fixture" / "memory" / "MEMORY.md")
        self.assertEqual(self.check(write(automatic_memory), write(self.target)), [])
        for path in (".claude/settings.json", ".claude/skills/example/SKILL.md"):
            with self.subTest(path=path):
                self.assertTrue(self.check(write(path), write(self.target)))

    def test_doc_only_rejects_outside_and_ambiguous_paths(self):
        for path in ("../docs/specs/coupon.md", "/tmp/docs/specs/coupon.md", "docs/../docs/specs/coupon.md",
                     "~/docs/specs/coupon.md", "${ROOT}/docs/specs/coupon.md", None, 3):
            with self.subTest(path=path):
                self.assertTrue(self.check(write(path), write(self.target)))

    def test_doc_only_rejects_symlink_documents_and_ancestors(self):
        self.document.unlink()
        self.document.symlink_to(self.root / "elsewhere.md")
        (self.root / "elsewhere.md").write_text("outside approved document")
        self.assertTrue(self.check(write(self.target)))
        self.document.unlink()
        self.document.parent.rmdir()
        self.document.parent.symlink_to(self.root)
        self.assertTrue(self.check(write(self.target)))

    def test_doc_only_requires_actual_nonempty_regular_document(self):
        self.document.unlink()
        self.assertTrue(self.check(write(self.target)))
        self.document.write_text("  \n")
        self.assertTrue(self.check(write(self.target)))
        self.document.unlink()
        self.document.mkdir()
        self.assertTrue(self.check(write(self.target)))

    def test_doc_only_requires_observed_document_write_and_target_change(self):
        self.assertTrue(self.check())
        self.assertTrue(self.check(self.bash("mkdir -p docs/specs")))
        self.assertTrue(self.check(write(self.target), changed=[]))
        self.assertTrue(self.check(write(self.target), changed=[self.target, "KNOWLEDGE.md"]))

    def test_doc_only_requires_plan_before_directory_creation(self):
        events = [assistant(self.bash("mkdir -p docs/specs"), text(), write(self.target))]
        result = dict(gate.analyze(events), changed=[self.target], fixture_root=self.root)
        self.assertTrue(gate.judge(gate.CASES["J2"], result))

    def test_doc_only_uses_untruncated_child_tool_events(self):
        long_input = {"irrelevant": "x" * 300, "file_path": "shop/coupon.py"}
        child = {"type": "tool_use", "name": "Edit", "input": long_input}
        events = [assistant(text()), assistant(child, parent="child"), assistant(write(self.target))]
        result = dict(gate.analyze(events), changed=[self.target], fixture_root=self.root)
        self.assertEqual(result["tool_events"][0]["input"], long_input)
        self.assertNotIn("shop/coupon.py", result["writes"][0])
        self.assertTrue(gate.judge(gate.CASES["J2"], result))

    def test_doc_only_updates_existing_path_without_competing_document(self):
        existing = "docs/requirements/coupon.md"
        document = self.root / existing
        document.parent.mkdir(parents=True)
        document.write_text("# 已有规格\n修订 r2\n")
        self.assertEqual(self.check(write(existing), changed=[existing], case_id="J3"), [])
        self.assertTrue(self.check(write(existing), write(self.target), changed=[existing], case_id="J3"))

    def test_strict_document_scope_does_not_restrict_legacy_implementation(self):
        result = dict(gate.analyze([assistant(text(), self.bash("python3 -m unittest"), write("README.md"))]),
                      changed=["README.md", "KNOWLEDGE.md"])
        self.assertEqual(gate.judge(gate.CASES["B1"], result), [])


class InvocationChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)
        self.args = SimpleNamespace(skill_root=gate.SKILL_ROOT, model=None, max_turns=3,
                                    timeout=5, max_budget_usd=1.0)

    def invoke(self, cli, status=None):
        status = status or subprocess.CompletedProcess([], 0, " M README.md\n", "")
        with patch.object(gate, "make_fixture"), patch.object(gate, "run_cli", side_effect=[cli]), \
             patch.object(gate.subprocess, "run", return_value=status):
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
        with patch.object(gate, "make_fixture"), patch.object(gate, "run_cli", return_value=cli) as run, \
             patch.object(gate.subprocess, "run", return_value=status):
            gate.run_once("B1", 0, self.args, self.logs)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "1.0")

    def test_no_plan_implementation_checks_real_target_and_keeps_summary(self):
        def cli(command, root, timeout):
            target = root / "README.md"
            target.write_text(target.read_text().replace("限届上下文", "限界上下文"), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stream(assistant(write(str(target))), success()), "")

        with patch.object(gate, "run_cli", side_effect=cli):
            result = gate.run_once("I1", 0, self.args, self.logs)
        summary = json.loads((self.logs / "I1-0.summary.json").read_text())
        self.assertEqual(result, ("I1", [], 0.12))
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["changed"], ["README.md"])
        self.assertEqual(summary["plan_length"], 0)
        self.assertEqual(summary["cost_usd"], 0.12)

    def test_doc_only_end_to_end_checks_fixture_before_cleanup(self):
        for case_id in ("J2", "J3"):
            for restored_code_write in (False, True):
                with self.subTest(case_id=case_id, restored_code_write=restored_code_write):
                    def cli(command, root, timeout):
                        self.assertNotIn("--allowedTools", command)
                        target = root / gate.CASES[case_id].allowed_paths[0]
                        self.assertEqual(target.exists(), case_id == "J3")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("# 满减券规格\n\n修订 r2，待实施。\n", encoding="utf-8")
                        blocks = [write(str(target))]
                        if restored_code_write:
                            code = root / "shop/coupon.py"
                            original = code.read_text()
                            code.write_text("wrong implementation")
                            code.write_text(original)
                            blocks.extend([write(str(code)), write(str(code))])
                        return subprocess.CompletedProcess(command, 0, stream(assistant(text(), *blocks), success()), "")

                    with patch.object(gate, "run_cli", side_effect=cli):
                        result = gate.run_once(case_id, 0, self.args, self.logs)
                    summary = json.loads((self.logs / f"{case_id}-0.summary.json").read_text())
                    self.assertEqual(summary["changed"], list(gate.CASES[case_id].allowed_paths))
                    self.assertEqual(summary["status"], "FAIL" if restored_code_write else "PASS")
                    self.assertEqual(bool(result[1]), restored_code_write)

    def test_doc_only_rejects_missing_artifact_in_real_fixture(self):
        def cli(command, root, timeout):
            target = root / gate.CASES["J2"].allowed_paths[0]
            return subprocess.CompletedProcess(command, 0, stream(assistant(text(), write(str(target))), success()), "")
        with patch.object(gate, "run_cli", side_effect=cli):
            result = gate.run_once("J2", 0, self.args, self.logs)
        self.assertTrue(any("非空普通文档" in failure for failure in result[1]))


@unittest.skipUnless(os.name == "posix", "POSIX process-group regression")
class ProcessChecks(unittest.TestCase):
    def test_timeout_stops_children_even_after_cli_parent_exits(self):
        original_popen = subprocess.Popen
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits), tempfile.TemporaryDirectory() as tmp:
                logs = Path(tmp)
                pids, marker = logs / "pids.json", logs / "late-write"
                child = ("import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                         "time.sleep(1.2); pathlib.Path("
                         + repr(str(marker)) + ").write_text('child still running')")
                parent = ("import json,os,pathlib,subprocess,sys,time\n"
                          "child=subprocess.Popen([sys.executable,'-B','-c'," + repr(child) + "])\n"
                          "pathlib.Path(" + repr(str(pids)) + ").write_text(json.dumps("
                          "{'parent':os.getpid(),'child':child.pid,'group':os.getpgrp()}))\n"
                          "print(" + repr(stream(assistant(text()))) + ",flush=True)\n"
                          "print('partial stderr',file=sys.stderr,flush=True)\n"
                          + ("sys.exit(0)\n" if parent_exits else "time.sleep(30)\n"))

                def offline_popen(command, *args, **kwargs):
                    if command[0] == "claude":
                        command = [sys.executable, "-B", "-c", parent]
                    return original_popen(command, *args, **kwargs)

                args = SimpleNamespace(skill_root=gate.SKILL_ROOT, model=None, max_turns=3,
                                       timeout=0.5, max_budget_usd=1.0)
                try:
                    # Match run_jobs: Popen must be safe when started from a worker thread.
                    with patch.object(gate.subprocess, "Popen", side_effect=offline_popen), \
                         ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(gate.run_once, "B1", 0, args, logs).result(timeout=10)
                    observed = json.loads(pids.read_text())
                    time.sleep(1.2)
                    self.assertFalse(marker.exists(), "child wrote after run_once timed out")
                    for pid in (observed["parent"], observed["child"]):
                        # Linux 容器的 init 可能延迟回收孤儿僵尸；它们已经不能执行或写入。
                        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                               capture_output=True, text=True, check=False).stdout.strip()
                        self.assertTrue(not state or state.startswith("Z"), f"process {pid} still running: {state}")
                    self.assertIn("超时", result[1][0])
                    self.assertIsNone(result[2])
                    self.assertIn(PLAN, json.loads((logs / "B1-0.jsonl").read_text())["message"]["content"][0]["text"])
                    self.assertEqual((logs / "B1-0.stderr.log").read_text(), "partial stderr\n")
                    summary = json.loads((logs / "B1-0.summary.json").read_text())
                    self.assertEqual(summary["status"], "ERROR")
                    self.assertFalse(summary["cost_known"])
                finally:
                    # A failing regression must never leave its own child running or kill our test group.
                    if pids.exists():
                        observed = json.loads(pids.read_text())
                        for pid in (observed["child"], observed["parent"]):
                            try:
                                if os.getpgid(pid) == observed["group"]:
                                    os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass


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
