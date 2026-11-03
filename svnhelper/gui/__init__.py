"""Окно программы (tkinter). Логика svn живёт в svn_core и server, здесь только интерфейс.

Правила потоков: svn и сеть — только в фоновых потоках; результаты возвращаются
в главный поток через queue и опрос after(); диалоги открываются только в главном потоке.

Модули: app (главное окно), download_tab (подвкладка «Скачать проект»), projects_tab (колонки и «Мои проекты»), project_panel и lines_tab
(колонка 3 «Состояние проекта», в том числе вкладка «Ветки»; подмешаны в App),
widgets (диалоги и ButtonRow), login (общее окно входа), models (константы, задачи, чистые функции).
"""
from .app import App, enable_dpi_awareness, exit_if_threads_alive, main

__all__ = ["App", "enable_dpi_awareness", "exit_if_threads_alive", "main"]
