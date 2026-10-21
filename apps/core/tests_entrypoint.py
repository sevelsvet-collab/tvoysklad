"""Запуск контейнера: docker/entrypoint.sh при ошибке миграций.

Сама проверка — docker/test_entrypoint.sh (подставные python, sleep и
приложение; без базы и без ожидания). Здесь она запускается вместе с
остальными тестами. Нужен bash: в образе приложения он есть, на Windows —
bash из Git.
"""
import shutil
import subprocess
from pathlib import Path
from unittest import SkipTest

from django.conf import settings
from django.test import SimpleTestCase

SCRIPT = Path(settings.BASE_DIR) / "docker" / "test_entrypoint.sh"


def _bash():
    bash = shutil.which("bash")
    if bash is None:
        raise SkipTest("bash не найден — проверка docker/entrypoint.sh не выполнялась")
    try:
        probe = subprocess.run([bash, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkipTest(f"bash не запускается ({exc}) — проверка docker/entrypoint.sh не выполнялась")
    if probe.stdout.strip() != "ok":
        # например, заглушка WSL в Windows без установленного дистрибутива
        raise SkipTest("bash не работает — проверка docker/entrypoint.sh не выполнялась")
    return bash


class EntrypointTests(SimpleTestCase):
    def test_migration_failure_stops_start_and_retries_recover(self):
        """Три сценария: сразу успех; ошибки, затем успех; все попытки неуспешны."""
        result = subprocess.run(
            [_bash(), SCRIPT.name], cwd=SCRIPT.parent, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Все проверки пройдены", result.stdout)
        for scenario in ("1) Миграции успешны", "2) Три ошибки, затем успех", "3) Все попытки migrate неуспешны"):
            self.assertIn(scenario, result.stdout)
