"""Task T.7: сквозные тесты проекта из шаблона на настоящем svn (file:///).

Сценарии целиком: шаблон на «сервере» -> проект на компьютере с историей -> правка и «сохранить у себя» ->
отправка на сервер -> второй пользователь скачивает; отдельно «сразу на сервер». Затем отмена и «убитый процесс»
на каждом шаге T.3-T.6, спецсимволы в названии проекта на каждой команде, длинный путь, шаблон с svn:keywords.

Убитый процесс имитируется исключением BaseException (оно проходит мимо `except Exception`); для шагов, где код
ловит и BaseException (сборка папки), заодно отключается уборка (`killed_process`): на диске остаётся то же,
что после настоящего убийства. Ожидания только по условию, жёстких таймаутов нет.
"""
import gc
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from itertools import count
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish as tp  # noqa: E402
import test_templates as tt  # noqa: E402
from svnhelper import config, localrepo, projectmeta, publish, server, templates  # noqa: E402
from svnhelper.config import FilterRules, SettingsStore  # noqa: E402
from svnhelper.gui import new_project_dialog as npd  # noqa: E402
from svnhelper.svn_core import CancelToken, CheckoutProgress, ErrorKind, SvnClient, SvnError  # noqa: E402

Killed = tp.Killed
SVNADMIN = shutil.which("svnadmin")
RULES = localrepo.local_rules(FilterRules())
TODAY = date(2026, 10, 28)
TEMPLATE_ID = "Шаблон проекта @1"
KEYWORD_FILE = "версия.txt"
KEYWORD_BYTES = b"$Id$ and $Date$ $Revision$\r\n"
BIN_FILE = "Схема узла.bin"
BIN_BYTES = b"\x00\x01" * 1000
README_OLD = "README.txt"
XLSX_OLD = "docs/Specification/Шаблон каталог спецификация электроника.xlsx"
NAMES = ["вафли @ 100%", "вафли@", "50%41 вафли"]      # '@' внутри и в конце, '%' и «%41» (похоже на закодированный 'A')
HAS_SVN = tp.HAS_SVN and os.name == "nt"
REASON = "нужны Windows, svn и svnadmin"

_shared = None


def setUpModule():
    """Мусор окон от прежних модулей собирается здесь, в главном потоке (иначе его мог бы собрать поток svn), затем
    один раз создаётся «сервер» с шаблоном: он только читается, у каждого теста свои место для проектов и папки."""
    gc.collect()
    global _shared
    if HAS_SVN:
        _shared = SharedServer()


def tearDownModule():
    global _shared
    if _shared is not None:
        _shared.close()
        _shared = None


class SharedServer:
    """Хранилище с папкой шаблонов: `Шаблон проекта @1` (пустые папки, docx/xlsx-подобные файлы, .bin, файл с
    svn:keywords) и обычная папка без template.json."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "server repo"
        tp.run("svnadmin", "create", str(self.repo))
        self.url = self.repo.as_uri()
        self.src = tt.make_template(root / "tpl_src" / TEMPLATE_ID)      # чистая копия: svn её не трогает
        (self.src / BIN_FILE).write_bytes(BIN_BYTES)
        (self.src / KEYWORD_FILE).write_bytes(KEYWORD_BYTES)
        wc = root / "seed"
        tp.run("svn", "checkout", self.url, str(wc))
        shutil.copytree(self.src, wc / "templates" / TEMPLATE_ID)
        (wc / "templates" / "обычная папка").mkdir()
        (wc / "templates" / "обычная папка" / "f.txt").write_text("x")
        tp.run("svn", "add", "--force", ".", cwd=wc)
        tp.run("svn", "propset", "svn:keywords", "Id Date Revision",
               str(wc / "templates" / TEMPLATE_ID / KEYWORD_FILE) + "@")
        tp.run("svn", "commit", "-m", "шаблоны", cwd=wc)
        self.root_url = self.url + "/templates"
        self.template_url = server.build_url(self.root_url, [TEMPLATE_ID])

    def close(self):
        self.tmp.cleanup()


def make_client(user: str = "ivanov") -> SvnClient:
    client = SvnClient("svn")
    client.store_auth = False
    client.set_credentials(user, "x")        # file:/// пароль не проверяет, но имя автора берёт отсюда
    return client


def content(root) -> dict:
    """{путь через «/»: байты или None для папки} без служебной .svn."""
    result = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".svn"]
        for name in dirs:
            result[os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/")] = None
        for name in files:
            full = os.path.join(base, name)
            result[os.path.relpath(full, root).replace(os.sep, "/")] = Path(full).read_bytes()
    return result


def expected_content(folder: str) -> dict:
    """Что должно лежать в проекте сразу после создания (без project.json): шаблон с проектными именами."""
    exp = tt.tree(_shared.src)
    exp.pop("template.json")
    exp[BIN_FILE], exp[KEYWORD_FILE] = BIN_BYTES, KEYWORD_BYTES
    exp[f"README_{folder}.txt"] = exp.pop(README_OLD)
    exp[f"docs/Specification/{folder}_спецификация.xlsx"] = exp.pop(XLSX_OLD)
    return exp


def without_meta(tree: dict) -> dict:
    return {k: v for k, v in tree.items() if k != projectmeta.FILE_NAME}


def fail_after(obj, name: str, error: BaseException):
    """Реальный метод выполняется (на диске и сервере всё сделано), затем «падает» ошибкой error."""
    real = getattr(obj, name)

    def wrapper(*args, **kwargs):
        real(*args, **kwargs)
        raise error
    return mock.patch.object(obj, name, side_effect=wrapper)


def cancel_before(obj, name: str, token: CancelToken):
    """Остановка приходит перед шагом: реальный метод запускается уже с остановленным токеном."""
    real = getattr(obj, name)

    def wrapper(*args, **kwargs):
        token.cancel()
        return real(*args, **kwargs)
    return mock.patch.object(obj, name, side_effect=wrapper)


@contextmanager
def killed_process():
    """Отключает уборку при сбое: на диске остаётся то же, что после убитого процесса."""
    with mock.patch.object(npd, "_remove_creating", return_value=False), \
            mock.patch.object(npd, "_drop_job"), \
            mock.patch.object(templates, "_rollback"):
        yield


class Env:
    """Один тест: свои папки на диске и своё место для проектов на общем «сервере» (с правилом «занимать .bin»)."""
    _numbers = count(1)

    def __init__(self, root: Path):
        self.root = Path(root)
        self.shared = _shared
        number = next(self._numbers)
        self.parent_url = server.build_url(_shared.url, ["konstruktor", f"projects {number}"])
        tp.run("svn", "mkdir", "--parents", "-m", "место для проектов", self.parent_url)
        cfg = self.root / "cfg"
        tp.run("svn", "checkout", "--depth", "empty", self.parent_url, str(cfg))
        tp.run("svn", "propset", "svn:auto-props", "*.bin = svn:needs-lock=*", str(cfg))
        tp.run("svn", "commit", "-m", "auto-props", str(cfg))
        self.base = self.root / "Проекты"
        self.base.mkdir(parents=True)
        self.jobs = self.root / "jobs"
        self.cache = self.root / "cache"
        self.store = SettingsStore(self.root / "settings.json")
        self.client = make_client("ivanov")
        self._loaded = None

    def loaded(self) -> "npd.LoadedTemplate":
        if self._loaded is None:
            infos = templates.list_templates(self.client, self.shared.root_url, self.cache)
            assert [i.id for i in infos] == [TEMPLATE_ID], infos
            self._loaded = npd.load_template(self.client, infos[0], self.cache, CancelToken(), ["konstruktor"],
                                             config.DEFAULT_FOLDER_FORMAT)
        return self._loaded

    def prepare(self, name: str, base=None):
        """(шаблон, план имён, project.json) как их собирает окно «Новый проект»."""
        loaded = self.loaded()
        base = str(base or self.base)
        values = {"date": "261028", "name": name}
        plan = templates.build_plan(loaded.manifest, values, "ivanov", TODAY, template_paths=loaded.paths,
                                    base_dir=base, warn_length=FilterRules().path_warn_length,
                                    staging_extra=len(".") + len(npd.CREATING_SUFFIX))
        meta = projectmeta.new_meta(name, plan.folder, login="ivanov",
                                    template={"id": loaded.info.id, "title": loaded.manifest.title,
                                              "revision": loaded.fetched.revision}, fields=values)
        return loaded, plan, meta

    def project_url(self, folder: str) -> str:
        return server.build_url(self.parent_url, [folder])

    def create_plain(self, name: str, base=None) -> str:
        """Проект на компьютере без истории (T.3)."""
        loaded, plan, meta = self.prepare(name, base)
        assert plan.ok, plan.errors
        return npd.create_project_folder(str(base or self.base), plan, loaded.fetched.path, meta, CancelToken(),
                                         self.jobs)

    def create_local(self, name: str, base=None, cancel=None):
        """Проект на компьютере с историей (T.3 + T.6). Возвращает (результат, план)."""
        loaded, plan, meta = self.prepare(name, base)
        assert plan.ok, plan.errors
        res = npd.create_local_project(self.client, str(base or self.base), plan, loaded.fetched.path, meta,
                                       cancel or CancelToken(), self.jobs, SVNADMIN, FilterRules(), self.store)
        return res, plan

    def create_on_server(self, name: str, cancel=None, base=None):
        loaded, plan, meta = self.prepare(name, base)
        assert plan.ok, plan.errors
        return npd.create_server_project(self.client, str(base or self.base), plan, loaded.fetched.path, meta,
                                         self.parent_url, "Новый проект из шаблона", FilterRules(),
                                         cancel or CancelToken(), CheckoutProgress(), None, self.jobs), plan

    def publish(self, folder: str, **kwargs):
        kwargs.setdefault("rules", RULES)
        kwargs.setdefault("jobs_dir", self.jobs)
        kwargs.setdefault("retry_delay", 0)
        return publish.publish(self.client, folder, self.parent_url, "Отправка проекта", **kwargs)

    def journals(self) -> list:
        return sorted(self.jobs.glob("*.json")) if self.jobs.exists() else []

    def server_has(self, folder: str) -> bool:
        return self.client.url_exists(self.project_url(folder))


@unittest.skipUnless(HAS_SVN, REASON)
class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._env_count = 0

    def new_env(self) -> Env:
        self._env_count += 1
        return Env(self.root / f"env{self._env_count}")

    # -- общие проверки ------------------------------------------------------------------

    def assert_on_server(self, env: Env, folder: str, local: str, expected: dict, history: bool = False):
        """Папка привязана к проекту на сервере; всё как у коллеги, который скачал проект заново."""
        project_url = env.project_url(folder)
        info = env.client.info(local)
        self.assertEqual(unquote(info.url).rstrip("/"), unquote(project_url + "/trunk"))
        self.assertEqual(env.client.status(local, rules=RULES).local_changes, [])
        self.assertEqual(without_meta(content(local)), without_meta(expected))
        self.assertFalse(os.access(os.path.join(local, BIN_FILE), os.W_OK))          # svn:needs-lock
        self.assertTrue(os.access(os.path.join(local, KEYWORD_FILE), os.W_OK))
        server_meta = env.client.cat(project_url + "/trunk/" + projectmeta.FILE_NAME)
        self.assertEqual(Path(local, projectmeta.FILE_NAME).read_bytes(), server_meta)
        self.assertEqual(json.loads(server_meta.decode("utf-8"))["svn"]["url"], project_url)
        self.assertEqual(sorted(os.listdir(os.path.dirname(local))),
                         sorted([folder] + ([localrepo.HISTORY_DIR] if history else [])))   # временных папок нет
        self.assertEqual(env.journals(), [])
        return project_url

    def assert_second_user_sees(self, env: Env, folder: str, first: str, expected: dict):
        """Второй пользователь скачивает проект в отдельную рабочую копию и видит то же самое."""
        second = env.root / "второй пользователь" / folder
        second.parent.mkdir(parents=True)
        client2 = make_client("petrov")
        project_url = env.project_url(folder)
        res = client2.checkout(project_url + "/trunk", str(second))
        self.assertEqual(content(second), content(first))                            # включая project.json
        self.assertEqual(without_meta(content(second)), without_meta(expected))
        self.assertEqual(res.revision, env.client.info(first).revision)
        self.assertFalse(os.access(second / BIN_FILE, os.W_OK))
        self.assertEqual(client2.status(str(second), rules=RULES).local_changes, [])
        # на сервере нет ни истории локального хранилища, ни служебных остатков
        self.assertEqual(sorted(e.name for e in client2.list_entries(project_url + "@") if e.kind == "dir"),
                         ["branches", "tags", "trunk"])
        return second


# ======================================================================================
# 1. Сквозные сценарии
# ======================================================================================

class FullFlowTests(TempDirCase):
    def test_template_to_local_history_to_server_to_second_user(self):
        """Шаблон на сервере -> проект с историей -> правка и «сохранить у себя» -> отправка -> второй пользователь.
        Название с '@', '%' и «%41» проходит через export, cat, checkout, status, info, import, commit, mkdir."""
        for name in NAMES:
            with self.subTest(name):
                self.run_flow(self.new_env(), name)

    def run_flow(self, env: Env, name: str):
        base = env.base
        loaded = env.loaded()
        # шаблон: ключевые слова не раскрыты (export с --ignore-keywords), cat их раскрывает - свойство действует
        self.assertEqual(loaded.fetched.path.joinpath(KEYWORD_FILE).read_bytes(), KEYWORD_BYTES)
        self.assertNotEqual(env.client.cat(server.build_url(_shared.template_url, [KEYWORD_FILE])), KEYWORD_BYTES)

        res, plan = env.create_local(name, base)
        folder, expected = res.path, expected_content(plan.folder)
        self.assertEqual((res.history, res.note), (True, ""))
        self.assertEqual(folder, os.path.join(str(base), plan.folder))
        self.assertEqual(without_meta(content(folder)), without_meta(expected))        # 1:1, пустые папки на месте
        meta = projectmeta.read(folder)
        self.assertEqual((meta.name, meta.folder, meta.svn), (name, plan.folder, None))
        self.assertEqual(meta.fields, {"date": "261028", "name": name})
        info = env.client.info(folder)
        self.assertTrue(info.url.startswith("file:///"))
        self.assertEqual(env.client.status(folder, rules=RULES).local_changes, [])
        self.assertTrue(localrepo.archive_path(env.store, folder))
        self.assertEqual(env.journals(), [])
        first_revision = info.revision

        # правка, новый файл, удаление пустой папки -> «сохранить у себя»
        readme = Path(folder, f"README_{plan.folder}.txt")
        readme.write_bytes("правка\r\nвторая строка".encode("utf-8"))
        Path(folder, "новый @ файл%.txt").write_bytes("новый".encode("utf-8"))
        shutil.rmtree(Path(folder, "07 Испытания"))
        saved = localrepo.save(env.client, folder, FilterRules(), "Правка перед отправкой")
        self.assertEqual(saved.revision, first_revision + 1)
        self.assertEqual(env.client.info(folder).revision, first_revision + 1)
        self.assertEqual(env.client.status(folder, rules=RULES).local_changes, [])
        expected[readme.name] = "правка\r\nвторая строка".encode("utf-8")
        expected["новый @ файл%.txt"] = "новый".encode("utf-8")
        del expected["07 Испытания"]
        self.assertEqual(without_meta(content(folder)), without_meta(expected))

        # на сервер: последнее состояние, без истории
        result = env.publish(folder)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.project_url, env.project_url(plan.folder))
        project_url = self.assert_on_server(env, plan.folder, folder, expected, history=True)
        self.assertEqual(len(env.client.last_log_entry(project_url, limit=10)), 2)      # папки + загрузка, без истории
        self.assertEqual(sorted(e.name for e in env.client.list_entries(project_url + "/trunk@", recursive=True)
                                if e.kind == "file"),
                         sorted([k for k, v in expected.items() if v is not None] + ["project.json"]))
        self.assertTrue(Path(localrepo.archive_path(env.store, folder)).is_dir())       # прежние сохранения - архив

        # второй пользователь
        self.assert_second_user_sees(env, plan.folder, folder, expected)

    def test_direct_to_server_then_second_user(self):
        """«Сразу на сервер»: сборка, загрузка, обычное скачивание в итоговую папку, второй пользователь."""
        for name in NAMES:
            with self.subTest(name):
                env = self.new_env()
                res, plan = env.create_on_server(name)
                folder, expected = os.path.join(str(env.base), plan.folder), expected_content(plan.folder)
                self.assertEqual((res.path, res.on_server, res.note), (folder, True, ""))
                project_url = self.assert_on_server(env, plan.folder, folder, expected)
                self.assertEqual(sorted(env.client.list_dirs(project_url + "@")), ["branches", "tags", "trunk"])
                self.assertEqual(len(env.client.last_log_entry(project_url, limit=10)), 2)
                meta = projectmeta.read(folder)
                self.assertEqual((meta.name, meta.svn["url"]), (name, project_url))
                self.assert_second_user_sees(env, plan.folder, folder, expected)

    def test_server_project_without_write_rights_stays_local(self):
        """Нет прав создавать в этом месте: проект остаётся на компьютере целым, сервер не тронут."""
        env = self.new_env()
        loaded, plan, meta = env.prepare("вафли")      # file:// права не проверяет: отказ имитируется ответом svn
        denied = SvnError(ErrorKind.AUTHZ, "svn: E220004: Access denied")
        with mock.patch.object(env.client, "import_dir", side_effect=denied):
            res = npd.create_server_project(env.client, str(env.base), plan, loaded.fetched.path, meta,
                                            env.parent_url, "c", FilterRules(), CancelToken(), CheckoutProgress(), None,
                                            env.jobs)
        self.assertFalse(res.on_server)
        self.assertEqual(res.note, npd.NO_RIGHTS_TEXT)
        self.assertEqual(without_meta(content(res.path)), without_meta(expected_content(plan.folder)))
        self.assertIsNone(projectmeta.read(res.path).svn)
        self.assertEqual(os.listdir(env.base), [plan.folder])
        self.assertEqual(env.journals(), [])
        self.assertFalse(env.server_has(plan.folder))                      # каркас, созданный до отказа, откатан

    def test_server_project_without_connection_stays_local_with_history(self):
        """Ревью 2026-11-02 п.6: связь пропала посреди загрузки - собранный проект не стирается, а остаётся на
        компьютере с историей (svnadmin есть), как в пути «на компьютере»."""
        env = self.new_env()
        loaded, plan, meta = env.prepare("вафли")
        real_import = env.client.import_dir

        def lost_for_server(src, url, *args, **kwargs):
            if url.startswith(env.parent_url):
                raise SvnError(ErrorKind.NO_CONNECTION, "svn: E170013: Unable to connect")
            return real_import(src, url, *args, **kwargs)        # история на компьютере грузится как обычно

        with mock.patch.object(env.client, "import_dir", side_effect=lost_for_server):
            res = npd.create_server_project(env.client, str(env.base), plan, loaded.fetched.path, meta,
                                            env.parent_url, "c", FilterRules(), CancelToken(), CheckoutProgress(), None,
                                            env.jobs, svnadmin=SVNADMIN, store=env.store)
        self.assertEqual((res.on_server, res.history, res.note), (False, True, npd.NO_LINK_KEEP_TEXT))
        self.assertEqual(without_meta(content(res.path)), without_meta(expected_content(plan.folder)))
        self.assertIsNone(projectmeta.read(res.path).svn)
        self.assertTrue(localrepo.is_local_url(env.client.info(res.path).url))       # история на этом компьютере
        self.assertEqual(len(env.store.settings.local_histories), 1)
        self.assertEqual(env.journals(), [])
        self.assertFalse(env.server_has(plan.folder))                      # каркас откатан


# ======================================================================================
# 2. Отмена и «убитый процесс» на шагах T.3 - T.6
# ======================================================================================

class CreateFolderInterruptTests(TempDirCase):
    """T.3: сборка в `.<имя>.creating`."""

    def test_cancel_in_the_middle_of_copying_leaves_nothing(self):
        env = self.new_env()
        loaded, plan, meta = env.prepare("вафли")
        token, copied = CancelToken(), []
        real_copy = shutil.copy2

        def copy_then_cancel(src, dst, **kwargs):
            copied.append(dst)
            if len(copied) == 3:
                token.cancel()
            return real_copy(src, dst, **kwargs)

        with mock.patch.object(shutil, "copy2", copy_then_cancel):
            with self.assertRaises(npd.CreateCancelled):
                npd.create_project_folder(str(env.base), plan, loaded.fetched.path, meta, token, env.jobs)
        self.assertGreaterEqual(len(copied), 3)
        self.assertEqual((os.listdir(env.base), env.journals()), ([], []))
        self.assertTrue(os.path.isdir(npd.create_project_folder(str(env.base), plan, loaded.fetched.path, meta,
                                                                CancelToken(), env.jobs)))   # после отмены создаётся

    def test_killed_at_each_step_is_cleaned_on_next_start(self):
        points = ["в середине копирования", "после копирования, до project.json", "перед переименованием"]
        for point in points:
            with self.subTest(point):
                env = self.new_env()
                loaded, plan, meta = env.prepare("вафли")
                creating = npd.creating_path(str(env.base), plan.folder)
                real_copy, real_rename, copied = shutil.copy2, os.rename, []

                def killing_copy(src, dst, **kwargs):
                    copied.append(dst)
                    if len(copied) == 3:
                        raise Killed()
                    return real_copy(src, dst, **kwargs)

                def killing_rename(src, dst, *args, **kwargs):
                    if os.path.basename(str(dst)) == plan.folder:
                        raise Killed()
                    return real_rename(src, dst, *args, **kwargs)

                patches = {
                    points[0]: mock.patch.object(shutil, "copy2", killing_copy),
                    points[1]: mock.patch.object(projectmeta, "write", side_effect=Killed()),
                    points[2]: mock.patch.object(os, "rename", killing_rename),
                }
                with killed_process(), patches[point], self.assertRaises(Killed):
                    npd.create_project_folder(str(env.base), plan, loaded.fetched.path, meta, CancelToken(), env.jobs)
                self.assertTrue(os.path.isdir(creating))                                   # недоделанная сборка
                self.assertEqual(len(env.journals()), 1)
                self.assertFalse(os.path.lexists(os.path.join(str(env.base), plan.folder)))
                self.assertEqual(npd.recover_create_jobs(env.jobs), 1)                       # при запуске
                self.assertEqual((os.listdir(env.base), env.journals()), ([], []))
                folder = npd.create_project_folder(str(env.base), plan, loaded.fetched.path, meta, CancelToken(),
                                                   env.jobs)
                self.assertEqual(without_meta(content(folder)), without_meta(expected_content(plan.folder)))


class LocalHistoryInterruptTests(TempDirCase):
    """T.6 в составе создания проекта (T.3): шаги svnadmin create, mkdir, import, checkout."""
    STEPS = ("mkdir_urls", "import_dir", "checkout")

    def assert_project_whole_without_history(self, env: Env, plan):
        folder = os.path.join(str(env.base), plan.folder)
        self.assertEqual(without_meta(content(folder)), without_meta(expected_content(plan.folder)))
        self.assertEqual(projectmeta.read(folder).folder, plan.folder)
        self.assertFalse(os.path.lexists(os.path.join(folder, ".svn")))
        self.assertEqual(os.listdir(env.base), [plan.folder])                                # ни .svnlite, ни .creating
        self.assertEqual(env.journals(), [])
        self.assertEqual(env.store.settings.local_histories, {})
        return folder

    def test_cancel_at_each_history_step_keeps_project_without_history(self):
        for step in self.STEPS:
            with self.subTest(step):
                env = self.new_env()
                token = CancelToken()
                with cancel_before(env.client, step, token):
                    res, plan = env.create_local("вафли", cancel=token)
                self.assertEqual((res.history, res.note), (False, npd.NO_HISTORY_TEXT))
                self.assert_project_whole_without_history(env, plan)

    def test_cancel_before_svnadmin_keeps_project_without_history(self):
        env = self.new_env()
        token = CancelToken()
        real = localrepo._svnadmin_create

        def cancel_then_create(*args, **kwargs):
            token.cancel()
            return real(*args, **kwargs)

        with mock.patch.object(localrepo, "_svnadmin_create", side_effect=cancel_then_create):
            res, plan = env.create_local("вафли", cancel=token)
        self.assertEqual((res.history, res.note), (False, npd.NO_HISTORY_TEXT))
        self.assert_project_whole_without_history(env, plan)

    def test_killed_at_each_history_step_is_cleaned_on_next_start(self):
        steps = [(localrepo, "_svnadmin_create"), *[(None, s) for s in self.STEPS]]
        for owner, step in steps:
            with self.subTest(step):
                env = self.new_env()
                target = owner or env.client
                with fail_after(target, step, Killed()), self.assertRaises(Killed):
                    env.create_local("вафли")
                self.assertEqual(len(env.journals()), 1)                                    # журнал истории остался
                self.assertEqual(npd.recover_create_jobs(env.jobs, store=env.store), 1)
                _loaded, plan, _meta = env.prepare("вафли")
                folder = self.assert_project_whole_without_history(env, plan)
                # после уборки историю можно включить заново
                result = localrepo.create_local_repo(env.client, folder, SVNADMIN, store=env.store, jobs_dir=env.jobs)
                self.assertTrue(Path(result.repo).is_dir())
                self.assertEqual(env.client.status(folder, rules=RULES).local_changes, [])


class PublishInterruptTests(TempDirCase):
    """T.4: отправка с компьютера, обе разновидности источника (обычная папка и папка с историей)."""
    PLAIN = ("mkdir_urls", "import_dir", "checkout")
    WITH_HISTORY = ("export", *PLAIN)
    # что после убитого процесса и разбора при запуске: None - сервер как был, "resume" - «Завершить отправку»
    AFTER_KILL = {"export": None, "mkdir_urls": None, "import_dir": "resume", "checkout": "resume"}

    def variants(self):
        for history, steps in ((False, self.PLAIN), (True, self.WITH_HISTORY)):
            for step in steps:
                yield history, step

    def project(self, env: Env, history: bool):
        if history:
            res, plan = env.create_local("вафли @ 100%")
            return res.path, plan
        _loaded, plan, _meta = env.prepare("вафли @ 100%")
        return env.create_plain("вафли @ 100%"), plan

    def test_cancel_at_each_step_keeps_folder_and_server_clean(self):
        for history, step in self.variants():
            with self.subTest(history=history, step=step):
                env = self.new_env()
                folder, plan = self.project(env, history)
                before = tp.tree_hash(folder)
                token = CancelToken()
                with cancel_before(env.client, step, token), self.assertRaises(publish.PublishError) as cm:
                    env.publish(folder, cancel=token)
                error = cm.exception
                self.assertEqual(error.code, publish.CANCELLED)
                if step == "checkout":                  # проект уже на сервере: «Завершить отправку»
                    self.assertEqual(error.stage, "bind")
                    self.assertIsNotNone(error.job)
                    self.assertTrue(env.server_has(plan.folder))
                else:
                    self.assertEqual(error.stage, "source" if step == "export" else "upload")
                    self.assertFalse(env.server_has(plan.folder))                          # откат сервера
                self.assertEqual(tp.tree_hash(folder), before)                             # файлы те же
                self.assertEqual(os.path.isdir(os.path.join(folder, ".svn")), history)    # прежняя привязка на месте
                if history:
                    self.assertTrue(env.client.info(folder).url.startswith("file:///"))
                self.assertFalse(os.path.exists(publish.svn_old_path(folder)))
                if step == "checkout":
                    result = publish.resume_publish(env.client, error.job, RULES, retry_delay=0)
                    self.assertEqual(result.changed, [])
                else:
                    self.assertEqual(env.journals(), [])
                    self.assertEqual(sorted(n for n in os.listdir(env.base) if n != localrepo.HISTORY_DIR),
                                     [plan.folder])                                         # временных папок нет
                    env.publish(folder)                                                    # потом отправка проходит
                self.assert_on_server(env, plan.folder, folder, expected_content(plan.folder), history)

    def test_killed_at_each_step_is_recovered_on_next_start(self):
        for history, step in self.variants():
            with self.subTest(history=history, step=step):
                env = self.new_env()
                folder, plan = self.project(env, history)
                before = tp.tree_hash(folder)
                with fail_after(env.client, step, Killed()), self.assertRaises(Killed):
                    env.publish(folder)
                [job] = publish.pending_jobs(env.jobs)
                resumable = publish.recover_job(env.client, job, retry_delay=0)
                if self.AFTER_KILL[step] is None:
                    self.assertIsNone(resumable)
                    self.assertFalse(env.server_has(plan.folder))
                    self.assertEqual(tp.tree_hash(folder), before)                         # папка как была
                    self.assertEqual(os.path.isdir(os.path.join(folder, ".svn")), history)
                    self.assertEqual(env.journals(), [])
                    self.assertFalse(os.path.exists(publish.svn_old_path(folder)))
                    env.publish(folder)
                else:
                    self.assertIsNotNone(resumable)
                    self.assertTrue(env.server_has(plan.folder))
                    publish.resume_publish(env.client, resumable, RULES, retry_delay=0)
                self.assert_on_server(env, plan.folder, folder, expected_content(plan.folder), history)


class ServerCreateInterruptTests(TempDirCase):
    """T.5: «Сразу на сервер»: сборка, каркас, загрузка, скачивание."""
    STEPS = ("apply_plan", "mkdir_urls", "import_dir", "checkout")

    def target(self, env: Env, step: str):
        return templates if step == "apply_plan" else env.client

    def test_cancel_at_each_step(self):
        for step in self.STEPS:
            with self.subTest(step):
                env = self.new_env()
                token = CancelToken()
                _loaded, plan, _meta = env.prepare("вафли @ 100%")
                final = os.path.join(str(env.base), plan.folder)
                if step == "checkout":
                    with cancel_before(env.client, step, token), self.assertRaises(npd.ServerCreatedNotDownloaded) as cm:
                        env.create_on_server("вафли @ 100%", token)
                    self.assertTrue(cm.exception.cancelled)
                    self.assertTrue(env.server_has(plan.folder))                          # готовый проект остаётся
                    self.assertEqual(sorted(env.client.list_dirs(env.project_url(plan.folder) + "@")),
                                     ["branches", "tags", "trunk"])
                    # «Скачать ещё раз» доделывает начатое
                    res = npd.download_project(env.client, env.project_url(plan.folder), final, str(env.base),
                                               CancelToken(), jobs_dir=env.jobs)
                    self.assertTrue(res.on_server)
                    self.assertEqual(without_meta(content(final)), without_meta(expected_content(plan.folder)))
                    self.assertEqual(env.journals(), [])
                    continue
                with cancel_before(self.target(env, step), step, token), self.assertRaises(npd.CreateCancelled) as cm:
                    env.create_on_server("вафли @ 100%", token)
                if step in ("mkdir_urls", "import_dir"):
                    self.assertTrue(cm.exception.rolled_back)                              # каркас убран с сервера
                self.assertFalse(env.server_has(plan.folder))
                self.assertEqual((os.listdir(env.base), env.journals()), ([], []))
                res, _plan = env.create_on_server("вафли @ 100%")                            # после отмены всё работает
                self.assertTrue(res.on_server)

    def test_killed_at_each_step_is_recovered_on_next_start(self):
        # что должно быть на сервере после разбора: None - ничего, "project" - готовый проект, о нём сообщают
        outcome = {"apply_plan": None, "mkdir_urls": None, "import_dir": "project", "checkout": "project"}
        for step in self.STEPS:
            with self.subTest(step):
                env = self.new_env()
                _loaded, plan, _meta = env.prepare("вафли @ 100%")
                final = os.path.join(str(env.base), plan.folder)
                project_url = env.project_url(plan.folder)
                with killed_process(), fail_after(self.target(env, step), step, Killed()), self.assertRaises(Killed):
                    env.create_on_server("вафли @ 100%")
                self.assertEqual(len(env.journals()), 1)
                landed = []
                npd.recover_create_jobs(env.jobs, client=env.client, landed=landed)         # при запуске программы
                self.assertEqual(env.journals(), [])
                self.assertFalse(os.path.lexists(npd.creating_path(str(env.base), plan.folder)))
                self.assertFalse(os.path.lexists(final))                                    # недокачанного нет
                if outcome[step] is None:
                    self.assertEqual((landed, env.server_has(plan.folder)), ([], False))
                    res, _plan = env.create_on_server("вафли @ 100%")                        # можно создать заново
                    self.assertTrue(res.on_server)
                else:
                    self.assertEqual(landed, [project_url])
                    self.assertTrue(env.client.url_exists(project_url + "/trunk"))
                    res = npd.download_project(env.client, project_url, final, str(env.base), CancelToken(),
                                               jobs_dir=env.jobs)                           # «Скачать ещё раз»
                    self.assertTrue(res.on_server)
                self.assert_on_server(env, plan.folder, final, expected_content(plan.folder))


# ======================================================================================
# 3. Длинный путь
# ======================================================================================

class LongPathTests(TempDirCase):
    def padded_base(self, env: Env, name: str, target: int) -> Path:
        """Папка, в которой самый длинный путь проекта (вместе с временной папкой сборки) ровно target символов."""
        probe = env.prepare(name)[1]
        pad = target - probe.longest_path - 1
        self.assertGreater(pad, 0)
        base = env.base / ("п" * pad)
        self.assertEqual(env.prepare(name, base)[1].longest_path, target)
        return base

    def test_default_warning_threshold_is_200_and_refusal_is_260(self):
        env = self.new_env()
        self.assertEqual(FilterRules().path_warn_length, 200)
        self.assertEqual(templates.MAX_PATH, 260)
        edge = env.prepare("вафли", self.padded_base(env, "вафли", 200))[1]
        self.assertTrue(edge.ok)
        self.assertEqual(edge.warnings, [])                                  # ровно 200 - предупреждения ещё нет
        over = env.prepare("вафли", self.padded_base(env, "вафли", 201))[1]
        self.assertTrue(over.ok)
        self.assertEqual(len(over.warnings), 1)
        self.assertIn("201", over.warnings[0])
        # от 260 символов до файла - отказ (сами папки такой длины создавать не нужно: путь только в плане)
        too_long = env.prepare("вафли", "C:\\" + "п" * 250)[1]
        self.assertFalse(too_long.ok)
        self.assertTrue(any("Самый длинный путь" in e for e in too_long.errors), too_long.errors)

    def test_too_long_plan_is_refused_and_nothing_is_created(self):
        env = self.new_env()
        loaded, plan, meta = env.prepare("вафли", "C:\\" + "п" * 250)
        self.assertFalse(plan.ok)
        with self.assertRaises(templates.TemplateError):
            npd.create_project_folder(str(env.base), plan, loaded.fetched.path, meta, CancelToken(), env.jobs)
        self.assertEqual((os.listdir(env.base), env.journals()), ([], []))
        with self.assertRaises(templates.TemplateError):
            npd.create_server_project(env.client, str(env.base), plan, loaded.fetched.path, meta, env.parent_url,
                                      "c", FilterRules(), CancelToken(), CheckoutProgress(), None, env.jobs)
        self.assertEqual((os.listdir(env.base), env.journals()), ([], []))
        self.assertFalse(env.server_has(plan.folder))

    def test_project_with_warning_length_works_through_the_whole_flow(self):
        """Путь длиннее 200, но короче пределов Windows: предупреждение есть, создание, история, отправка работают."""
        env = self.new_env()
        base = self.padded_base(env, "вафли", 232)
        base.mkdir()
        plan = env.prepare("вафли", base)[1]
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.warnings), 1)
        res, plan = env.create_local("вафли", base)
        self.assertTrue(res.history, res.note)
        folder, expected = res.path, expected_content(plan.folder)
        self.assertGreater(max(len(os.path.join(folder, p.replace("/", os.sep))) for p in expected), 200)
        self.assertEqual(without_meta(content(folder)), without_meta(expected))
        Path(folder, f"README_{plan.folder}.txt").write_bytes("правка".encode("utf-8"))
        self.assertIsNotNone(localrepo.save(env.client, folder, FilterRules(), "длинный путь"))
        expected[f"README_{plan.folder}.txt"] = "правка".encode("utf-8")
        result = env.publish(folder)
        self.assertEqual(result.changed, [])
        self.assertEqual(env.client.status(folder, rules=RULES).local_changes, [])
        second = env.root / "второй" / plan.folder
        second.parent.mkdir()
        make_client("petrov").checkout(env.project_url(plan.folder) + "/trunk", str(second))
        self.assertEqual(without_meta(content(second)), without_meta(expected))


# ======================================================================================
# 4. Шаблон с svn:keywords
# ======================================================================================

class KeywordsTemplateTests(TempDirCase):
    def test_keywords_are_not_expanded_anywhere_in_the_chain(self):
        env = self.new_env()
        loaded = env.loaded()
        # кэш шаблона: байты как в хранилище (export --ignore-keywords)
        self.assertEqual(content(loaded.fetched.path)[KEYWORD_FILE], KEYWORD_BYTES)
        # обычное скачивание шаблона (checkout) ключевые слова раскрыло бы: свойство в хранилище действительно есть
        plain = env.root / "обычный checkout"
        env.client.checkout(_shared.template_url, str(plain))
        self.assertNotEqual(Path(plain, KEYWORD_FILE).read_bytes(), KEYWORD_BYTES)
        self.assertTrue(Path(plain, KEYWORD_FILE).read_bytes().startswith(b"$Id: "))
        # проект: файл тот же побайтово в папке, в локальной истории, на сервере и у второго пользователя
        res, plan = env.create_local("вафли")
        self.assertEqual(Path(res.path, KEYWORD_FILE).read_bytes(), KEYWORD_BYTES)
        self.assertEqual(env.client.status(res.path, rules=RULES).local_changes, [])      # и свойства keywords нет
        env.publish(res.path)
        self.assertEqual(env.client.cat(env.project_url(plan.folder) + "/trunk/" + KEYWORD_FILE), KEYWORD_BYTES)
        self.assertEqual(Path(res.path, KEYWORD_FILE).read_bytes(), KEYWORD_BYTES)
        second = env.root / "второй" / plan.folder
        second.parent.mkdir()
        make_client("petrov").checkout(env.project_url(plan.folder) + "/trunk", str(second))
        self.assertEqual(Path(second, KEYWORD_FILE).read_bytes(), KEYWORD_BYTES)


if __name__ == "__main__":
    unittest.main()
