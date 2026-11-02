"""Дерево папок на сервере: переиспользуемый виджет и окно «Место на сервере» (Task T.5 из docs/PLAN_templates.md).

Дерево вынесено из вкладки «Скачать проект» без изменения поведения: папки открываются по одной, сохранённый список
показывается сразу и обновляется с сервера, проекты отмечены значком ✔. Кэш списков остаётся у главного окна
(`cache_get`, `cache_put`): общий для вкладки и окон выбора места.

Все обращения к серверу идут в фоне через `app.run_bg`. Тексты без терминов SVN.
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from .. import messages, server, theme
from ..dialogs import center_window, FONT_FAMILY
from ..svn_core import ErrorKind
from .models import JobStamp, Node, TREE_HINT

log = logging.getLogger("svnhelper.server_tree")

PLACE_HINT = "Откройте папки и выберите ту, где должен лежать проект. Проекты отмечены значком ✔."
PLACE_IS_PROJECT = "Это проект, а не папка. Выберите папку, в которой он должен лежать."


class ServerTree(ttk.Frame):
    """Дерево папок сервера. Корни берутся из `config.repos`.

    cache_get(segments) -> запись кэша или None; cache_put(segments, listing, server_url) — запомнить список;
    on_select(Node) — выбрана строка; on_status(текст, ошибка) — подсказка под деревом;
    on_idle() — очередная загрузка закончилась (окно могло потерять захват из-за окна входа).
    """

    def __init__(self, master, app, *, cache_get: Callable, cache_put: Callable, on_select: Callable[[Node], None],
                 on_status: Callable[[str, bool], None], on_idle: Optional[Callable[[], None]] = None,
                 height: int = 7):
        super().__init__(master)
        self.app = app
        self._cache_get = cache_get
        self._cache_put = cache_put
        self._on_select = on_select
        self._on_status = on_status
        self._on_idle = on_idle
        self.nodes: dict[str, Node] = {}
        self.busy = 0                      # сколько загрузок папок идёт сейчас
        self._counter = 0
        self._reveal: list[str] = []       # путь, который надо раскрыть и выделить, когда папки загрузятся
        app._extra_trees.add(self)         # перекраска при смене темы
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, show="tree", selectmode="browse", height=height)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        app._tag_project_rows(self.tree)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_selected)

    def _status(self, text: str, error: bool = False) -> None:
        self._on_status(text, error)

    def destroy(self) -> None:
        self.app._extra_trees.discard(self)
        super().destroy()

    def recolor(self) -> None:
        self.app._tag_project_rows(self.tree)

    def reveal(self, segments: list[str]) -> None:
        """Раскрывает папки до segments и выделяет последнюю (по мере загрузки папок с сервера)."""
        self._reveal = list(segments)
        self._reveal_step()

    def _reveal_step(self) -> None:
        target = self._reveal
        if not target:
            return
        iid = next((i for i in self.tree.get_children("") if self.nodes[i].segments == target[:1]), None)
        while iid is not None:
            node = self.nodes[iid]
            if node.segments == target:
                self._reveal = []
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return
            if node.state in ("new", "error"):
                self.tree.item(iid, open=True)
                self.load_node(iid)
                return
            if node.state == "loading":
                self.tree.item(iid, open=True)
                return                      # дождёмся ответа: шаг повторится из _idle
            name = target[len(node.segments)]
            child = next((c for c in self.tree.get_children(iid)
                          if c in self.nodes and self.nodes[c].segments[-1] == name), None)
            self.tree.item(iid, open=True)
            if child is None or node.is_project:
                self._reveal = []
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return
            iid = child
        self._reveal = []

    # -- узлы ------------------------------------------------------------------------

    def _add_node(self, parent: str, segments: list[str], is_project: bool = False) -> str:
        self._counter += 1
        iid = f"n{self._counter}"
        name = segments[-1]
        self.nodes[iid] = Node(segments, is_project=is_project)
        if is_project:
            self.tree.insert(parent, "end", iid=iid, text=f"✔  {name}", tags=("project",))
        else:
            self.tree.insert(parent, "end", iid=iid, text=name)
            self.tree.insert(iid, "end", iid=f"{iid}.d", text="Загрузка…", tags=("muted",))
        return iid

    def _forget_node(self, iid: str) -> None:
        """Убирает узел вместе с вложенными из дерева и из словаря узлов."""
        for child in self.tree.get_children(iid):
            self._forget_node(child)
        self.nodes.pop(iid, None)
        self.tree.delete(iid)

    def fill_roots(self) -> None:
        for repo in self.app.config.repos:
            self._add_node("", [repo])

    def reset(self) -> None:
        """Дерево заново: остаются только корни, списки папок читаются с сервера снова."""
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.busy = 0
        self.fill_roots()

    # -- загрузка папок --------------------------------------------------------------

    def _on_open(self, _event) -> None:
        iid = self.tree.focus()
        node = self.nodes.get(iid)
        if node and node.state in ("new", "error"):
            self.load_node(iid)

    def load_node(self, iid: str) -> None:
        node = self.nodes.get(iid)
        if node is None:
            return
        app = self.app
        if app.client is None:
            self._node_error(iid, "Не найдена программа svn.exe (TortoiseSVN).")
            return
        node.state = "loading"
        self.busy += 1
        segments = list(node.segments)
        cached = self._cache_get(segments)
        if cached is not None:  # сначала показываем сохранённое, потом обновляем с сервера
            self._show_listing(iid, [(str(d[0]), bool(d[1])) for d in cached["dirs"]
                                     if isinstance(d, list) and len(d) == 2],
                               bool(cached.get("is_project")))
            if iid in self.nodes:
                self.nodes[iid].state = "loading"
            self._status("Показан сохранённый список, обновляется…", False)
        else:
            self.tree.item(f"{iid}.d", text="Загрузка…")
            self._status("Загрузка списка папок…", False)

        client, server_url = app.client, app.config.server   # снимок: к концу загрузки настройки могли смениться

        def work() -> server.FolderListing:
            listing = server.load_folder(client, server_url, segments, app._probe_pool)
            self._cache_put(segments, listing, server_url)
            return listing

        app.run_bg(work, lambda res, err, gen: self._on_folder_loaded(iid, res, err, gen))

    def _show_listing(self, iid: str, entries: list[tuple[str, bool]], is_project: bool) -> None:
        """Выводит список папок в узле. Уже открытые вложенные папки остаются как были."""
        node = self.nodes[iid]
        if is_project:
            self._mark_as_project(iid)
            return
        for suffix in (".d", ".e"):
            if self.tree.exists(f"{iid}{suffix}"):
                self.tree.delete(f"{iid}{suffix}")
        wanted = dict(entries)
        existing: dict[str, str] = {}
        for child in self.tree.get_children(iid):
            child_node = self.nodes.get(child)
            if child_node is None:
                continue
            name = child_node.segments[-1]
            if wanted.get(name) is None or wanted[name] != child_node.is_project:
                self._forget_node(child)  # папки больше нет или она стала проектом (или перестала им быть)
            else:
                existing[name] = child
        for index, (name, project) in enumerate(entries):
            child = existing.get(name) or self._add_node(iid, [*node.segments, name], project)
            self.tree.move(child, iid, index)
        if not entries:
            self.tree.insert(iid, "end", iid=f"{iid}.e", text="(нет вложенных папок)", tags=("muted",))
        node.state = "loaded"

    def _on_folder_loaded(self, iid: str, listing, error, gen: JobStamp) -> None:
        self.busy = max(0, self.busy - 1)
        if not self.winfo_exists():         # окно закрыли, пока шла загрузка
            return
        node = self.nodes.get(iid)
        if node is None:  # список обновили, пока шла загрузка
            return
        if self.busy == 0:
            self._status(TREE_HINT, False)
        if error is not None:
            app = self.app
            kind = app._error_kind(error)
            if kind is ErrorKind.AUTH:
                self.busy += 1
                self._status("Нужен вход на сервер…", False)
                app.login.request(retry=lambda: self._retry_node(iid), give_up=lambda: self._give_up_node(iid),
                                  job_gen=gen)
                return
            self._node_error(iid, messages.short_message(kind))
            self._idle()
            return
        self.app.login.reset_attempts()  # ходили на сервер с учётными данными
        self._show_listing(iid, [(e.name, e.is_project) for e in listing.entries], listing.is_project)
        self._idle()

    def _idle(self) -> None:
        if self._on_idle is not None:
            self._on_idle()
        self._reveal_step()

    def _retry_node(self, iid: str) -> None:
        self.busy = max(0, self.busy - 1)
        if self.winfo_exists():
            self.load_node(iid)

    def _give_up_node(self, iid: str) -> None:
        self.busy = max(0, self.busy - 1)
        if not self.winfo_exists():
            return
        self._node_error(iid, messages.short_message(ErrorKind.AUTH))
        self._idle()

    def _node_error(self, iid: str, text: str) -> None:
        node = self.nodes.get(iid)
        if node is None:
            return
        node.state = "error"
        self._reveal = []
        if self.tree.exists(f"{iid}.d"):
            self.tree.item(f"{iid}.d", text=f"⚠ {text}")
        elif self.tree.get_children(iid):
            text = f"Показан сохранённый список: он мог устареть. {text}"
        self._status(text, True)

    def _mark_as_project(self, iid: str) -> None:
        """Корень хранилища сам оказался проектом (основная ветка лежит прямо в нём)."""
        node = self.nodes[iid]
        node.is_project = True
        node.state = "loaded"
        for child in self.tree.get_children(iid):
            self._forget_node(child)
        self.tree.item(iid, text=f"✔  {node.segments[-1]}", tags=("project",), open=False)

    def _on_selected(self, _event) -> None:
        sel = self.tree.selection()
        node = self.nodes.get(sel[0]) if sel else None
        if node is not None:
            self._on_select(node)


class ServerPlaceDialog(tk.Toplevel):
    """Окно «Место на сервере»: выбор папки, в которой будет лежать проект. result — список частей пути или None."""

    def __init__(self, app, current: Optional[list[str]] = None, parent=None):
        super().__init__(app.root, background=theme.PALETTE.surface)
        self.app = app
        self.result: Optional[list[str]] = None
        self.picked: Optional[list[str]] = None
        self.title("Место на сервере")
        self.transient(parent or app.root)
        px = app.px
        body = ttk.Frame(self, padding=px(16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text="Где должен лежать проект", font=(FONT_FAMILY, 12, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(body, text=PLACE_HINT, style="Hint.TLabel", wraplength=px(440), justify="left").grid(
            row=1, column=0, sticky="w", pady=(px(4), px(8)))
        frame = ttk.Frame(body, style="Card.TFrame", padding=1)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ServerTree(frame, app, cache_get=lambda s: app._cached_folder(s),
                               cache_put=lambda *a: app._cache_store(*a), on_select=self._on_pick,
                               on_status=self._on_status, on_idle=self._regrab, height=10)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.place_var = tk.StringVar(value="Папка не выбрана")
        ttk.Label(body, textvariable=self.place_var, font=(FONT_FAMILY, 10, "bold"), wraplength=px(440),
                  justify="left").grid(row=3, column=0, sticky="w", pady=(px(8), 0))
        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(body, textvariable=self.status_var, style="Hint.TLabel", wraplength=px(440),
                                      justify="left")
        self.status_label.grid(row=4, column=0, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, sticky="e", pady=(px(12), 0))
        self.ok_btn = ttk.Button(buttons, text="Выбрать", style="Accent.TButton", command=self._on_ok,
                                 state="disabled")
        self.ok_btn.pack(side="left", padx=(0, px(8)))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.tree.tree.bind("<Double-1>", lambda _e: self._on_ok())
        self.tree.fill_roots()
        if current:
            self.tree.reveal(current)        # текущее место раскрыто и выделено
        center_window(self, parent=parent or app.root)
        self.grab_set()

    def _regrab(self) -> None:
        """Окно входа могло забрать захват себе: возвращаем его, когда оно закрылось."""
        if self.winfo_exists() and not (self.app.login.dialog_open or self.app.error_open):
            self.grab_set()

    def _on_status(self, text: str, error: bool) -> None:
        if text == TREE_HINT:
            text = ""
        self.status_var.set(text)
        self.status_label.configure(style="Error.TLabel" if error else "Hint.TLabel")

    def _on_pick(self, node: Node) -> None:
        if node.is_project:
            self.picked = None
            self.place_var.set(PLACE_IS_PROJECT)
            self.ok_btn.configure(state="disabled")
            return
        self.picked = list(node.segments)
        self.place_var.set("Выбрано: " + "/".join(node.segments))
        self.ok_btn.configure(state="normal")

    def _on_ok(self) -> None:
        if self.picked:
            self.result = self.picked
            self.destroy()
