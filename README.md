# Сервис генерации

Принимает заказ, списывает токены, отдаёт задачу провайдеру и при отказе идёт к следующей «ноге». Если ноги кончились — один возврат. Деньги двигаются только строками журнала, каждая не больше одного раза, даже если вебхук пришёл двадцать раз в четыре воркера.

Пользователей регистрировать не нужно: человек появляется в БД при первой успешной оплате (`status=paid`). Интерфейса нет, файлы — строка-ссылка.

## Что поднимает compose

| Сервис | Внутри сети | С хоста | Процессы |
| --- | --- | --- | --- |
| PostgreSQL | `db:5432` | `localhost:15432` | 1 |
| API | `api:8000` | **`localhost:18000`** | **uvicorn --workers 4** |
| Провайдер alpha | `alpha:8000` | **`localhost:18001`** | 1 |
| Провайдер beta | `beta:8000` | **`localhost:18002`** | 1 |

Порты 18000–18002 выбраны, чтобы не конфликтовать с занятыми 8000–8002 на машине разработчика. В логах контейнера API слушает `:8000` — это нормально.

Секрет подписи вебхуков: `test-secret` (`X-Signature` = hex HMAC-SHA256 сырого тела).  
`PROVIDER_TIMEOUT=60` (для тестов сторожа).

## Запуск

Нужны Docker и Docker Compose.

```bash
cd generation-service
docker compose up --build
```

В другом терминале (или с `-d`) проверка:

```bash
curl http://localhost:18000/health
# {"ok":true}
```

На старте контейнер API делает `alembic upgrade head`, затем:

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Это один контейнер, **четыре процесса-воркера** плюс мастер. Состояние в Postgres, не в RAM воркера. Убедиться:

```bash
docker compose exec api python -c "import os; print([open(f'/proc/{p}/cmdline','rb').read() for p in os.listdir('/proc') if p.isdigit()] )"
```

Должны быть процесс с `--workers 4` и четыре `multiprocessing.spawn`.

Остановка: `docker compose down`. Снести данные БД: `docker compose down -v`.

## Тесты

Четыре обязательных сценария ходят в **живой** стек, не в моки:

1. 20 одновременных одинаковых вебхуков оплаты → одно начисление.
2. 10 одновременных `failed` на последней ноге → один возврат.
3. Отказ первой ноги → успех второй, в истории две попытки.
4. Поздний `completed` после сторожа → задача `failed`, возврат есть, `result_url` пустой.

Тест 4 ждёт `PROVIDER_TIMEOUT` (60 с), весь прогон около двух минут. Не обрывайте его.

### Из контейнера API (проще: зависимости уже стоят)

```bash
docker compose up --build -d
docker compose exec -e API_URL=http://127.0.0.1:8000 \
  -e ALPHA_URL=http://alpha:8000 \
  -e BETA_URL=http://beta:8000 \
  api pytest -q
```

### С хоста

```bash
docker compose up --build -d
pip install -r requirements.txt
pytest -q
```

По умолчанию тесты стучат в `http://localhost:18000` (API), `18001` (alpha), `18002` (beta).

## Каталог моделей (из миграции)

| Модель | Цена | Ноги |
| --- | --- | --- |
| `image-fast` | 2 | alpha, до 4 референсов → beta, до 10 |
| `video-long` | 25 | beta, до 2 → alpha, до 1, `skip_on_moderation` |

## API

Все тела JSON. Вебхуки без подписи → 401.

- `POST /generate` `{user_id, model_slug, prompt, ref_images}` → `{task_id}`.  
  402 нет токенов, 429 уже 3 активные (`running`) задачи, 400 ни одна нога не принимает столько референсов (**до** списания), 404 неизвестный пользователь или модель.
- `POST /webhooks/providers/{alpha|beta}` — результат провайдера.
- `POST /webhooks/payments` `{payment_id, user_id, tokens, status}` — кредит только при `status=paid`. Одна оплата может прийти много раз и сразу.
- `GET /tasks/{id}` — статус, текущая нога, история попыток (провайдер, тип ошибки, время).
- `GET /users/{id}/balance` — баланс и последние строки журнала. Баланс всегда равен сумме журнала.

Фейковые провайдеры принимают `POST /tasks` → сразу `202 {"external_id"}`, через 2–20 с шлют вебхук. Для тестов:

`PUT http://localhost:18001/admin/settings` (или `18002`) — `fail_rate`, `error_type`, `sync_fail` (`503` / `timeout`), `webhook_duplicate`, `webhook_never`, `webhook_late`.

## Короткий смоук руками

Пользователь создаётся оплатой. Сначала платёж, потом `/generate`.

```bash
# 1) начисление 100 токенов (подставьте новые uuid)
python - <<'PY'
import hmac, json, uuid, urllib.request
from hashlib import sha256
API, SECRET = "http://localhost:18000", b"test-secret"
user_id, payment_id = str(uuid.uuid4()), str(uuid.uuid4())
print("user_id", user_id)
payload = {"payment_id": payment_id, "user_id": user_id, "tokens": 100, "status": "paid"}
raw = json.dumps(payload).encode()
sig = hmac.new(SECRET, raw, sha256).hexdigest()
req = urllib.request.Request(API + "/webhooks/payments", data=raw, method="POST",
    headers={"Content-Type": "application/json", "X-Signature": sig})
print(urllib.request.urlopen(req).read())
print(urllib.request.urlopen(API + f"/users/{user_id}/balance").read())
PY
```

Повтор того же `payment_id` не должен увеличить баланс. Затем `POST /generate` с этим `user_id` и `model_slug: image-fast`. Через несколько секунд `GET /tasks/{id}` — `completed` и `result_url`.

## Миграции

Схема только Alembic, не `create_all`. Контейнер API применяет их сам.

С хоста (БД на `localhost:15432`):

```bash
set DATABASE_URL=postgresql+asyncpg://gen:gen@localhost:15432/gen
alembic upgrade head
alembic revision -m "add_something"
# правите alembic/versions/<revision>_add_something.py
alembic upgrade head
```

Alembic подменяет драйвер на `psycopg2`.

## Ровно один раз: начисление и возврат

Источник истины — `ledger_entries`, не поле в памяти.

- Оплата: уникальный индекс по `payment_id`. 20 одинаковых вебхуков → одна строка `payment`.
- Списание: одна строка `charge` на задачу.
- Возврат: одна строка `refund` на задачу.
- `users.balance` считает триггер `SUM(amount)`. Приложение баланс не пишет. `balance >= 0` закрывает гонку двух списаний.

Терминальный статус задачи ставит первый успешный коммит при `status = running`. Остальные вебхуки отвечают 200 и ничего не меняют.

## Падение процесса посреди вебхука

1. Подпись проверяется по сырому телу до записи в БД.
2. Статус задачи и строка журнала — одна транзакция. Нет окна «деньги уже вернули, статус ещё running».
3. Смерть до `COMMIT` → откат. Повтор вебхука снова проходит уникальный индекс / уже терминальный статус, ответ 200.
4. Смерть после `COMMIT`, до HTTP 200 → тот же индекс / CAS.

Сторож крутится в каждом из четырёх воркеров. Просроченные попытки берёт `SELECT ... FOR UPDATE SKIP LOCKED`: одну строку обрабатывает один процесс.

## Правило 6: поздний `completed`

Терминальное состояние неизменно. Если сторож уже сделал `timed_out`, `failed` и возврат, поздний `completed` подтверждается и **игнорируется**: `result_url` не пишется, возврат не откатывается.

Иначе пользователь получил бы и файл, и токены (бесплатная генерация) либо повторное списание после возврата. Поздний успех провайдера — потерянный артефакт, не финансовая операция.
