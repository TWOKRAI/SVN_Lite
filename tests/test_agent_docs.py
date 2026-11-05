"""Task AG.6: документы для ИИ-помощника (docs/agent) совпадают с реальной программой и не нарушают правил слов.

Проверки: команды в SKILL.md и INSTALL_AGENT.md существуют в `svnlite --help`; имена инструментов MCP есть в
`svnhelper/mcp_server.py`; в AGENT_RULES.md нет слов svn вне блока «Что не говорить человеку»; нет паролей, логинов
и чужих адресов сервера; образец разрешений Claude Code это валидный JSON, где разрешено только существующее.
"""
import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "agent"
SERVER_ADDRESS = "svn://192.168.11.11"
FORBIDDEN_IN_RULES = re.compile(r"commit|update|revision|lock|checkout|trunk", re.IGNORECASE)
NOT_TO_SAY_HEADING = "## Что не говорить человеку"
# пароли и логины: ключи командной строки, пары «ключ=значение», слова «пароль: …»
SECRETS = re.compile(r"password|passwd|--username|--user\b|login\s*[=:]\s*\S|user(name)?\s*=|логин\s*[:=]\s*\S|"
                     r"пароль\s*[:=]\s*\S", re.IGNORECASE)
SVNLITE_CALL = re.compile(r"svnlite(?:\.exe)?[ \t]+([A-Za-z][A-Za-z-]*)")
DOC_FILES = ("AGENT_RULES.md", "SKILL.md", "INSTALL_AGENT.md", "claude_code_permissions.sample.json")
# команды записи: пары `<имя>-plan` (читают) и `<имя>-apply` (пишут, в образце разрешений только в `ask`)
WRITE_BASES = ("get-new", "save", "lock", "unlock", "branch", "switch", "rollback", "create-project", "publish")
PLAN_COMMANDS = {f"{base}-plan" for base in WRITE_BASES}
APPLY_COMMANDS = {f"{base}-apply" for base in WRITE_BASES}


def read_doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def cli_help() -> str:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "120"}
    result = subprocess.run([sys.executable, "-m", "svnhelper.cli", "--help"], cwd=ROOT, env=env, capture_output=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8")


def help_commands(text: str) -> set:
    """Команды из раздела «команды:» справки: `    имя` или `    имя (запасное-имя)`."""
    found, inside = set(), False
    for line in text.splitlines():
        if line.startswith("команды:"):
            inside = True
        elif inside and line.startswith("Коды возврата"):
            break
        elif inside:
            match = re.match(r"^    ([a-z][a-z-]*)(?: \(([a-z-]+)\))?(?:\s|$)", line)
            if match:
                found.update(name for name in match.groups() if name)
    return found


def mcp_tool_names() -> set:
    """Значения словаря TOOL_NAMES из mcp_server.py (без импорта: пакет mcp может быть не установлен)."""
    tree = ast.parse((ROOT / "svnhelper" / "mcp_server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TOOL_NAMES" for t in node.targets):
            return set(ast.literal_eval(node.value).values())
    raise AssertionError("TOOL_NAMES не найден в mcp_server.py")


def rules_without_reference_block(text: str) -> str:
    """AGENT_RULES.md без блока «Что не говорить человеку» (от его заголовка до следующего «## » или конца)."""
    kept, skipping = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = line.strip() == NOT_TO_SAY_HEADING
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


class AgentDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.help_text = cli_help()
        cls.commands = help_commands(cls.help_text)

    def test_all_documents_exist(self):
        for name in (*DOC_FILES, "claude_desktop_config.sample.json", "mcp.json.sample"):
            self.assertTrue((DOCS / name).is_file(), name)

    def test_help_parser_sees_commands(self):
        self.assertGreaterEqual(self.commands, {"doctor", "projects", "status", "changes", "incoming", "history",
                                                "locks", "templates", "info", "server-projects", "mcp"})

    def test_skill_commands_exist_in_help(self):
        for name in ("SKILL.md", "INSTALL_AGENT.md"):
            used = set(SVNLITE_CALL.findall(read_doc(name)))
            self.assertTrue(used, name)
            self.assertEqual(used - self.commands, set(), f"{name}: нет в `svnlite --help`")

    def test_skill_describes_every_read_command(self):
        used = set(SVNLITE_CALL.findall(read_doc("SKILL.md")))
        self.assertEqual({c for c in self.commands if c != "project-info"} - used, set())
        self.assertGreaterEqual(self.commands, PLAN_COMMANDS | APPLY_COMMANDS | {"commands"})

    def test_skill_frontmatter(self):
        text = read_doc("SKILL.md")
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---\n")[1]
        fields = dict(line.split(": ", 1) for line in front.strip().splitlines())
        self.assertEqual(fields["name"], "svn-lite")
        # allowed-tools перечисляет только чтение и планы: любая запись (`…-apply`) требует вопроса человеку
        allowed = re.findall(r"Bash\(svnlite ([a-z-]+):\*\)", fields["allowed-tools"])
        self.assertEqual(set(allowed), self.commands - {"mcp", "project-info"} - APPLY_COMMANDS)
        self.assertNotIn("Bash(svnlite:*)", fields["allowed-tools"])
        self.assertGreater(len(fields["description"]), 40)

    def test_exit_codes_in_skill_match_help(self):
        codes = re.findall(r"^ {2}(\d)  ", self.help_text, re.MULTILINE)
        self.assertEqual(sorted(codes), [str(i) for i in range(10)])
        skill = read_doc("SKILL.md")
        for code in codes:
            self.assertRegex(skill, rf"(?m)^\| {code} \|", f"код {code} не описан в SKILL.md")

    def test_mcp_tools_named_in_docs_exist(self):
        tools = mcp_tool_names()
        self.assertEqual(len(tools), 11)
        tools = tools | {f"{b.replace('-', '_')}_{kind}" for b in WRITE_BASES for kind in ("plan", "apply")}
        skill = read_doc("SKILL.md")
        install = read_doc("INSTALL_AGENT.md")
        for tool in (*tools, "job_status"):
            self.assertIn(f"`{tool}`", skill)
            self.assertIn(f"`{tool}`", install)

    def test_rules_have_no_svn_words_outside_reference_block(self):
        text = read_doc("AGENT_RULES.md")
        self.assertIn(NOT_TO_SAY_HEADING, text)
        self.assertTrue(FORBIDDEN_IN_RULES.search(text), "блок «что не говорить» должен называть эти слова")
        found = [(n, line) for n, line in enumerate(rules_without_reference_block(text).splitlines(), 1)
                 if FORBIDDEN_IN_RULES.search(line)]
        self.assertEqual(found, [])

    def test_reference_block_filter_works(self):
        sample = "## А\ntext\n## Что не говорить человеку\ncommit\n## Б\nok\n"
        self.assertEqual(rules_without_reference_block(sample), "## А\ntext\n## Б\nok")

    def test_rules_cover_required_topics(self):
        text = read_doc("AGENT_RULES.md")
        for fragment in ("сохранил на сервер, сохранение № 7", "занять файл", "данные коллег",
                         "Запись выключена и включается только человеком", "VPN", "откройте SVN-lite и войдите", "не повторять",
                         "settings.json", "agent_policy.json", "Subversion\\auth"):
            self.assertIn(fragment.lower(), text.lower())
        for code in ("3", "4", "7", "8"):
            self.assertRegex(text, rf"(?m)^\| {code} \|", f"код {code}")

    def test_no_passwords_or_logins(self):
        for name in (*DOC_FILES, "claude_desktop_config.sample.json", "mcp.json.sample"):
            self.assertIsNone(SECRETS.search(read_doc(name)), name)

    def test_only_the_real_server_address(self):
        for name in (*DOC_FILES, "claude_desktop_config.sample.json", "mcp.json.sample"):
            text = read_doc(name)
            for url in re.findall(r"svn://[^\s`'\"),;]+", text):
                self.assertEqual(url, SERVER_ADDRESS, f"{name}: {url}")
            for address in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
                self.assertEqual(address, "192.168.11.11", f"{name}: {address}")
        self.assertIn(SERVER_ADDRESS, read_doc("AGENT_RULES.md"))

    def test_permissions_sample(self):
        data = json.loads(read_doc("claude_code_permissions.sample.json"))
        permissions = data["permissions"]
        for command in re.findall(r"^Bash\(svnlite ([a-z-]+)", "\n".join(permissions["allow"]), re.MULTILINE):
            self.assertIn(command, self.commands - {"mcp"})
        self.assertEqual({re.match(r"Bash\(svnlite ([a-z-]+)", rule).group(1) for rule in permissions["allow"]},
                         self.commands - {"mcp", "project-info"} - APPLY_COMMANDS)
        self.assertEqual({re.match(r"Bash\(svnlite ([a-z-]+)", rule).group(1) for rule in permissions["ask"]},
                         APPLY_COMMANDS)
        for rule in ("Bash(svn *)", "Bash(svn.exe *)", "Bash(svnadmin *)"):
            self.assertIn(rule, permissions["deny"])
        for tool in ("Read", "Edit"):
            self.assertTrue(any(r.startswith(f"{tool}(") and "SvnHelper" in r for r in permissions["deny"]), tool)
            self.assertTrue(any(r.startswith(f"{tool}(") and "Subversion" in r and "auth" in r
                                for r in permissions["deny"]), tool)
        self.assertFalse(APPLY_COMMANDS & {m for r in permissions["allow"] for m in re.findall(r"svnlite ([a-z-]+)", r)})

    def test_write_is_documented_as_off_by_default_with_two_steps(self):
        for name in ("AGENT_RULES.md", "SKILL.md", "INSTALL_AGENT.md"):
            text = read_doc(name).lower()
            self.assertIn("выключена", text, name)
            self.assertIn("план", text, name)
            self.assertIn("согласи", text, name)
        install = read_doc("INSTALL_AGENT.md")
        for fragment in ("write_confirm", "write_full", "read_only", "rollback", "branches", "SVNLITE_DATA_DIR",
                         "agent_policy", "12 месяцев"):
            self.assertIn(fragment, install)
        self.assertNotIn("Bash(svnlite:*)", read_doc("SKILL.md").split("---")[1])

    def test_no_files_for_working_copies(self):
        install = read_doc("INSTALL_AGENT.md")
        self.assertIn(".claude/", install)
        self.assertIn("рабочие копии проектов", install)


if __name__ == "__main__":
    unittest.main()
