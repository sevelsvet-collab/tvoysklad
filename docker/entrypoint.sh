#!/usr/bin/env bash
set -e

# Миграции: база может подняться не сразу, поэтому несколько попыток.
# Если все попытки неуспешны, приложение НЕ запускается: работать с базой,
# схема которой не соответствует коду, нельзя. (set -e здесь не помогает —
# команда стоит внутри if, её ошибка не прерывает скрипт.)
MIGRATE_ATTEMPTS=30
echo "→ Применяю миграции базы данных..."
migrated=no
for i in $(seq 1 "$MIGRATE_ATTEMPTS"); do
  if python manage.py migrate --noinput; then
    migrated=yes
    break
  fi
  if [ "$i" -lt "$MIGRATE_ATTEMPTS" ]; then
    echo "  Миграции не выполнены (база ещё не готова?), повтор через 2с ($i/$MIGRATE_ATTEMPTS)"
    sleep 2
  fi
done
if [ "$migrated" != yes ]; then
  echo "ОШИБКА: миграции базы данных не выполнены за $MIGRATE_ATTEMPTS попыток." >&2
  echo "Приложение не запущено: статика не собиралась, пользователь не создавался." >&2
  echo "Проверьте журнал выше (доступность базы, ошибка миграции) и запустите контейнер снова." >&2
  exit 1
fi

echo "→ Собираю статические файлы..."
python manage.py collectstatic --noinput

# Создаём суперпользователя, если заданы переменные окружения (идемпотентно)
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  echo "→ Проверяю суперпользователя $DJANGO_SUPERUSER_USERNAME..."
  python manage.py createsuperuser --noinput 2>/dev/null && \
    echo "  Суперпользователь создан" || echo "  Суперпользователь уже существует"
fi

echo "→ Запускаю приложение..."
exec "$@"
