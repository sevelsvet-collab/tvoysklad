#!/usr/bin/env bash
set -e

echo "→ Применяю миграции базы данных..."
for i in $(seq 1 30); do
  if python manage.py migrate --noinput; then
    break
  fi
  echo "  База данных ещё не готова, повтор через 2с ($i/30)"
  sleep 2
done

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
