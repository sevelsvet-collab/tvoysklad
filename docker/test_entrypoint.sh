#!/usr/bin/env bash
# Проверка docker/entrypoint.sh без базы и без сервера.
#
# Вместо настоящих команд подставляются:
#   python   — записывает вызов в журнал; на «manage.py migrate» отвечает
#              ошибкой заданное число раз (FAKE_MIGRATE_FAILURES: число или all);
#   sleep    — только записывает вызов, не ждёт;
#   fake-app — «приложение»: записывает, что запущено.
# Проверяются код завершения и фактические вызовы, а не текст скрипта.
#
#   bash docker/test_entrypoint.sh [путь к проверяемому entrypoint.sh]
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="${1:-$HERE/entrypoint.sh}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"

cat > "$WORK/bin/python" <<'EOF'
#!/usr/bin/env bash
if [ "${2:-}" = "migrate" ]; then
  count=$(( $(cat "$FAKE_STATE" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$FAKE_STATE"
  if [ "$FAKE_MIGRATE_FAILURES" = "all" ] || [ "$count" -le "$FAKE_MIGRATE_FAILURES" ]; then
    echo "migrate fail" >> "$CALL_LOG"
    echo "fake: база недоступна" >&2
    exit 1
  fi
  echo "migrate ok" >> "$CALL_LOG"
  exit 0
fi
echo "python $*" >> "$CALL_LOG"
exit 0
EOF
cat > "$WORK/bin/sleep" <<'EOF'
#!/usr/bin/env bash
echo "sleep $*" >> "$CALL_LOG"
EOF
cat > "$WORK/bin/fake-app" <<'EOF'
#!/usr/bin/env bash
echo "app $*" >> "$CALL_LOG"
EOF
chmod +x "$WORK/bin/python" "$WORK/bin/sleep" "$WORK/bin/fake-app"

FAILED=0
CODE=0

# run <ошибок migrate>: запускает entrypoint, заполняет CODE, журнал — $WORK/calls.log
run() {
  : > "$WORK/calls.log"
  rm -f "$WORK/state"
  CALL_LOG="$WORK/calls.log" FAKE_STATE="$WORK/state" FAKE_MIGRATE_FAILURES="$1" \
    DJANGO_SUPERUSER_USERNAME=admin DJANGO_SUPERUSER_PASSWORD=test-only \
    PATH="$WORK/bin:$PATH" \
    bash "$ENTRYPOINT" fake-app --serve > "$WORK/out.txt" 2>&1
  CODE=$?
}

count() { grep -cx "$1" "$WORK/calls.log"; }

check() {   # check <описание> <ожидаемое> <фактическое>
  if [ "$2" = "$3" ]; then
    echo "  ok   $1: $3"
  else
    echo "  FAIL $1: ожидалось $2, получено $3"
    FAILED=1
  fi
}

no_migrate_after_success() {
  # после «migrate ok» не должно быть ни одной попытки migrate
  awk 'seen && /^migrate/ {bad=1} /^migrate ok$/ {seen=1} END {print bad ? "есть" : "нет"}' "$WORK/calls.log"
}

echo "Проверяется: $ENTRYPOINT"

echo "1) Миграции успешны с первой попытки"
run 0
check "код завершения" 0 "$CODE"
check "попыток migrate" 1 "$(count 'migrate ok')"
check "неуспешных migrate" 0 "$(count 'migrate fail')"
check "пауз между попытками" 0 "$(grep -c '^sleep' "$WORK/calls.log")"
check "collectstatic" 1 "$(count 'python manage.py collectstatic --noinput')"
check "createsuperuser" 1 "$(count 'python manage.py createsuperuser --noinput')"
check "приложение запущено" 1 "$(count 'app --serve')"

echo "2) Три ошибки, затем успех"
run 3
check "код завершения" 0 "$CODE"
check "неуспешных migrate" 3 "$(count 'migrate fail')"
check "успешных migrate" 1 "$(count 'migrate ok')"
check "migrate после успеха" "нет" "$(no_migrate_after_success)"
check "пауз между попытками" 3 "$(grep -c '^sleep' "$WORK/calls.log")"
check "collectstatic" 1 "$(count 'python manage.py collectstatic --noinput')"
check "приложение запущено" 1 "$(count 'app --serve')"
check "приложение — последний вызов" "app --serve" "$(tail -n 1 "$WORK/calls.log")"

echo "3) Все попытки migrate неуспешны"
run all
if [ "$CODE" -ne 0 ]; then nonzero="да"; else nonzero="нет"; fi
check "код завершения ненулевой" "да" "$nonzero"
check "попыток migrate" 30 "$(count 'migrate fail')"
check "пауз между попытками" 29 "$(grep -c '^sleep' "$WORK/calls.log")"
check "collectstatic не вызывался" 0 "$(count 'python manage.py collectstatic --noinput')"
check "createsuperuser не вызывался" 0 "$(count 'python manage.py createsuperuser --noinput')"
check "приложение не запущено" 0 "$(count 'app --serve')"
if grep -q "миграции базы данных не выполнены" "$WORK/out.txt"; then message="есть"; else message="нет"; fi
check "сообщение об ошибке" "есть" "$message"

if [ "$FAILED" -ne 0 ]; then
  echo "--- вывод последнего запуска ---"
  cat "$WORK/out.txt"
  echo "ПРОВАЛ"
  exit 1
fi
echo "Все проверки пройдены"
