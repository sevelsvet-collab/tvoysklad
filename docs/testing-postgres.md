# Проверка на PostgreSQL

SQLite не умеет блокировать строки, поэтому тесты одновременных операций
(`apps/inventory/tests_concurrency.py`, `apps/finance/tests_concurrency.py`)
там пропускаются, а их успех на SQLite ничего не доказывает. Их нужно
запускать на PostgreSQL той же версии, что на сервере (16).

## На сервере

Django создаёт и удаляет отдельную базу `test_<имя базы>`; рабочая база
не затрагивается.

```bash
docker compose -f docker-compose.behind-proxy.yml exec web python manage.py test apps.inventory.tests_concurrency apps.finance.tests_concurrency
docker compose -f docker-compose.behind-proxy.yml exec web python manage.py test
```

## На Windows без Docker (как проверялось 14.10.2026)

1. Официальный архив PostgreSQL 16 для Windows с get.enterprisedb.com
   (`postgresql-16.13-1-windows-x64-binaries.zip`, 326 150 816 байт,
   SHA-256 `9b4c7a1bc31147b5b2ba59178d0eef86c16181d54a20177192b387dff20f00a9`).
   Из архива нужны только `pgsql/bin`, `pgsql/lib`, `pgsql/share`.
2. Отдельный кластер во временной папке, от имени текущего пользователя,
   без установки служб:

   ```bash
   initdb -D <папка>/data -U sklad_test --pwfile=<файл с паролем> --auth=scram-sha-256 -E UTF8 --locale=en-US
   ```

   В `postgresql.conf`: `listen_addresses = '127.0.0.1'`, `port = 55432`.
   Локаль обязательно не `C`: в ней PostgreSQL не различает регистр
   кириллицы, и поиск по наименованию («ромашка» → «Ромашка») ложно падает.
3. Запуск и остановка: `pg_ctl -D <папка>/data -l <журнал> -w start` и
   `pg_ctl -D <папка>/data -m fast -w stop`.
4. Тесты — с переменной окружения только для этой команды:

   ```bash
   DATABASE_URL=postgres://sklad_test:<пароль>@127.0.0.1:55432/sklad_conc python manage.py test apps.inventory.tests_concurrency apps.finance.tests_concurrency
   ```

## Как устроены тесты одновременных операций

`apps/core/concurrency_testing.py`:

- каждая операция — в своём потоке и своём соединении с базой; ошибки
  потоков возвращаются тесту; тест ждёт завершения всех потоков;
- у каждого соединения ограничены ожидание блокировки (`lock_timeout`, 20 с)
  и время запроса (`statement_timeout`, 60 с), у потоков — время работы
  (60 с): взаимная блокировка превращается в провал теста, а не в зависание;
- `run_overlapping` доказывает пересечение: первая операция
  останавливается в точке, где уже держит свои блокировки; вторая
  запускается, и тест по `pg_stat_activity` убеждается, что она ждёт
  блокировку. Только потом первая продолжается. Если вторая не ждала,
  тест проваливается — «просто два потока» за проверку не считаются;
- после операций проверяется итоговое состояние базы (остатки, оплата
  счетов, отсутствие несовместимых связей и превышений возвратов).

Порядок блокировок: склад — строка документа, затем товары по возрастанию
id; деньги — платежи, затем кассы, затем счета покупателям, внутри уровня
по возрастанию id.
