"""Task U4.6 (чистая часть): `svnhelper.next_step` - «что делать дальше» без окна.

Таблица «факты о проекте -> код, ключ текста, подсвеченная кнопка, безопасность щелчка, код для ops» для всех кодов
раздела 6 docs/PLAN_ui_v4.md; сверка с прежними `main_action`, `ops._next_action` и строками «Что сделать»."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svnhelper import localrepo, messages, next_step as ns  # noqa: E402
from svnhelper.next_step import Facts, next_step  # noqa: E402
from svnhelper.svn_core import LocalChange, WcStatus  # noqa: E402


def change(path="Рама.cdw", kind="modified", needs_lock=False, locked_by_me=False):
    return LocalChange(path, kind, locked_by_me=locked_by_me, needs_lock=needs_lock)


def status(**kwargs):
    """WcStatus; свои правки можно задать списком local_changes (modified и unversioned тогда считаются сами)."""
    return WcStatus(**kwargs)


def svn(st=None, **kwargs):
    return Facts(state="svn", status=st, **kwargs)


FILES = ["a.txt", "b.txt", "c.txt"]
ONE = [change()]
LOCK_NEEDED = [change(needs_lock=True)]
LOCK_TAKEN = [change(needs_lock=True, locked_by_me=True)]

# (название, факты, код, ключ текста, кнопка, безопасно ли нажимать по щелчку, код ops)
TABLE = [
    # вход и папка проектов: глобальные дела, важнее всего
    ("вход не выполнен", svn(status(), login_failed=True), "login", "login", "login", True, "nothing"),
    ("вход важнее конфликта", svn(status(conflicts=1), login_failed=True), "login", "login", "login", True,
     "human_resolve_conflict"),
    ("папка проектов не найдена", svn(status(), folder_missing=True), "no_folder", "no_folder", "choose_folder",
     True, "nothing"),
    ("проект не выбран, папки нет", Facts(folder_missing=True), "no_folder", "no_folder", "choose_folder", True,
     "nothing"),
    # проект с сервера
    ("конфликт", svn(status(conflicts=1)), "conflict", "conflict", "resolve", True, "human_resolve_conflict"),
    ("конфликт важнее нового и правок", svn(status(conflicts=1, incoming=2, local_changes=ONE)), "conflict",
     "conflict", "resolve", True, "human_resolve_conflict"),
    ("проверка не удалась", svn(None, error="Ошибка"), "error", "error", "update", False, "nothing"),
    ("папка повреждена", Facts(state="broken"), "broken", "broken", "repair", False, "human_resolve_conflict"),
    ("переход не закончен", svn(status(switched=2)), "switch", "switch", "update", False, "nothing"),
    ("переход не закончен важнее нового", svn(status(switched=1, incoming=1)), "switch", "switch", "update", False,
     "get_new"),
    ("новое у коллег", svn(status(incoming=3, incoming_files=FILES)), "get_new", "get_new", "update", False,
     "get_new"),
    ("новое и свои правки: сначала получить", svn(status(incoming=1, local_changes=ONE)), "get_new",
     "get_new_with_changes", "update", False, "get_new"),
    ("свои правки", svn(status(local_changes=ONE)), "save", "save", "commit", False, "save"),
    ("только новый файл (его нет на сервере)", svn(status(local_changes=[change("Новый.txt", "unversioned")])),
     "save", "save", "commit", False, "save"),
    ("файл изменён, но не занят", svn(status(local_changes=LOCK_NEEDED)), "need_lock", "need_lock", "lock", True,
     "take_lock"),
    ("файл изменён и занят вами", svn(status(local_changes=LOCK_TAKEN)), "save", "save", "commit", False, "save"),
    ("правки в версии", svn(status(local_changes=ONE), read_only=True), "tag_changes", "tag_changes", "update",
     False, "nothing"),
    ("нужный файл занят другим", svn(status(), needed_locked=("Рама.cdw", "Иванов", "10:20")), "locked_other",
     "locked_other", None, False, "nothing"),
    ("правки важнее занятого другим", svn(status(local_changes=ONE), needed_locked=("Рама.cdw", "Иванов", "10:20")),
     "save", "save", "commit", False, "save"),
    # только на этом компьютере
    ("обычная папка", Facts(state="plain"), "publish", "publish", "publish", True, "nothing"),
    ("папка с меткой проекта", Facts(state="plain", local_only=True), "publish", "publish", "publish", True,
     "send_to_server"),
    ("обычная папка без связи", Facts(state="plain", offline=True), "publish", "publish_offline", "publish", True,
     "nothing"),
    ("история на компьютере: всё сохранено", svn(status(), local=True), "publish", "publish_local", "publish", True,
     "nothing"),
    ("история на компьютере: правки", svn(status(local_changes=ONE), local=True), "save", "save_local", "commit",
     False, "save"),
    ("история потеряна, не найдена", svn(None, local=True, history_missing=True), "publish",
     "publish_history_lost", "publish", True, "human_resolve_conflict"),
    ("история потеряна, найдена", svn(None, local=True, history_missing=True, history_found=True), "history",
     "history", "restore", False, "human_resolve_conflict"),
    # проверка и связь
    ("проверка не запускалась", svn(None), "check", "check", "update", False, "nothing"),
    ("проверка идёт", svn(None, checking=True), "checking", "checking", "update", False, "nothing"),
    ("нет связи", svn(None, error="Нет связи", offline=True), "offline", "offline", "update", False, "nothing"),
    ("идёт операция", svn(status(local_changes=ONE), busy=True), "busy", "busy", "commit", False, "wait"),
    # всё в порядке
    ("всё в порядке", svn(status()), "nothing", "nothing", "update", False, "nothing"),
    ("версия, делать нечего", svn(status(), read_only=True), "read_only", "read_only", "update", False, "nothing"),
    ("проект не выбран", Facts(), "none", "none", None, False, "nothing"),
    # проблема другого проекта
    ("у другого проекта конфликт", svn(status(), other=("сушилки", "conflict")), "other", "other", None, False,
     "nothing"),
    ("проект не выбран, у другого дело", Facts(other=("сушилки", "save")), "other", "other", None, False, "nothing"),
    ("свои дела важнее чужих", svn(status(local_changes=ONE), other=("сушилки", "conflict")), "save", "save",
     "commit", False, "save"),
    ("без связи подсказка молчит и о чужих", svn(None, error="Нет связи", offline=True, other=("сушилки", "save")),
     "offline", "offline", "update", False, "nothing"),
]


class NextStepTable(unittest.TestCase):
    def test_table(self):
        for name, facts, code, text_key, button, safe, op in TABLE:
            with self.subTest(name):
                step = next_step(facts)
                self.assertEqual((step.code, step.text_key, step.button, step.safe, step.op),
                                 (code, text_key, button, safe, op))

    def test_every_code_is_in_the_table(self):
        self.assertEqual({row[2] for row in TABLE}, set(ns.CODES))
        self.assertEqual(set(ns.PRIORITY), set(ns.CODES))

    def test_quiet_codes(self):
        for name, facts, code, *_rest in TABLE:
            with self.subTest(name):
                step = next_step(facts)
                self.assertEqual(step.quiet, code in ("checking", "offline", "busy") or step.text_key == "publish_offline")

    def test_click_matches_the_safety_rule(self):
        """Щелчок нажимает кнопку, только если у обработчика своё окно; остальное - фокус или переход."""
        expected = {"login": ("invoke", True), "no_folder": ("invoke", True), "conflict": ("invoke", True),
                    "broken": ("focus", False), "history": ("focus", False), "get_new": ("focus", False),
                    "save": ("open_tab", False), "need_lock": ("open_tab_invoke", True),
                    "locked_other": ("open_tab", False), "publish": ("invoke", True),
                    "other": ("select_project", False)}
        seen = set()
        for name, facts, code, *_rest in TABLE:
            if code in expected:
                step = next_step(facts)
                with self.subTest(name):
                    self.assertEqual((step.click, step.safe), expected[code])
                seen.add(code)
        self.assertEqual(seen, set(expected))

    def test_tabs_for_clicks_that_open_a_tab(self):
        tabs = {"save": "changes", "need_lock": "locks", "locked_other": "locks", "switch": "lines",
                "tag_changes": "lines"}
        for name, facts, code, *_rest in TABLE:
            step = next_step(facts)
            with self.subTest(name):
                self.assertEqual(step.tab, tabs.get(code))
                if step.click in ("open_tab", "open_tab_invoke"):
                    self.assertIsNotNone(step.tab)

    def test_priority_follows_the_plan_order(self):
        order = ["login", "no_folder", "conflict", "error", "broken", "get_new", "save", "locked_other", "publish",
                 "check", "other", "nothing"]
        values = [ns.PRIORITY[code] for code in order]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), len(values))
        for same in (("broken", "switch", "history"), ("save", "need_lock", "tag_changes"),
                     ("nothing", "read_only"), ("checking", "offline", "busy")):
            self.assertEqual(len({ns.PRIORITY[code] for code in same}), 1, same)

    def test_priority_is_carried_by_the_step(self):
        conflict = next_step(svn(status(conflicts=1)))
        self.assertEqual(conflict.priority, ns.PRIORITY["conflict"])
        lost = next_step(svn(None, local=True, history_missing=True))        # код publish, но срочность как у history
        self.assertEqual((lost.code, lost.priority), ("publish", ns.PRIORITY["history"]))


class NextStepData(unittest.TestCase):
    def test_numbers_and_names_for_texts(self):
        step = next_step(svn(status(incoming=1, incoming_files=FILES, local_changes=[change(), change("b.txt")],
                                    my_locks=[object(), object()])))
        self.assertEqual((step.data["incoming"], step.data["files"], step.data["with_changes"],
                          step.data["my_locks"]), (3, 2, True, 2))
        step = next_step(svn(status(conflicts=1, local_changes=[change("C:\\p\\Рама.cdw", "conflicted")])))
        self.assertEqual((step.data["conflicts"], step.data["file"]), (1, "Рама.cdw"))
        step = next_step(svn(status(local_changes=[change("p/Узел.dwg", needs_lock=True)])))
        self.assertEqual((step.code, step.data["file"]), ("need_lock", "Узел.dwg"))
        step = next_step(svn(status(), needed_locked=("Рама.cdw", "Иванов", "10:20")))
        self.assertEqual((step.data["file"], step.data["who"], step.data["since"]), ("Рама.cdw", "Иванов", "10:20"))
        step = next_step(svn(status(), other=("сушилки", "conflict")))
        self.assertEqual((step.data["project"], step.data["project_code"]), ("сушилки", "conflict"))

    def test_files_count_ignores_conflicted_entries_and_falls_back_to_counter(self):
        both = status(conflicts=1, local_changes=[change("a", "conflicted"), change("b"), change("c")])
        self.assertEqual(next_step(svn(both)).data["files"], 2)
        self.assertEqual(next_step(svn(status(modified=4))).data["files"], 4)

    def test_incoming_comes_from_the_largest_counter(self):
        self.assertEqual(next_step(svn(status(incoming=0, incoming_files=FILES))).data["incoming"], 3)
        self.assertEqual(next_step(svn(status(incoming=5, incoming_files=FILES[:1]))).data["incoming"], 5)

    def test_server_not_checked_hides_incoming(self):
        step = next_step(svn(status(incoming=2), server_checked=False))
        self.assertEqual((step.code, step.op), ("offline", "nothing"))
        step = next_step(svn(status(local_changes=ONE, incoming=2), server_checked=False))
        self.assertEqual((step.code, step.op), ("save", "save"))

    def test_data_is_not_shared_between_calls(self):
        first = next_step(svn(status(conflicts=1)))
        first.data["x"] = 1
        self.assertNotIn("x", next_step(svn(status(conflicts=1))).data)


class OtherProjects(unittest.TestCase):
    def test_most_important_picks_the_urgent_one(self):
        steps = [("a", next_step(svn(status(local_changes=ONE)))), ("b", next_step(svn(status(conflicts=1)))),
                 ("c", next_step(svn(status()))), ("d", next_step(svn(status(incoming=1))))]
        name, step = ns.most_important(steps)
        self.assertEqual((name, step.code), ("b", "conflict"))

    def test_most_important_ties_keep_the_first(self):
        steps = [("a", next_step(svn(status(local_changes=ONE)))), ("b", next_step(svn(status(local_changes=ONE))))]
        self.assertEqual(ns.most_important(steps)[0], "a")

    def test_most_important_ignores_quiet_and_idle(self):
        steps = [("a", next_step(svn(None, error="Нет связи", offline=True))), ("b", next_step(svn(status()))),
                 ("c", next_step(svn(None))), ("d", next_step(Facts(state="svn", busy=True, status=status(conflicts=1))))]
        self.assertIsNone(ns.most_important(steps))
        self.assertIsNone(ns.most_important([]))

    def test_other_shows_only_when_own_project_is_fine(self):
        other = ("сушилки", "conflict")
        for facts, code in ((svn(status(), other=other), "other"), (svn(status(), read_only=True, other=other), "other"),
                            (Facts(other=other), "other"), (svn(status(incoming=1), other=other), "get_new"),
                            (svn(None, other=other), "check"), (svn(None, checking=True, other=other), "checking"),
                            (Facts(state="plain", other=other), "publish")):
            with self.subTest(code=code, state=facts.state):
                self.assertEqual(next_step(facts).code, code)

    def test_login_and_missing_folder_beat_other_projects(self):
        self.assertEqual(next_step(svn(status(), login_failed=True, other=("x", "conflict"))).code, "login")
        self.assertEqual(next_step(svn(status(), folder_missing=True, other=("x", "conflict"))).code, "no_folder")

    def test_main_button_ignores_global_facts(self):
        """Подсветка проекта не зависит от входа, папки и чужих проектов: так считала прежняя main_action."""
        base = svn(status(local_changes=ONE))
        noisy = svn(status(local_changes=ONE), login_failed=True, folder_missing=True, other=("x", "conflict"))
        self.assertEqual((ns.main_button(base), ns.main_button(noisy)), ("commit", "commit"))


class MainActionParity(unittest.TestCase):
    """Таблицы прежних MainActionTests (tests/test_gui_smoke.py): подсветка та же, кроме одного названного случая."""

    def test_server_projects_like_main_action(self):
        changes = [change()]
        cases = [
            (None, None, False, None),
            ("plain", None, False, "publish"),
            ("broken", None, False, "repair"),
            ("svn", None, False, "update"),
            ("svn", WcStatus(), False, "update"),
            ("svn", WcStatus(incoming=3), False, "update"),
            ("svn", WcStatus(local_changes=changes), False, "commit"),
            ("svn", WcStatus(conflicts=1), False, "resolve"),
            ("svn", WcStatus(conflicts=1, local_changes=changes, incoming=1), False, "resolve"),
            ("svn", WcStatus(local_changes=changes, incoming=1), True, "update"),
        ]
        for state, st, read_only, expected in cases:
            with self.subTest(state=state, read_only=read_only, conflicts=getattr(st, "conflicts", None)):
                self.assertEqual(ns.main_button(Facts(state=state, status=st, read_only=read_only)), expected)

    def test_new_and_own_changes_now_highlights_get_new(self):
        """Расхождение, которое разрешает план (раздел 6): карточка и ops говорили «сначала получить новое», а
        прежняя main_action подсвечивала сохранение. Теперь везде «Получить новое»."""
        st = WcStatus(local_changes=[change()], incoming=1)
        self.assertEqual(ns.main_button(svn(st)), "update")
        self.assertEqual(next_step(svn(st)).op, "get_new")

    def test_local_history_like_main_action(self):
        changes = [change()]
        cases = [(None, False, False, "commit"), (WcStatus(), False, False, "publish"),
                 (WcStatus(local_changes=changes), False, False, "commit"),
                 (None, True, True, "restore"), (WcStatus(), True, True, "restore"),
                 (None, True, False, "publish"), (WcStatus(local_changes=changes), True, False, "publish")]
        for st, missing, found, expected in cases:
            with self.subTest(missing=missing, found=found, changes=bool(getattr(st, "local_changes", None))):
                facts = Facts(state="svn", status=st, local=True, history_missing=missing, history_found=found)
                self.assertEqual(ns.main_button(facts), expected)


class OpsParity(unittest.TestCase):
    """Те же случаи, что `StatusTests.test_states_and_next_actions` и соседние в tests/test_ops_read.py: код ops прежний."""

    def test_status_cases(self):
        cases = [
            (status(), "nothing"),
            (status(incoming=3, incoming_files=FILES), "get_new"),
            (status(local_changes=[change("a.txt")]), "save"),
            (status(local_changes=[change("a.txt", needs_lock=True)]), "take_lock"),
            (status(local_changes=[change("a.txt", needs_lock=True, locked_by_me=True)]), "save"),
            (status(local_changes=[change("a.txt")], incoming=2, incoming_files=FILES[:2]), "get_new"),
            (status(conflicts=1, local_changes=[change("a.txt", "conflicted")]), "human_resolve_conflict"),
        ]
        for st, expected in cases:
            with self.subTest(expected=expected, incoming=st.incoming):
                self.assertEqual(next_step(svn(st)).op, expected)

    def test_tag_with_changes_is_not_saved(self):
        self.assertEqual(next_step(svn(status(local_changes=[change("a.txt")]), read_only=True)).op, "nothing")

    def test_plain_local_only_broken_and_busy(self):
        self.assertEqual(next_step(Facts(state="plain")).op, "nothing")
        self.assertEqual(next_step(Facts(state="plain", local_only=True)).op, "send_to_server")
        self.assertEqual(next_step(Facts(state="broken")).op, "human_resolve_conflict")
        self.assertEqual(next_step(svn(None, local=True, history_missing=True)).op, "human_resolve_conflict")
        self.assertEqual(next_step(svn(status(conflicts=1), busy=True)).op, "wait")

    def test_unchecked_server_is_nothing_but_local_changes_still_save(self):
        self.assertEqual(next_step(svn(status(), server_checked=False)).op, "nothing")        # unknown_offline
        self.assertEqual(next_step(svn(status(local_changes=ONE), server_checked=False)).op, "save")

    def test_switch_does_not_change_the_ops_code(self):
        """Ops о незаконченном переходе не знает: код считается как без него."""
        self.assertEqual(next_step(svn(status(switched=1, local_changes=ONE))).op, "save")
        self.assertEqual(next_step(svn(status(switched=1, incoming=1))).op, "get_new")

    def test_ops_next_action_goes_through_next_step(self):
        """U4.6: ops `_next_action` - перевод кода next_step по таблице; виды папок ops дают прежние коды."""
        from svnhelper.ops import projects as ops
        self.assertEqual(set(ops.NEXT_ACTIONS), {v for k, v in vars(ns).items() if k.startswith("OP_")})
        self.assertEqual(set(ops.NEXT_ACTIONS.values()), {"nothing", "get_new", "save", "take_lock",
                                                          "human_resolve_conflict", "send_to_server", "wait"})
        cases = [(ops.KIND_PLAIN, None, False, False, False, "nothing"),
                 (ops.KIND_LOCAL_ONLY, None, False, False, False, "send_to_server"),
                 (ops.KIND_BROKEN, None, False, False, False, "human_resolve_conflict"),
                 (ops.KIND_HISTORY_MISSING, None, False, False, False, "human_resolve_conflict"),
                 (ops.KIND_LOCAL_HISTORY, status(local_changes=ONE), False, False, False, "save"),
                 (ops.KIND_LOCAL_HISTORY, status(), False, False, False, "nothing"),
                 (ops.KIND_PROJECT, None, True, False, False, "wait"),           # папку держит другая программа
                 (ops.KIND_PROJECT, status(incoming=2), False, False, False, "nothing"),   # сервер не проверялся
                 (ops.KIND_PROJECT, status(incoming=2, local_changes=ONE), False, False, True, "get_new"),
                 (ops.KIND_PROJECT, status(local_changes=LOCK_NEEDED), False, False, True, "take_lock"),
                 (ops.KIND_PROJECT, status(local_changes=ONE), False, True, True, "nothing")]   # версия
        for kind, st, busy, read_only, checked, expected in cases:
            with self.subTest(kind=kind, expected=expected):
                self.assertEqual(ops._next_action(kind, st, busy, read_only, checked), expected)

    def test_ops_codes_are_known_values(self):
        known = {"nothing", "get_new", "save", "take_lock", "human_resolve_conflict", "send_to_server", "wait"}
        for name, facts, *_rest in TABLE:
            self.assertIn(next_step(facts).op, known, name)


class CardParity(unittest.TestCase):
    """Строки «Что сделать» (messages.status_line, localrepo.status_line) говорят то же, что код next_step."""

    PHRASES = {"conflict": "«Разобрать конфликт»", "get_new_with_changes": "Сначала «Получить новое»",
               "get_new": "«Получить новое»", "save": "«Сохранить отмеченное»", "nothing": "Ничего делать не нужно",
               "check": "«⟳»", "checking": "Идёт проверка", "broken": "«Починить папку»", "publish": "«Отправить на сервер…»"}

    def test_server_project_card_matches_code(self):
        cases = [(WcStatus(conflicts=1), "conflict"), (WcStatus(incoming=1, modified=1), "get_new_with_changes"),
                 (WcStatus(incoming=1), "get_new"), (WcStatus(modified=1, local_changes=ONE), "save"),
                 (WcStatus(), "nothing"), (None, "check")]
        for st, key in cases:
            with self.subTest(key=key):
                step = next_step(svn(st))
                self.assertEqual(step.text_key, key)
                self.assertIn(self.PHRASES[key], messages.status_line("svn", st, False)[2])
        self.assertIn(self.PHRASES["checking"], messages.status_line("svn", None, True)[2])
        self.assertEqual(next_step(svn(None, checking=True)).code, "checking")

    def test_other_states_card_matches_code(self):
        self.assertIn(self.PHRASES["broken"], messages.status_line("broken", None, False)[2])
        self.assertEqual(next_step(Facts(state="broken")).code, "broken")
        self.assertIn(self.PHRASES["publish"], messages.status_line("plain", None, False)[2])
        self.assertEqual(next_step(Facts(state="plain")).code, "publish")

    def test_local_history_card_matches_code(self):
        cases = [(WcStatus(modified=1, local_changes=ONE), False, False, "save", "«Сохранить у себя»"),
                 (WcStatus(), False, False, "publish", "отправить проект на сервер"),
                 (None, False, False, "check", "«⟳»"),
                 (None, True, True, "history", "«Восстановить историю»"),
                 (None, True, False, "publish", "«Отправить на сервер…»")]
        for st, missing, found, code, phrase in cases:
            with self.subTest(code=code, missing=missing, found=found):
                step = next_step(Facts(state="svn", status=st, local=True, history_missing=missing,
                                       history_found=found))
                self.assertEqual(step.code, code)
                self.assertIn(phrase, localrepo.status_line(st, False, missing, found)[2])

    def test_unversioned_only_is_a_change_for_all_sources(self):
        """Расхождение: у нового файла без правок остальных `status.modified` равен 0, и карточка писала «Актуален», а
        подсветка и ops уже предлагали сохранить. next_step считает правкой любой элемент local_changes, и с U4.6
        карточка берёт код у него же."""
        st = WcStatus(unversioned=1, local_changes=[change("Новый.txt", "unversioned")])
        self.assertEqual(messages.status_line("svn", st, False)[::2],
                         ("Есть несохранённые правки", "Нажмите «Сохранить отмеченное»"))
        step = next_step(svn(st))
        self.assertEqual((step.code, step.button, step.op), ("save", "commit", "save"))


class StateLine(unittest.TestCase):
    """U4.6: messages.state_line - строка «Что сделать» карточки и таблицы по коду next_step, с поправками окна."""

    def test_every_project_row_of_the_table_has_words(self):
        """У всех строк таблицы, которые описывают сам проект, есть состояние и подсказка."""
        for name, facts, code, *_rest in TABLE:
            if code in ("login", "no_folder", "other", "none", "busy", "locked_other"):
                continue        # дела окна целиком, чужих проектов и занятой папки в строку проекта не попадают
            with self.subTest(name):
                text, tag, todo = messages.state_line(facts)
                self.assertTrue(text and todo, (text, todo))
                self.assertIn(tag, ("ok", "new", "bad", "plain", "frozen"))

    def test_window_corrections(self):
        cases = [
            (svn(status(local_changes=ONE), read_only=True), False, ("Есть несохранённые правки", "new",
                                                                    messages.TAG_MODIFIED)),
            (svn(status(local_changes=ONE), read_only=True), True, ("Есть несохранённые правки", "new",
                                                                   messages.TAG_MODIFIED_SHORT)),
            (svn(status(), read_only=True), False, ("Актуален", "frozen", messages.TAG_TODO)),
            (svn(status(), read_only=True), True, ("Актуален", "frozen", "Только для чтения")),
            (svn(None, read_only=True), False, ("Проект с сервера", "frozen", "Нажмите «⟳»")),
            (svn(status(switched=1, local_changes=ONE, incoming=1)), False,
             (messages.SWITCH_UNFINISHED, "bad", messages.SWITCH_UNFINISHED_TODO)),
            (svn(None, error="Нет связи", offline=True), False,
             ("Нет связи", "plain", "Сохранить получится, когда связь появится")),
            (svn(None, error="Нет связи", offline=True, local=True), False,
             ("Нет связи", "plain", "Сохранить получится, когда связь появится")),
            (svn(None, error="Сбой", checking=True), False, ("Проект с сервера", "ok", "Идёт проверка…")),
            (svn(None, error="Сбой"), False, ("Сбой", "bad", "Нажмите «⟳»")),
            (svn(None, error="Сбой", local=True, history_missing=True), False,
             (localrepo.MISSING_TEXT, "bad", localrepo.NOT_FOUND_TODO)),
            (Facts(state="plain", offline=True), False, (messages.PLAIN_TEXT, "plain", messages.PLAIN_OFFLINE_TODO)),
        ]
        for facts, short, expected in cases:
            with self.subTest(expected=expected, short=short):
                self.assertEqual(messages.state_line(facts, short=short), expected)

    def test_line_ignores_window_wide_facts(self):
        """Вход, папка проектов, занятость папки и чужие проекты - не про этот проект: строка та же."""
        plain = messages.state_line(svn(status(local_changes=ONE)))
        noisy = messages.state_line(svn(status(local_changes=ONE), login_failed=True, folder_missing=True, busy=True,
                                        other=("x", "conflict"), needed_locked=("a", "b", "c")))
        self.assertEqual(noisy, plain)

    def test_my_locks_reminder_only_for_server_projects(self):
        locks = [object(), object()]
        self.assertTrue(messages.state_line(svn(status(my_locks=locks)))[2].endswith(
            "Не забудьте освободить занятые файлы (2)"))
        self.assertNotIn("освободить", messages.state_line(svn(status(my_locks=locks), read_only=True))[2])
        self.assertNotIn("освободить", messages.state_line(svn(status(my_locks=locks, switched=1)))[2])


class StepTexts(unittest.TestCase):
    """U4.6: тексты по ключу NextStep.text_key (messages.NEXT_STEP_TEXTS, они же hints.NEXT_STEP_TEXTS)."""

    def test_every_text_key_has_texts(self):
        for name, facts, _code, text_key, *_rest in TABLE:
            if text_key in ("none", "busy"):
                continue                 # проект не выбран, идёт операция: панель молчит, строки проекта нет
            with self.subTest(name):
                entry = messages.NEXT_STEP_TEXTS[text_key]
                self.assertTrue(entry.short and entry.detailed, text_key)
        self.assertLessEqual(set(messages.OTHER_PROBLEMS), set(messages.NEXT_STEP_TEXTS))

    def test_numbers_and_names_are_filled_in(self):
        step = next_step(svn(status(incoming=3, incoming_files=FILES)))
        self.assertEqual(messages.step_text(step), "У коллег новое: 3 файла. Нажмите «Получить новое» слева: ваши "
                                                   "файлы не пострадают.")
        step = next_step(svn(status(local_changes=[change(), change("b.txt")])))
        self.assertTrue(messages.step_text(step).startswith("Не сохранено: 2 файла."))
        step = next_step(svn(status(local_changes=[change("p/Узел.dwg", needs_lock=True)])))
        self.assertTrue(messages.step_text(step).startswith("«Узел.dwg» изменён, но не занят"))
        step = next_step(svn(status(conflicts=1, local_changes=[change("Рама.cdw", "conflicted")])))
        self.assertTrue(messages.step_text(step).startswith("Конфликт в «Рама.cdw»: "))
        step = next_step(svn(status(), needed_locked=("Рама.cdw", "Иванов", "с 10:20")))
        self.assertEqual(messages.step_text(step, "short"), "«Рама.cdw» занят: Иванов, с 10:20. Попросите освободить его")
        step = next_step(svn(status(), other=("сушилки", "conflict")))
        self.assertEqual(messages.step_text(step, "short"), "В проекте «сушилки»: конфликт")
        step = next_step(Facts(folder_missing=True))
        self.assertEqual(messages.step_text(step, "short", folder="Z:\\Проекты"), "Папка «Z:\\Проекты» не найдена")

    def test_card_and_panel_use_one_entry(self):
        """Строка «Что сделать» карточки - поле short того же ключа, что подробный текст панели."""
        st = status(incoming=1, local_changes=ONE)
        step = next_step(svn(st))
        self.assertEqual(messages.state_line(svn(st))[2], messages.step_text(step, "short"))
        self.assertEqual(messages.state_line(svn(st))[0], messages.NEXT_STEP_TEXTS[step.text_key].state)


class PureModule(unittest.TestCase):
    def test_module_does_not_import_tkinter(self):
        """В чистом интерпретаторе после `import svnhelper.next_step` в sys.modules нет tkinter и окна."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import svnhelper.next_step; "
                "bad = sorted(m for m in sys.modules if m == 'tkinter' or m.startswith(('tkinter.', 'svnhelper.gui'))); "
                "print(bad); sys.exit(1 if bad else 0)")
        done = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_module_has_no_runtime_dependencies_on_the_rest_of_the_program(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import svnhelper.next_step; "
                "bad = sorted(m for m in sys.modules if m.startswith('svnhelper.') and m != 'svnhelper.next_step'); "
                "print(bad); sys.exit(1 if bad else 0)")
        done = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_facts_and_steps_are_immutable(self):
        facts = Facts(state="svn")
        with self.assertRaises(Exception):
            facts.state = "plain"
        with self.assertRaises(Exception):
            next_step(facts).code = "x"


if __name__ == "__main__":
    unittest.main()
