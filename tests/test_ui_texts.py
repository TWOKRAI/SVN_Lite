"""Тексты для человека не должны содержать терминов svn (commit, update, revision, lock, checkout, trunk, Clean up, diff).

Сканер обходит дерево каждого модуля svnhelper и берёт все строки с кириллицей, кроме docstring и аргументов log.*.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FORBIDDEN = re.compile(r"commit|update|revision|lock|checkout|trunk|clean ?up|diff", re.IGNORECASE)
# русские слова из той же группы (T.3, T.5, T.6): «ревизия», «репозиторий», «svnadmin» в новых строках не допускаются
FORBIDDEN_RU = re.compile(r"ревизи|репозитор|svnadmin", re.IGNORECASE)
# слова «хранилище» и «SVN» (регистр у SVN важен: «svn.exe», «свойство svn:needs-lock» бывают в именах файлов) не
# допускаются ни в одной строке интерфейса: человеку они ничего не говорят (ревью 2026-11-02, п.7)
FORBIDDEN_WORDS = re.compile(r"хранилищ|SVN", re.IGNORECASE)
SVN_WORD = re.compile(r"SVN")
STORAGE_WORD = re.compile(r"хранилищ", re.IGNORECASE)
# модули, написанные с этим правилом с самого начала; ревизия/репозиторий/svnadmin проверяются только в них
RU_CHECKED = ("gui/new_project_dialog.py", "newproject.py", "gui/server_tree.py", "localrepo.py", "gui/projects_tab.py",
              "gui/project_panel.py", "gui/agent_dialog.py", "gui/hints.py", "gui/icons.py")
RU_CHECKED_DIRS = ("ops/",)      # слой ops для ИИ-помощника (AG.1): все его модули, в том числе будущие
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# что разрешено оставить в тексте: названия программ, адреса и ключи для администратора
# (commit_templates и update_path - ключи svn_helper.json, их называют в сообщениях администратору)
# «Проекты_SVN» - пример имени папки в подсказке для администратора (поле mirrors в svn_helper.json)
ALLOWED = ("SVN-lite", "TortoiseSVN", "svn.exe", "svn://", "update_path", "commit_templates", "Коды svn",
           "Ответ svn", "Проекты_SVN")


def mock_create_error(rolled_back):
    from svnhelper import importer
    from svnhelper.svn_core import ErrorKind, SvnError
    return importer.CreateError(SvnError(ErrorKind.AUTHZ, "svn: E220004"), "import", rolled_back=rolled_back)


def _log_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id in ("log", "logging"))


def user_strings(tree: ast.AST):
    """(строка, текст) для каждой строки программы с кириллицей; f-строка берётся целиком."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                skip.add(id(body[0].value))
        if _log_call(node):
            skip.update(id(sub) for sub in ast.walk(node))
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            skip.update(id(v) for v in node.values)
            text = "".join(parts)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        else:
            continue
        if CYRILLIC.search(text):
            yield node.lineno, text


class UiTextsTests(unittest.TestCase):
    def test_no_svn_terms_in_user_texts(self):
        found = []
        for path in sorted((ROOT / "svnhelper").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, text in user_strings(tree):
                cleaned = text
                for ok in ALLOWED:
                    cleaned = cleaned.replace(ok, "")
                relative = path.relative_to(ROOT / "svnhelper").as_posix()
                ru_checked = relative in RU_CHECKED or relative.startswith(RU_CHECKED_DIRS)
                if (FORBIDDEN.search(cleaned) or SVN_WORD.search(cleaned) or STORAGE_WORD.search(cleaned)
                        or (ru_checked and FORBIDDEN_RU.search(cleaned))):
                    found.append(f"{path.relative_to(ROOT)}:{lineno}: {text[:100]!r}")
        self.assertEqual(found, [], "термины svn в текстах для человека:\n" + "\n".join(found))

    def test_local_history_texts_are_plain_russian(self):
        """T.6: тексты локальной истории (в том числе собираемые на ходу) без терминов svn и слов «ревизия», «репозиторий»."""
        from svnhelper import localrepo
        from svnhelper.svn_core import WcStatus
        texts = [v for k, v in vars(localrepo).items() if k.isupper() and isinstance(v, str) and CYRILLIC.search(v)]
        texts += list(localrepo.MESSAGES.values())
        texts += [localrepo.saved_text(3), localrepo.auto_comment()]
        for status, missing in ((None, False), (WcStatus(), False), (WcStatus(modified=1), False), (None, True)):
            texts += list(localrepo.status_line(status, False, missing))
        self.assertGreater(len(texts), 20)
        found = []
        for text in texts:
            cleaned = text
            for ok in ALLOWED:
                cleaned = cleaned.replace(ok, "")
            if FORBIDDEN.search(cleaned) or FORBIDDEN_RU.search(cleaned):
                found.append(text[:80])
        self.assertEqual(found, [])
        for button in (localrepo.SAVE_BUTTON, localrepo.PUBLISH_BUTTON, localrepo.CONTINUE_BUTTON):
            self.assertTrue(CYRILLIC.search(button))
        self.assertEqual((localrepo.SAVE_BUTTON, localrepo.CONTINUE_BUTTON, localrepo.WHERE_TEXT),
                         ("Сохранить у себя", "Продолжить без истории", "Хранится только на этом компьютере"))

    def test_journal_reliability_texts_are_plain_russian(self):
        """Ревью 2026-11-02, этап A: откат не удался, нет связи, чей каркас неизвестно - без терминов svn и без
        «хранилище»."""
        from svnhelper import publish
        from svnhelper.gui import new_project_dialog as npd
        texts = [publish.ROLLBACK_LATER_TEXT, publish.RECOVER_RETRY_TEXT, npd.NO_LINK_KEEP_TEXT,
                 publish.unknown_owner_text("svn://srv/konstruktor/projects/261028_%D0%B2"),
                 npd.undo_text(False), npd.publish_error_text(publish.PublishError(
                     publish.NO_RIGHTS, "upload", create_error=mock_create_error(rolled_back=False)))]
        found = [t for t in texts if FORBIDDEN.search(t) or FORBIDDEN_RU.search(t) or re.search("хранилищ", t, re.I)]
        self.assertEqual(found, [])
        self.assertIn("при следующем запуске программы", publish.ROLLBACK_LATER_TEXT)
        self.assertIn("«Отправить на сервер…»", npd.NO_LINK_KEEP_TEXT)

    def test_lost_history_and_plain_folder_texts(self):
        """Ревью 2026-11-02, п.5 и 7: потерянная история, «Только на этом компьютере», отправка без истории,
        примеры ссылок, откат создания: без «хранилище», «SVN», терминов svn и слов «ревизия», «репозиторий»."""
        from svnhelper import localrepo, messages, publish, server
        from svnhelper.svn_core import ErrorKind, WcStatus
        texts = [localrepo.RESTORE_BUTTON, localrepo.RESTORE_DONE, localrepo.RESTORING_TEXT, localrepo.CONTINUE_ASK,
                 localrepo.CONTINUE_ASK_FOUND, localrepo.NOT_FOUND_TEXT, localrepo.NOT_FOUND_TODO, localrepo.SAVE_NOW_ASK,
                 localrepo.MESSAGES[localrepo.HISTORY_NOT_FOUND], localrepo.MESSAGES[localrepo.ALREADY_VERSIONED],
                 publish.MESSAGES[publish.LOCAL_HISTORY_MISSING], messages.PLAIN_TEXT, messages.PLAIN_TODO,
                 messages.PLAIN_OFFLINE_TODO, messages.create_undo_text(True), messages.create_undo_text(False, "x"),
                 messages.LINES_PICK_PROJECT, server.example_url(""), server.example_url("svn://srv.local", ["proj"]),
                 *messages.status_line("plain", None, False), *messages.status_line("svn", None, False),
                 *localrepo.status_line(None, False, True, found=True), *localrepo.status_line(None, False, True)]
        for text in (WcStatus(), WcStatus(modified=1)):
            texts += list(localrepo.status_line(text, False, False))
        for text in ("", "http:/oops", "/без_схемы"):
            try:
                server.parse_project_url(text, "svn://srv.local")
            except ValueError as exc:
                texts.append(str(exc))
        found = []
        for text in texts:
            cleaned = text
            for ok in ALLOWED:
                cleaned = cleaned.replace(ok, "")
            if (FORBIDDEN.search(cleaned) or FORBIDDEN_RU.search(cleaned) or SVN_WORD.search(cleaned)
                    or STORAGE_WORD.search(cleaned)):
                found.append(text[:80])
        self.assertEqual(found, [])
        self.assertIn("Только на этом компьютере", messages.PLAIN_TEXT)
        self.assertEqual(localrepo.RESTORE_BUTTON, "Восстановить историю")
        self.assertIn("История этого проекта на компьютере не найдена", localrepo.NOT_FOUND_TODO)
        self.assertNotIn("нельзя", publish.MESSAGES[publish.LOCAL_HISTORY_MISSING])        # отправка при этом возможна
        self.assertNotIn("192.168", server.example_url(""))                                  # боевой адрес в примере не зашит
        self.assertTrue(server.example_url("svn://srv.local", ["proj"]).startswith("svn://srv.local/proj/"))

    def test_agent_dialog_and_versions_texts_are_plain_russian(self):
        """AG.7: окно «ИИ-помощник…», его пометки состояния и фраза doctor про разные версии: без терминов svn."""
        from svnhelper.gui import agent_dialog
        from svnhelper.ops import texts
        texts_list = [agent_dialog.TITLE, agent_dialog.INTRO, agent_dialog.NOT_YET_TEXT, agent_dialog.FOLDERS_FIXED_TEXT,
                      agent_dialog.FOLDERS_EXTRA_TEXT, agent_dialog.FOLDERS_LOCKED_TEXT, agent_dialog.COPIED_CODE_TEXT,
                      agent_dialog.COPIED_DESKTOP_TEXT, agent_dialog.CLI_MISSING_TEXT, agent_dialog.SAVE_ERROR_TEXT,
                      agent_dialog.LOG_FOLDER_TEXT, agent_dialog.FOLDERS_CHANGED_TEXT, agent_dialog.FOLDER_NETWORK_TEXT,
                      texts.VERSIONS_DIFFER.format(window="1.0.0", cli="1.0.1"),
                      texts.VERSIONS_DIFFER_PENDING.format(window="1.0.0", cli="1.0.1"),
                      *agent_dialog.CEILING_TEXTS.values(), *agent_dialog.SIGNATURE_TEXTS.values(),
                      *(agent_dialog.PERMISSION_PREFIX + label for _action, label in agent_dialog.PERMISSIONS),
                      texts.POLICY_TAMPERED, texts.POLICY_FOREIGN, texts.POLICY_UNREADABLE,
                      *(texts.policy_mode_text(mode) for mode in ("off", "read_only", "write"))]
        self.assertGreater(len(texts_list), 20)
        found = []
        for text in texts_list:
            cleaned = text
            for ok in ALLOWED:
                cleaned = cleaned.replace(ok, "")
            if (FORBIDDEN.search(cleaned) or FORBIDDEN_RU.search(cleaned) or SVN_WORD.search(cleaned)
                    or STORAGE_WORD.search(cleaned)):
                found.append(text[:80])
        self.assertEqual(found, [])
        self.assertEqual(len(agent_dialog.PERMISSIONS), 6)
        self.assertTrue(all(text.startswith("Разрешить помощнику: ") for text in
                            (agent_dialog.PERMISSION_PREFIX + label for _a, label in agent_dialog.PERMISSIONS)))

    def test_forbidden_words_are_caught(self):
        tree = ast.parse('a = "Проект в хранилище"\nb = "Подключён к SVN"\nc = "Хранилище возвращено"\n'
                         'd = "SVN-lite и TortoiseSVN можно"\n')
        caught = []
        for _n, text in user_strings(tree):
            cleaned = text
            for ok in ALLOWED:
                cleaned = cleaned.replace(ok, "")
            if SVN_WORD.search(cleaned) or STORAGE_WORD.search(cleaned):
                caught.append(text)
        self.assertEqual(caught, ["Проект в хранилище", "Подключён к SVN", "Хранилище возвращено"])

    def test_russian_terms_are_caught_in_new_texts(self):
        tree = ast.parse('x = "Ревизия проекта"\ny = "Создать репозиторий"\nz = "Запустите svnadmin"\n')
        self.assertEqual(len([t for _n, t in user_strings(tree) if FORBIDDEN_RU.search(t)]), 3)

    def test_scanner_catches_terms(self):
        tree = ast.parse('x = "Сохранить (Commit)"\ny = f"Нужно {n} update"\nlog.info("Обновление update")\n'
                         'def f():\n    """Обновление update"""\n')
        self.assertEqual([t for _n, t in user_strings(tree)], ["Сохранить (Commit)", "Нужно  update"])


if __name__ == "__main__":
    unittest.main()
