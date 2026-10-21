"""Помощники для тестов одновременных операций (только PostgreSQL).

run_together — запускает операции одновременно, каждую в своём потоке и
своём соединении с базой. Ошибки потоков возвращаются тесту, тест ждёт
завершения всех работников, а зависание превращается в провал, а не в
бесконечное ожидание: у каждого соединения ограничено ожидание блокировки
(lock_timeout) и время запроса (statement_timeout), у потоков — время работы.

run_overlapping — доказывает, что опасное пересечение действительно
произошло. Первая операция останавливается в заданной точке, уже держа свои
блокировки (точка — функция, которую операция вызывает после блокировок).
Вторая операция запускается и должна встать в ожидание блокировки: это
проверяется по pg_stat_activity. Только после этого первая отпускается.
Если вторая не ждала (пересечения не было), тест проваливается.
"""
import threading
import time
from unittest import mock

from django.db import connection, connections

LOCK_TIMEOUT = "20s"
STATEMENT_TIMEOUT = "60s"
THREAD_TIMEOUT = 60


def _limit_waiting():
    """Ограничения ожидания для соединения текущего потока."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        cursor.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")


class ConcurrencyMixin:
    """Для TransactionTestCase: каждый поток видит закоммиченные данные теста."""

    def _start(self, operation, index, results, barrier=None):
        def worker():
            try:
                _limit_waiting()
                if barrier is not None:
                    barrier.wait(timeout=10)
                operation()
            except Exception as exc:  # noqa: BLE001 — любые ошибки, в т.ч. взаимные блокировки
                results[index] = exc
            finally:
                connections.close_all()      # своё соединение потока

        thread = threading.Thread(target=worker, name=f"op-{index}", daemon=True)
        thread.start()
        return thread

    def _join(self, threads):
        for thread in threads:
            thread.join(timeout=THREAD_TIMEOUT)
        alive = [thread.name for thread in threads if thread.is_alive()]
        self.assertEqual(alive, [], "операция зависла — возможна взаимная блокировка")

    def run_together(self, *operations):
        """Операции стартуют одновременно. Возвращает [None | исключение]."""
        barrier = threading.Barrier(len(operations))
        results = [None] * len(operations)
        threads = [self._start(op, i, results, barrier) for i, op in enumerate(operations)]
        self._join(threads)
        return results

    def run_together_watching(self, *operations):
        """Как run_together, но пока операции идут, следит по pg_stat_activity,
        вставала ли какая-нибудь из них в ожидание блокировки.
        Возвращает (результаты, было ли ожидание)."""
        barrier = threading.Barrier(len(operations))
        results = [None] * len(operations)
        threads = [self._start(op, i, results, barrier) for i, op in enumerate(operations)]
        waited = False
        deadline = time.monotonic() + THREAD_TIMEOUT
        while time.monotonic() < deadline and any(thread.is_alive() for thread in threads):
            if not waited and self._lock_waiters():
                waited = True
            time.sleep(0.02)
        self._join(threads)
        return results, waited

    def _lock_waiters(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            )
            return cursor.fetchone()[0]

    def run_overlapping(self, first, second, pause_on):
        """first доходит до pause_on=(объект, имя функции) и ждёт; запускается
        second; убеждаемся, что second ждёт блокировку; first продолжается.

        Возвращает [результат first, результат second] — None или исключение.
        """
        target, name = pause_on
        original = getattr(target, name)
        reached, release = threading.Event(), threading.Event()
        owner = {}

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            if threading.current_thread() is owner.get("thread") and not reached.is_set():
                reached.set()
                release.wait(timeout=THREAD_TIMEOUT)
            return result

        results = [None, None]
        threads = []
        with mock.patch.object(target, name, paused):
            threads.append(self._start(first, 0, results))
            owner["thread"] = threads[0]
            reached_in_time = reached.wait(timeout=20)
            if not reached_in_time:
                release.set()
                self._join(threads)
                self.fail(f"первая операция не дошла до точки остановки ({name}): {results[0]!r}")
            threads.append(self._start(second, 1, results))
            deadline = time.monotonic() + 20
            waited = False
            while time.monotonic() < deadline and threads[1].is_alive():
                if self._lock_waiters():
                    waited = True
                    break
                time.sleep(0.05)
            release.set()
            self._join(threads)
        self.assertTrue(
            waited,
            "вторая операция не ждала блокировку первой — пересечения не было "
            f"(результаты: {results!r})",
        )
        return results
