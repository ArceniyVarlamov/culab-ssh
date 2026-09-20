# culab — заметка для AI-агентов

Этот репозиторий — мост к JupyterHub `jupyter.culab.ru`. У тебя нет SSH и нет sudo на сервере; ты ходишь туда через WebSocket terminal, авторизуясь `JUPYTERHUB_TOKEN` из `~/.culab-env`.

**Единственная точка входа — обёртка `./culab`.** Не дёргай `python3 jupyter_terminal_exec.py` напрямую и не пиши собственные WebSocket-вызовы — это устаревший путь, он зальёт твой контекст PTY-мусором.

Параллельным агентам давай разные стабильные session names: `./culab --session codex-aamas exec "pwd"`.
Каждая session переиспользует собственный terminal; случайное новое имя на каждый вызов не создавать.

## Когда использовать

- Запустить команду на сервере → `./culab exec`.
- Запустить долгую задачу (обучение, скачивание, билд) → `./culab spawn` + опрос.
- Залить локальный проект → `./culab push LOCAL_DIR REMOTE_DIR`.
- Прочитать файл с сервера → `./culab exec "cat /path/to/file"`.
- Записать файл на сервер маленький → `./culab exec` + heredoc, большой → `./culab push` целого каталога.

## Команды и контракт вывода

| команда | что печатает в stdout | exit code |
|---|---|---|
| `./culab exec "<cmd>" [--cwd PATH] [--timeout SEC]` | **чистый stdout** удалённой команды; stderr на stderr | **rc удалённой команды** (не 0, если упало) |
| `./culab spawn "<cmd>" [--cwd PATH]` | только `job_id` (8-символьный hex) | 0 если стартовал |
| `./culab status JID` | `{"running":true,"pid":N}` или `{"running":false,"rc":0,"pid":N}` | 0 |
| `./culab log JID [--tail N] [--grep PATTERN]` | сырой stdout+stderr процесса | 0 |
| `./culab kill JID [--signal SIGTERM]` | `{"ok":true,"signal":"..."}` | 0 |
| `./culab jobs` | `{"jobs":[...]}` — что запомнил демон | 0 |
| `./culab reap JID` | `{"ok":true}` | 0 |
| `./culab push LOCAL_DIR REMOTE_DIR [--exclude PATH]...` | одна строка `pushed N files (B bytes, C chunks) -> REMOTE_DIR` | 0 если sha256 совпал |
| `./culab ping` | `{"pong":true,"ts":...,"pid":...}` (PID серверного демона) | 0 |
| `./culab terminals` | JSON со всеми терминалами в JupyterHub, наш помечен `"ours":true` | 0 |
| `./culab cleanup --ours \| --name X` | `{"ok":true,"removed":[...]}` | 0 |
| `./culab reset` | `{"ok":true,"removed_terminal":"..."}` | 0 |

Глобальный `--session NAME` ставится перед командой и изолирует terminal/state. Без него используется
session `default` — это обратная совместимость для `rig`.

Обещание: stdout не содержит ANSI, prompt'ов, эха, маркеров — только то, что напечатала команда.

## Правила времени

- **Команда заведомо < ~20 секунд** → `exec`. Дефолтный `--timeout=120`.
- **Команда может длиться дольше** или вообще unbounded → `spawn`. Иначе ты подвесишь WebSocket и/или поймаешь таймаут.
- При работе со `spawn`:
  - Большой лог читай с `--tail N` или `--grep`, **не целиком**. Лог хранится на сервере в `~/.cache/culab-jobs/<jid>.log` и переживает рестарт демона.
  - `status` — это in-memory таблица демона; **после рестарта демона** (рассинхронизация WS, pod redeploy) `status` для старых job вернёт `{"error":"unknown_job"}`. Лог при этом цел — читай его.
  - Когда задача больше не нужна — `reap` чтобы убрать лог-файл с диска.

## Push: что фильтруется

`./culab push` пакует только «код», фильтр зашит в `culab_rpc.py:_should_include`:
- Включает: `*.py *.ipynb *.toml *.lock *.md *.txt *.yaml *.yml *.json *.ini *.cfg *.sh`, `.gitignore .dockerignore .python-version`, `Dockerfile Makefile`, **а также `Dockerfile.* / *.Dockerfile / Makefile.*`** (например `Dockerfile.vllm`).
- Исключает: `.git .venv venv __pycache__ node_modules outputs artifacts checkpoints models .pytest_cache .mypy_cache .ruff_cache .DS_Store`, `*.csv *.parquet *.pkl *.pickle *.joblib *.db *.zip *.tgz *.tar *.gz *.pt *.pth *.ckpt *.safetensors *.onnx`, и `data/*.json` `data/*.npz`.
- Точечно исключить файл — `--exclude path/relative/to/project`.

После распаковки на сервере проверяется `sha256sum -c .codex_push_manifest.sha256` — если хоть один файл побит, `./culab push` упадёт с rc≠0.

## Чего НЕ делать

1. Клиентского лимита частоты больше нет. Проверка 21.09.2026 прошла на 20 REST req/s, 8 одновременных terminal WS и 100 RPC ping без ошибок; это наблюдение, не SLA. При `429`, `5xx` или captcha включить backoff через `CULAB_MIN_INTERVAL`. Для обычного наблюдения чаще раза в 1–5 секунд практической пользы нет.
2. **Никогда `./culab cleanup --all`** — этой опции нет специально. Чужие терминалы (`ours: false`) могут быть твоими собственными активными сессиями. Удаляй только по точному имени или `--ours`.
3. **Не пиши прямые WebSocket-вызовы**, не вызывай `python3 jupyter_terminal_exec.py` руками — PTY-мусор зальёт контекст.
4. **Не читай большие логи целиком**. Всегда `--tail` или `--grep`.

## Восстановление после ошибок

- `RuntimeError: websocket closed` / `EOFError` — встроенный retry уже один раз отработал. Если упал снова — `./culab reset` и повтори. Возможно pod jupyterhub'a рестартанул.
- `tmgrdfrend/showcaptcha` — поймал captcha. Сделай паузу и `./culab reset`; token не проси вставлять в чат.
- `{"error":"sha256_mismatch", ...}` в push — не должно происходить (chunks идемпотентны через `offset`). Если случилось — `./culab reset` и повтори push.
- `unknown_job` от `status`/`log`/`kill` — демон рестартанул и забыл in-memory таблицу. Лог на диске всё ещё есть: `./culab exec "ls ~/.cache/culab-jobs/"` и `./culab exec "tail -50 ~/.cache/culab-jobs/<jid>.log"`.

## Где живёт состояние

- Локально: `~/.cache/culab/rpc.json` для `default` и `rpc-<session>.json` для именованных sessions — terminal и timestamp connect.
- На сервере:
  - демон-процесс живёт в JupyterHub-терминале, имя в state-файле выше.
  - `~/.cache/culab-jobs/<jid>.log` — логи фоновых задач.
  - `~/.cache/culab-jobs/<jid>.cmd` — команда, которой стартовали.

## Переменные окружения

| переменная | по умолчанию | для чего |
|---|---|---|
| `HUB_URL` | `https://jupyter.culab.ru` | endpoint JupyterHub |
| `CULAB_ENV` | `~/.culab-env` | файл с `JUPYTERHUB_TOKEN` |
| `JUPYTERHUB_TOKEN` | из `CULAB_ENV` | Hub REST и terminal REST/WebSocket |
| `CULAB_SESSION` | `default` | имя независимого переиспользуемого terminal |
| `CULAB_MIN_INTERVAL` | `0` | опциональная пауза между WS handshakes |
| `CULAB_CHUNK` | `262144` (256 KB) | размер одного upload-чанка |
| `CULAB_CHUNK_TIMEOUT` | `10` | таймаут на один чанк (сек) |
| `CULAB_MAX_ATTEMPTS` | `3` | попыток на один RPC при сбое WS |
| `CULAB_PTY_PIECE` | `65536` | разбиение больших WS-фреймов |
| `CULAB_PROGRESS` | (нет) | если задан — печатает прогресс push в stderr |

## Полезные шаблоны

**Запустить и проследить тренировку:**
```bash
JID=$(./culab spawn 'cd /home/jovyan/datadojo1 && python3 train.py --epochs 10')
echo "$JID" > /tmp/current-job
# Через какое-то время:
./culab log $(cat /tmp/current-job) --tail 50
./culab status $(cat /tmp/current-job)
# Если надо остановить:
./culab kill $(cat /tmp/current-job)
```

**Залить локальный проект и сразу что-то проверить:**
```bash
./culab push /Users/me/Programming/myproject /home/jovyan/myproject
./culab exec "cd /home/jovyan/myproject && python3 -c 'import myproject; print(myproject.__version__)'"
```

**Удалить один зомби-терминал:**
```bash
./culab terminals    # посмотреть имена
./culab cleanup --name 7
```

## Что под капотом (если очень нужно)

- `culab` — bash-обёртка над `culab_rpc.py`.
- `culab_rpc.py` — клиент. Держит `RpcSession` (один WebSocket на серию вызовов), кэширует имя терминала в `~/.cache/culab/rpc.json`, делает throttle, retry, авто-cleanup мёртвых терминалов. Логика фильтра push (`_should_include`, `_make_tarball`) тоже здесь.
- `culab_rpc_server.py` — серверный демон. Загружается inline через `exec python3 -c "exec(b64decode(...))"`; никаких файлов на сервере не создаётся для bootstrap.
- `jupyter_terminal_exec.py` — низкоуровневый token-auth WS-handshake и framing. Не дёргай напрямую.
