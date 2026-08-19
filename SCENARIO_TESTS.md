# Scenario Tests (PST) — Slack Connector

Метод: `Docs/session-notes/SCENARIO_TESTING_STANDARD.md`.

---

## Прогон 2026-08-19

**Существующее покрытие до PST:** 280 тестов в 13 файлах — глубокое
покрытие подключения/авторизации, разрешения неоднозначных каналов,
отправки/чтения сообщений, движка автоответчика, sweeptimer,
inbound e2e, журнала, панелей и прайсинг-контракта. Аудит по точному
имени функции нашёл **14 функций, никогда не тестировавшихся через
свой реальный хендлер** (`test_pricing.py`/`test_contract.py` только
упоминают эти имена в словаре цен / AST-обходе, реальных вызовов нет):

`autoreply_status`, `connect_events`, `create_channel`, `delete_message`,
`edit_message`, `fetch_message`, `fetch_thread_context`,
`invite_to_channel`, `list_users`, `pin_message`, `react_to_message`,
`read_thread`, `set_autoreply`, `set_channel_topic`.

**Новый файл:** `tests/test_pst_scenarios.py` — 25 сценариев (happy,
error, blocked, adversarial по каждой функции), паттерн QueueHTTP из
`test_tools.py`: `auth.test` для резолва воркспейса, затем
`conversations.list` для резолва канала по имени, затем сам вызов.

### 🐛 Найден и исправлен реальный баг

`fetch_message` и `fetch_thread_context` (`handlers_events.py`, строки
88 и 141) при сценарии «сообщение/тред не найдены» ссылались на
несуществующую константу `sc.SLACK_NOT_FOUND` вместо
`sc.SLACK_MESSAGE_NOT_FOUND` — реальный `AttributeError` на
законном, легко достижимом пути (сообщение удалено или ts устарел),
а не аккуратная структурная ошибка. Обнаружено PST-тестом
`test_error_fetch_message_not_found`. Исправлено на месте (обе строки),
тест перезапущен — зелёный.

Также одна собственная ошибка теста: `test_happy_invite_to_channel` не
подставлял HTTP-ответ на резолв пользователя `vlad` перед
`conversations.invite` — добавлен недостающий `http.push(...)`.

### Результат

305/305 тестов зелёные (280 существующих + 25 новых), после fix→rerun
цикла. Публикация — по правилу dual-publish: это правка кода
(`handlers_events.py`), значит обязательны ОБЕ публикации — git commit
И `developer.deploy_app`.

---
