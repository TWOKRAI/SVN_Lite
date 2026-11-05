"""Долгие операции записи в сервере MCP: ждать недолго, остальное в фоне (Task AG.5, шаг 8).

Клиент MCP ждёт ответа ограниченное время, а получение большого проекта или создание проекта на сервере идёт минуты.
`JobBoard.run` запускает операцию в потоке-демоне и ждёт `wait` секунд: успела - возвращает её ответ, нет - ответ
`{"state": "running", "job_id": ...}`, и клиент спрашивает `job_status`. Отмены нет (операция записи
останавливается только по своему времени). Задачи живут в памяти процесса сервера; готовые хранятся час. В консольной
программе операции синхронные, этот модуль не используется. Одновременно работает не больше MAX_RUNNING задач.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import OpsError, from_exception
from .result import EXIT_FOLDER_BUSY, Result

log = logging.getLogger("svnhelper.ops")

WAIT_SECONDS = 20.0
KEEP_SECONDS = 3600.0
MAX_RUNNING = 4
POLL = 0.1


@dataclass
class Job:
    id: str
    tool: str
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[Result] = None
    finished: float = 0.0


class JobBoard:
    def __init__(self) -> None:
        self._jobs: dict = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for key in [k for k, j in self._jobs.items() if j.done.is_set() and now - j.finished > KEEP_SECONDS]:
            del self._jobs[key]

    def start(self, tool: str, runner: Callable[[], Result]) -> Job:
        """Запускает runner в потоке-демоне. Слишком много работающих задач: OpsError (код 8, можно повторить)."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if sum(1 for j in self._jobs.values() if not j.done.is_set()) >= MAX_RUNNING:
                raise OpsError("busy", EXIT_FOLDER_BUSY, "Помощник уже выполняет несколько долгих действий.",
                               "Подождите, пока они закончатся, и повторите.", retryable=True)
            job = Job(secrets.token_hex(8), tool, now)
            self._jobs[job.id] = job

        def work() -> None:
            try:
                job.result = runner()
            except BaseException as exc:             # noqa: BLE001  (причина в ответе и в журнале)
                log.exception("Долгая операция %s завершилась ошибкой", tool)
                job.result = Result.failure(tool, from_exception(exc))
            finally:
                job.finished = time.monotonic()
                job.done.set()

        threading.Thread(target=work, name="svnlite-job", daemon=True).start()
        return job

    def get(self, job_id: object) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id) if isinstance(job_id, str) else None

    @staticmethod
    def running_result(job: Job) -> Result:
        spent = int(time.monotonic() - job.started)
        return Result(job.tool, data={"state": "running", "job_id": job.id, "seconds": spent},
                      text=f"Действие ещё выполняется ({spent} с). Спросите о его ходе через job_status с номером {job.id}.")

    def status(self, job_id: object) -> Result:
        """Ответ job_status: готовый ответ операции или «ещё выполняется»; номера нет - ошибка."""
        job = self.get(job_id)
        if job is None:
            error = from_exception(OpsError("not_found", 1, "Такого действия нет: оно уже забыто или номер неверный.",
                                            "Проверьте состояние проекта командой состояния."))
            return Result.failure("job_status", error)
        if not job.done.is_set():
            return self.running_result(job)
        result = job.result or Result.failure(job.tool, from_exception(RuntimeError("no result")))
        result.data.setdefault("job_id", job.id)
        return result

    async def run(self, tool: str, runner: Callable[[], Result], wait: float = WAIT_SECONDS) -> Result:
        """Запускает и ждёт до wait секунд (не блокируя цикл событий)."""
        import asyncio
        job = self.start(tool, runner)
        deadline = time.monotonic() + wait
        while not job.done.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(POLL)
        if job.done.is_set():
            result = job.result or Result.failure(tool, from_exception(RuntimeError("no result")))
            return result
        return self.running_result(job)
