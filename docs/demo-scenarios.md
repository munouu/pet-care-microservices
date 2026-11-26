# Демонстрация Pet Care

Запустить `python scripts/run.py` или `docker compose up --build -d`. Импортировать коллекцию и окружение из `postman/`, выбрать `Pet Care`. Открыть терминал с логами. Выполнить папку «0. Подготовка» один раз.

| Сценарий | Последовательность Postman | Ожидаемый результат |
|---|---|---|
| Успех | Создание записи → результат PSP SUCCEEDED → GET статуса → уведомления | PENDING_PAYMENT → CONFIRMED, PAID, слот BOOKED, push SENT |
| Отказ | Создание второй записи → PSP FAILED → GET статуса → свободные слоты | PENDING_PAYMENT → CANCELLED, FAILED, слот снова FREE |

Подождать 1–3 с перед проверкой итогового статуса; если событие ещё не обработано, повторить GET. В логах показать `RECEIVED`, REST между сервисами, `PUBLISH payment.completed` / `payment.failed`, `CONSUME`, `BOOKED` / `RELEASED`, `CONFIRMED` / `CANCELLED`, отправку push. Один `correlationId` связывает запрос создания записи с её событиями. REST между бизнес-сервисами реальный; брокер, авторизация, PSP и FCM заменены моками (README, раздел 8.3).
