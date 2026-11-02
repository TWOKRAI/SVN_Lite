"""Заглушка svn для тестов. Режим задаётся переменной FAKE_SVN_MODE.

ok | auth | authz | noconn | notfound | hang | commit_ood | commit_tag | lost_local
lost_local: команды, в аргументах которых есть «lost», падают как при пропавшем локальном хранилище
(E170013 + E180001 на file://); остальные работают как в ok.
Каждый вызов дописывается в файл FAKE_SVN_LOG (JSON: аргументы и stdin).
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
args = sys.argv[1:]
mode = os.environ.get("FAKE_SVN_MODE", "ok")

if "--version" in args:
    print(os.environ.get("FAKE_SVN_VERSION", "1.14.2"))
    sys.exit(0)

password = None
if "--password-from-stdin" in args:
    # как настоящий svn для Windows: читает до конца, отрезает только '\r\n' (одиночный '\n' остаётся в пароле)
    password = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if password.endswith("\r\n"):
        password = password[:-2]
elif "--password" in args:
    password = args[args.index("--password") + 1]

def _acp():
    try:
        import ctypes
        return "cp%d" % ctypes.windll.kernel32.GetACP()
    except Exception:  # не Windows
        return "utf-8"


log_path = os.environ.get("FAKE_SVN_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        entry = {"args": args, "password": password}
        # файлы временные: содержимое читаем сейчас. Как и настоящий svn: --file в кодировке --encoding
        # (UTF-8), --targets в ANSI-кодовой странице системы
        for flag, key, enc in (("--file", "message", "utf-8"), ("--targets", "targets", _acp())):
            if flag in args:
                with open(args[args.index(flag) + 1], encoding=enc, newline="") as src:
                    entry[key] = src.read()
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def fail(text, code=1):
    sys.stderr.write(text + "\n")
    sys.exit(code)


if mode == "auth" and password != "good":
    fail("svn: E215004: No more credentials or we tried too many times.\nAuthentication failed")
if mode == "authz":
    fail("svn: E170001: Authorization failed")
if mode == "noconn":
    fail("svn: E170013: Unable to connect to a repository at URL 'svn://x'\n"
         "svn: E731001: Этот хост неизвестен.")
if mode == "notfound":
    fail("svn: warning: W160013: URL 'svn://x/y' non-existent in revision 3\n"
         "svn: E200009: Could not list all targets because some targets don't exist")

if mode == "lost_local" and any("lost" in a for a in args):
    fail("svn: E170013: Unable to connect to a repository at URL 'file:///C:/history/abc'\n"
         "svn: E180001: Unable to open repository 'file:///C:/history/abc'")

cmd = args[0]
if cmd == "list":
    recursive = "-R" in args
    print('<?xml version="1.0" encoding="UTF-8"?><lists><list path="x">')
    if recursive:
        print('<entry kind="dir"><name>docs</name></entry>')
        print('<entry kind="file"><name>a.txt</name><size>1000</size></entry>')
        print('<entry kind="file"><name>docs/б в.txt</name><size>2000</size></entry>')
        print('<entry kind="file"><name>c.bin</name><size>3000</size></entry>')
    else:
        print('<entry kind="dir"><name>trunk</name></entry>')
        print('<entry kind="dir"><name>tags</name></entry>')
        print('<entry kind="file"><name>readme.txt</name><size>5</size></entry>')
    print("</list></lists>")
elif cmd == "info":
    print('<?xml version="1.0" encoding="UTF-8"?><info><entry path="." revision="42" kind="dir"/></info>')
elif cmd == "checkout":
    # checkout [--force] ... URL DEST: DEST идёт после -r HEAD и URL; файлы, уже лежащие в DEST, при --force - «E»
    dest = args[args.index("-r") + 3]
    for name in ("a.txt", "docs", "docs\\б в.txt", "c.bin"):
        existed = "--force" in args and os.path.exists(os.path.join(dest, name))
        print(("E    " if existed else "A    ") + name, flush=True)
        if mode == "hang":
            time.sleep(0.05)
    if mode == "hang":
        time.sleep(60)
    print("Checked out revision 42.")
elif cmd == "export":
    # export --ignore-keywords --ignore-externals URL@ DEST@: создаёт DEST с файлами из list -R
    dest = [a for a in args[1:] if not a.startswith("--")][1].rstrip("@")
    os.makedirs(os.path.join(dest, "docs"))
    print("A    " + dest, flush=True)
    for name in ("a.txt", "docs/б в.txt"):
        with open(os.path.join(dest, *name.split("/")), "w", encoding="utf-8") as fh:
            fh.write("fake " + name)
        print("A    " + os.path.join(dest, *name.split("/")), flush=True)
        if mode == "hang":
            time.sleep(0.05)
    if mode == "hang":
        time.sleep(60)
    print("Exported revision 42.")
elif cmd == "cat":
    sys.stdout.buffer.write(("fake content of " + args[1]).encode("utf-8"))
elif cmd == "import":
    # import ... -m msg . URL: показывает файлы текущей папки как «Adding»
    for base, dirs, files in os.walk("."):
        for name in sorted(dirs + files):
            print("Adding         " + os.path.normpath(os.path.join(base, name)), flush=True)
    print("Committed revision 43.")
elif cmd == "mkdir":
    print("Committed revision 44.")
elif cmd == "status":
    wc = [a for a in args[1:] if not a.startswith("-")][0].rstrip("@")

    def entry(name, item, props="none", rev=None, extra="", incoming=""):
        rev_attr = f' revision="{rev}"' if rev else ""
        return (f'<entry path="{os.path.join(wc, name)}"><wc-status item="{item}" props="{props}"{rev_attr}>'
                f'{extra}</wc-status>{incoming}</entry>')

    print('<?xml version="1.0" encoding="UTF-8"?><status><target path="x">')
    print(entry("a.txt", "modified", rev=40))
    print(entry("свойства.txt", "normal", props="modified", rev=40))
    print(entry("новый файл.txt", "unversioned"))
    print(entry("Thumbs.db", "unversioned"))
    print(entry("backup", "unversioned"))
    print(entry("добавлен.txt", "added", rev=0))
    print(entry("удалён.txt", "deleted", rev=40))
    print(entry("потерян.txt", "missing", rev=40))
    print(entry("спор.txt", "conflicted", rev=40))
    print(entry("занят.txt", "modified", rev=42,
                extra='<lock><token>t</token><owner>я</owner></lock>'))
    print(entry("чистый.txt", "normal", rev=42,
                incoming='<repos-status item="modified" props="none"/>'))
    print('<against revision="44"/></target></status>')
elif cmd == "propget":
    wc = [a for a in args[2:] if not a.startswith("-")][0].rstrip("@")
    print('<?xml version="1.0" encoding="UTF-8"?><properties>')
    for name in ("a.txt", "занят.txt"):
        print(f'<target path="{os.path.join(wc, name)}"><property name="svn:needs-lock">*</property></target>')
    print("</properties>")
elif cmd in ("add", "rm", "resolve"):
    pass
elif cmd == "commit":
    if mode == "commit_ood":
        fail("svn: E155011: Commit failed (details follow):\n"
             "svn: E160028: File '/p/trunk/a.txt' is out of date")
    if mode == "commit_tag":
        fail("svn: E165001: Commit blocked by pre-commit hook (exit code 1) with output:\n"
             "Версия (тег) только для чтения.\nA version (tag) is read-only.")
    targets = open(args[args.index("--targets") + 1], encoding=_acp()).read().splitlines()
    for line in targets:
        print("Sending        " + line.rstrip("@"))
    print("Transmitting file data ..")
    print("Committed revision 45.")
elif cmd == "log":
    print('<?xml version="1.0" encoding="UTF-8"?><log>')
    print('<logentry revision="43"><author>иванов</author><date>2026-10-20T08:00:00.000000Z</date>'
          '<paths><path action="M" kind="file">/p/trunk/чертёж 1.txt</path>'
          '<path action="A" kind="file">/p/trunk/новый.txt</path></paths><msg>Правка чертежа</msg></logentry>')
    print('<logentry revision="44"><author>петров</author><date>2026-10-21T09:30:00.000000Z</date>'
          '<paths><path action="D" kind="file">/p/trunk/старый.txt</path></paths><msg>Убрал лишнее</msg></logentry>')
    print("</log>")
elif cmd == "diff":
    print('<?xml version="1.0" encoding="UTF-8"?><diff><paths>')
    print('<path props="none" kind="file" item="modified">svn://x/p/branches/b/чертёж 1.txt</path>')
    print('<path props="none" kind="file" item="added">svn://x/p/branches/b/docs/new.txt</path>')
    print('<path props="none" kind="file" item="deleted">svn://x/p/trunk/old.txt</path>')
    print("</paths></diff>")
else:
    fail("svn: E000000: unknown command " + cmd)
