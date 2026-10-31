# Hermes Chat

Веб-интерфейс для работы с установленным **Hermes Agent**: пользователь загружает данные, задаёт вопрос обычным языком и получает ответ и, при необходимости, файлы с результатами анализа.

Документация проекта:

- [Architecture.md](Architecture.md) — как решение устроено сейчас и как его можно развить.
- [Criteria.md](Criteria.md) — сценарии и технические критерии с текущими результатами проверки.
- [TestCases.md](TestCases.md) — шесть вопросов из `questions.txt`, правила проверки и фактический статус прогонов.
- [questions.txt](questions.txt) — исходный список вопросов без изменений.

## Архитектура

- `frontend` — интерфейс для формулировки задачи, прикрепления файлов и просмотра результата.
- `backend` — хранит загруженные файлы и результаты на диске, держит историю сессии в памяти процесса, создаёт сессию Hermes, передаёт запрос и возвращает результаты.
- `Hermes Agent` — единственный исполнитель задачи; его инструменты и навыки выполняются на стороне стандартной установки Hermes.

История сессий находится в памяти backend и не восстанавливается после его перезапуска; файлы и артефакты остаются в `DATA_DIR`. Загруженные входные файлы доступны Hermes через временно авторизованные URL. Токен действует только во время текущего хода и инвалидируется после его завершения или отмены. Файлы результата можно отправить назад в backend по отдельному endpoint и увидеть в чате. Не нужен общий каталог между хостом Hermes и Docker-контейнером.

## Требования

Нужны установленный и настроенный Hermes Agent, доступная модель `pvx-ai-test:latest`, Docker Compose v2 для контейнерного запуска или Python 3.11+ для запуска без Docker.

## Настройка Hermes Agent

Проверьте, что модель доступна у выбранного Hermes-провайдера, затем задайте модель и размер контекста:

```powershell
hermes config set model.default pvx-ai-test:latest
hermes config set model.context_length 65536
```

Размер контекста на стороне Hermes не может увеличить фактическое окно модели. Если используется Ollama, проверьте также контекстный лимит в конфигурации Ollama.

В `~/.hermes/.env` включите штатный API Server и задайте ключ:

```ini
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642
API_SERVER_KEY=replace-with-a-long-random-secret
API_SERVER_MODEL_NAME=hermes-agent
```

Запустите Hermes и проверьте API:

```powershell
hermes gateway
curl.exe -H "Authorization: Bearer replace-with-a-long-random-secret" http://127.0.0.1:8642/v1/health
curl.exe -H "Authorization: Bearer replace-with-a-long-random-secret" http://127.0.0.1:8642/v1/toolsets
curl.exe -H "Authorization: Bearer replace-with-a-long-random-secret" http://127.0.0.1:8642/v1/skills
```

### Включение инструментов для API Server

Hermes Chat не может самостоятельно выдать модели инструменты: их список формируется на стороне Hermes Agent и передаётся в API Server. Если модель отвечает, что Python запрещён, или печатает `execute_code:` как текст, проверьте фактический список через `/v1/toolsets`.

Из каталога проекта запустите:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_hermes_toolsets.ps1
```

Для интерактивного включения инструментов:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable_hermes_tools.ps1
```

В менеджере Hermes выберите платформу `api_server` и включите:
- `code_execution` — предоставляет `execute_code` для Python/pandas и статистики;
- `terminal` — команды оболочки, скачивание входных файлов и проверка окружения;
- `file` — `read_file`, `write_file`, `patch`, `search_files`;
- `web` — `web_search`, `web_extract`;
- `skills` — поиск и чтение навыков;
- `delegation` — подзадачи, если нужны;
- `memory` — память, если нужна.

Можно также включить `browser`, `vision`, `image_gen`, `todo` и `session_search` по необходимости. После изменения конфигурации перезапустите Hermes Gateway и повторите `check_hermes_toolsets.ps1`. Проверяйте именно инструменты в ответе API Server, а не только наличие их названий в `config.yaml`: выключение отдельных tools может перекрывать включённый toolset. Имена toolsets и список доступных инструментов зависят от установленной версии Hermes.

`execute_code` следует использовать для локальных вычислений; сетевые загрузки выполняются через `terminal`. Если файловая система `execute_code` не совпадает с файловой системой `terminal`, обрабатывайте файл в среде, где он реально доступен.

Для проверки Chat Completions и диагностики 503 на Windows PowerShell выполните из каталога проекта:

```powershell
.\scripts\check_hermes_api.ps1
```

Скрипт формирует тестовый JSON через `ConvertTo-Json` и передаёт UTF-8 байты через `Invoke-RestMethod`, поэтому внутренние кавычки JSON не теряются при обработке аргументов `curl.exe`. Клиент backend явно обходит переменные HTTP proxy для loopback, private и link-local адресов (включая `127.0.0.1`), потому что HTTPX по умолчанию наследует настройки прокси из окружения. Для внешнего `HERMES_BASE_URL` поддержка системного прокси сохранена.

В `.env` проекта установите `HERMES_API_KEY` равным `API_SERVER_KEY`. `HERMES_API_MODEL=hermes-agent` — имя API-псевдонима; `HERMES_MODEL=pvx-ai-test:latest` — метка для интерфейса. Модель фактически выбирается конфигурацией Hermes Agent. Hermes API Server использует базовый промпт, навыки и инструменты установленного агента. Приложение передаёт дополнительную инструкцию только для текущей задачи.

## Запуск в Docker

```powershell
Copy-Item .env.example .env
```

Укажите правильный `HERMES_API_KEY`, затем:

```powershell
docker compose up --build -d
```

Откройте <http://localhost:8080>. Backend опубликован только на `127.0.0.1:8000`, чтобы локальный Hermes Agent мог безопасно скачивать входные файлы и отправлять готовые артефакты. `HERMES_CALLBACK_BASE_URL` формируется Compose с учётом `BACKEND_PORT`; менять его вручную для стандартного запуска не нужно.

Остановка:

```powershell
docker compose down
```

## Запуск без Docker

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Copy-Item .env.example .env
```

Проверьте `HERMES_API_KEY` и запустите интерфейс:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -KeepRunning
```

Скрипт запускает только backend и статический frontend. Hermes Agent работает отдельно через `hermes gateway`. Интерфейс будет доступен по адресу <http://127.0.0.1:8085/?api=http://127.0.0.1:8000/api>. Для проверки реального запроса модели добавьте `-RunSmokeTest`.

## Как проходит задача

1. Пользователь создаёт новую сессию, формулирует задачу и прикрепляет файлы.
2. Backend сначала запрашивает `GET /v1/capabilities`. Если Hermes поддерживает Runs API, задача создаётся через `POST /v1/runs`, а backend опрашивает `GET /v1/runs/{run_id}` до терминального состояния. В ответ пользователю попадает только `output` у `status=completed`; tool events и промежуточные сообщения не показываются как результат. Для сохранения непрерывности диалога передаётся `X-Hermes-Session-Key`, а для безопасной идентификации запуска — `Idempotency-Key`.
3. Если Runs API недоступен, клиент использует Responses API, когда он объявлен, а затем совместимый резервный путь Chat Completions (`stream: false`). В этих режимах backend выбирает только финальные текстовые поля и не выдаёт reasoning, tool arguments или tool output за ответ. HTTP 5xx, сетевой обрыв и неизвестное состояние не запускают автоматический повтор задачи.
4. Если запрос с вложением завершается только текстом-планом («Начинаю с шага 1») и не появился артефакт, backend делает одну ограниченную попытку продолжения в той же сессии. Если и вторая попытка возвращает план, интерфейс показывает конкретную ошибку вместо плана под видом результата.
5. Для сетевого доступа к вложениям Hermes использует `terminal` (`curl.exe` в Windows или `curl` в POSIX shell), а не `execute_code`: его среда может ограничивать сетевые операции. После скачивания агент проверяет файл и анализирует локальную копию. Если `terminal` отключён для `api_server`, сначала включите его и повторите короткий тест; повторная загрузка CSV не требуется.
6. При удалённом terminal backend адрес из `HERMES_CALLBACK_BASE_URL` должен быть достижим из той среды. `127.0.0.1` указывает на саму среду выполнения и не является адресом хоста во всех конфигурациях.
7. При необходимости Hermes отправляет графики, таблицы или документы в backend через указанный endpoint артефактов. Ответ Hermes и загруженные артефакты отображаются в интерфейсе.

Поддерживаются CSV, TSV, TXT, Markdown, JSON, YAML, XML, PDF, DOCX, XLSX, XLS, PPTX, изображения, ZIP и распространённые файлы исходного кода. Содержимое файлов обрабатывает сам Hermes своими инструментами. Успешная загрузка файла не гарантирует, что в конкретной установке Hermes есть все библиотеки или навыки для его формата.


### Проверка доступных инструментов Hermes

Если Hermes пишет, что не может скачать вложение, проверьте наличие `terminal` в toolset платформы `api_server`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_hermes_toolsets.ps1
```

Скрипт проверит `GET /v1/toolsets` и выведет доступные наборы инструментов. Если `terminal` не перечислен или выключен, запустите `hermes tools` и включите `terminal` для платформы `api_server`, затем перезапустите `hermes gateway` и повторите проверку. В версиях Hermes, поддерживающих команду управления инструментами, можно использовать `hermes tools enable terminal --platform api_server`. Сверяйтесь с выводом `/v1/toolsets`, поскольку формат настроек может отличаться по версии.

## Базовый промпт

Если базовый промпт уже настроен в `~/.hermes/SOUL.md` или конфигурации профиля Hermes, приложение не дублирует его. Оно добавляет системную инструкцию для поведения оболочки. Если у вас есть отдельный файл промпта, который Hermes автоматически не загружает, добавьте его в `SOUL.md` либо включите в настройки основного профиля Hermes.

## Настройки

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `HERMES_BASE_URL` | `http://127.0.0.1:8642/v1` | адрес Hermes API при запуске без Docker |
| `HERMES_DOCKER_BASE_URL` | `http://host.docker.internal:8642/v1` | адрес Hermes API из Docker |
| `HERMES_API_KEY` | `change-me-local-dev` | Bearer-ключ API Server Hermes |
| `HERMES_MODEL` | `pvx-ai-test:latest` | имя модели в интерфейсе |
| `HERMES_API_MODEL` | `hermes-agent` | API-псевдоним Hermes |
| `HERMES_CALLBACK_BASE_URL` | `http://127.0.0.1:8000/api` | адрес backend, доступный самому Hermes для обмена файлами |
| `HERMES_TIMEOUT_S` | `1800` | максимальная длительность одного обращения к Hermes; действует как общий deadline для долгих задач |
| `MAX_UPLOAD_MB` | `20` | максимальный размер входного файла |
| `ARTIFACT_MAX_MB` | `12` | максимальный размер файла результата |
| `MAX_TABLE_ROWS` / `MAX_TABLE_COLS` | `200` / `40` | лимиты табличного предпросмотра |
| `PREVIEW_ROWS` | `5` | число строк предпросмотра CSV/TSV |
| `DATA_DIR` | `./data` | локальные загруженные файлы и результаты |
| `FRONTEND_PORT` | `8080` | порт интерфейса Docker |
| `BACKEND_PORT` | `8000` | локальный порт callback API |

Для файлов датасета больше 20 МБ увеличьте `MAX_UPLOAD_MB` в `.env` (например, до `250`). Учитывайте, что текущий backend читает CSV целиком в память при первичном профилировании. Для больших файлов запускайте backend на машине с достаточным запасом RAM; потоковая загрузка и обработка больших CSV — часть дальнейшего развития архитектуры, а не гарантия текущего прототипа.

Клиент выбирает протокол по `GET /v1/capabilities`: Runs API → Responses API → резервный Chat Completions. Для Runs используется отдельный `run_id` и polling статуса `completed`; непрерывность сессии привязана к `X-Hermes-Session-Key`. Для каждого запуска передаётся `Idempotency-Key`, но backend не пересылает неизвестный запуск заново автоматически. `HERMES_TEMPERATURE` и `HERMES_MAX_TOKENS` не являются параметрами, которые API-адаптер Hermes Agent v0.22.5 применяет из Chat Completions; настройки модели и контекста меняйте в самом Hermes/провайдере.

Если инструменты Hermes выполняются не на том же хосте, где запущен backend, `HERMES_CALLBACK_BASE_URL` должен быть адресом, доступным из среды выполнения инструментов Hermes. Не публикуйте callback API в общедоступную сеть без отдельной защиты.

## API приложения

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/api/health` | состояние Hermes API |
| `GET` | `/api/config` | конфигурация модели и лимитов |
| `POST` | `/api/sessions` | создать беседу |
| `GET` | `/api/sessions/{id}` | получить историю беседы |
| `DELETE` | `/api/sessions/{id}` | удалить сессию и её файлы |
| `POST` | `/api/sessions/{id}/files` | прикрепить файл |
| `GET` | `/api/sessions/{id}/files/{file_id}/content` | дать Hermes содержимое файла по токену задачи |
| `DELETE` | `/api/sessions/{id}/files/{file_id}` | удалить вложение |
| `PUT` | `/api/sessions/{id}/artifacts/{name}` | принять готовый файл от Hermes по токену задачи |
| `POST` | `/api/sessions/{id}/messages` | отправить задачу; ответ идёт через SSE |
| `GET` | `/api/artifacts/{id}/{artifact_id}` | скачать результат |

## Проверки

```powershell
python -m unittest discover -s tests -v
python -m compileall -q backend scripts tests
node --check frontend\app.js
```

Сквозной тест с настоящей моделью:

```powershell
python scripts\smoke_test.py --base http://127.0.0.1:8000 --csv samples\sales.csv
```

## Безопасность

Backend выдаёт Hermes случайный токен текущей задачи. Пока запрос обрабатывается, токен позволяет читать входные файлы только этой сессии и загружать результаты в неё же; по окончании или отмене хода токен инвалидируется. Входные имена файлов нормализуются, размеры файлов ограничены, а исполнение кода выполняется собственными средствами Hermes. При этом Hermes использует полномочия своих инструментов — не подключайте недоверенных пользователей к агенту, который имеет доступ к личным файлам, секретам или административным командам.


### v10 заметки

- Исправлено определение незавершённых ответов, включая `Plan.md:` и текстовые псевдовызовы `execute_code:`. Такие ответы не должны показываться как успешно завершённая задача.
- Статические файлы используют cache-busting query `?v=10.0.0`, чтобы браузер не применял старые CSS/JS.
