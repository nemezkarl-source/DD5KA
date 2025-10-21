# DD-5KA · Master Prompt & Workflow (единый источник правды)

Этот документ — центральная точка знания проекта.  
Любой ИИ-ассистент обязан работать строго по этому файлу.  
Все изменения кода/конфигурации должны отражаться здесь.

Правило №1: **один ответ ИИ = одна правка/одна команда проверки.**
Правило №2: без изменения архитектуры без RFC.
Правило №3: все пути и окружение фиксируются в этом документе.

## 1) Коротко о проекте

**DD-5KA** — система детекции дронов на базе Raspberry Pi 5.  
Используются камера Sony IMX500 и Python-сервис для анализа видеопотока (YOLO).  
Отдельный Flask-сервис предоставляет веб-панель (MJPEG-видеопоток, снимки и health-чеки).

Текущая цель этапа разработки:  
стабильный запуск панели и детектора, логирование событий и корректная работа камеры.

**CH5-start (CPU-инференс YOLO) — выполнено:**
- Детектор работает на CPU (Ultralytics YOLO), события пишутся в `logs/detections.jsonl`.
- Панель отдаёт оверлей поверх видеопотока: `GET /stream/overlay.mjpg` (порт :8098).
- Схема события `detection` расширена полем `"perf"`.
- Переключение бэкенда: `DD5KA_BACKEND=cpu`.

## §1. Режим работы

**Выполнение на RPi — только one-liner через ssh.** После каждого шага оператор отвечает «готово», затем следующий шаг. Контур неизменен: **Mac → GitHub → RPi (pull) → systemctl**.

## 2) Архитектура и окружение (общая фиксация)

Аппаратная часть:
- Raspberry Pi 5
- Камера Sony IMX500 (через `libcamera` / `rpicam-*`)
- (опционально) AI-ускоритель Hailo-8 PCIe

Софт:
- ОС: Debian 12 (aarch64)
- Детектор: Python + Ultralytics YOLO
- Панель: Flask (веб-интерфейс)
- Менеджер процессов: systemd

Развёртывание:
- Панель и Детектор — это **два отдельных Python-сервиса**
- Каждый работает как systemd unit
- Логи пишутся в отдельную директорию
- Управление сервисами производится через `systemctl`

## §3. Хостинг/пути/сервисы

- Репозиторий: `/home/nemez/DD5KA`
- Симлинк: `/home/nemez/project_root -> /home/nemez/DD5KA`
- Venv: `/home/nemez/drone-env/bin/python`

**Сервисы (systemd):**
- `dd5ka-detector.service` — WorkingDirectory=`/home/nemez/project_root`, запускает `/home/nemez/project_root/src/detector/daemon.py`, лог: `/home/nemez/project_root/logs/detector.log`
- `dd5ka-panel.service` — запускает `/home/nemez/project_root/src/panel/app.py`, лог: `/home/nemez/project_root/logs/panel.log`

## 3) Структура каталогов и важные пути (на Raspberry Pi)

Корень проекта:
`/home/nemez/project_root/`

Основные директории:
/home/nemez/project_root/
src/
panel/ # Flask-панель (веб-UI)
detector/ # YOLO-инференс и обработка кадров
models/ # веса моделей (YOLO HEF/ONNX/PT)
configs/ # .env и конфигурационные файлы
logs/ # логи сервисов и detections.jsonl
docs/ # документация (в т.ч. PROMPT_MASTER)

markdown


Виртуальное окружение детектора:
`/home/nemez/drone-env/`

Сервисы (systemd):
- `dd5ka-panel.service`  → Flask UI
- `dd5ka-detector.service` → YOLO-детектор

Расположение логов по умолчанию:
`/home/nemez/project_root/logs/`

## §4. Модель/артефакты

**CPU-модель YOLO:** `/home/nemez/DD5KA/models/cpu/best.pt`  
Каталоги создавать при отсутствии: `/home/nemez/DD5KA/models/cpu/`.

## §5. ENV-переменные

**Детектор (`dd5ka-detector.service`):**
- `DD5KA_BACKEND=cpu|stub` — выбор бэкенда
- `DETECTOR_MIN_CONF=float` — порог уверенности (деф. 0.55; на тестах 0.25–0.35)
- `DETECTOR_CLASS_ALLOW=csv` — разрешённые имена (lower), напр. `dron,drone,дрон,uav`
- `DETECTOR_CLASS_IDS=csv` — разрешённые ID классов (НАПР.: `0`), применяется ДО name-фильтра (AND-логика)
- `IMG_MAX_SIDE=int` — макс. сторона кадра для инференса (деф. 1280; на тестах 1600)
- `PANEL_BASE_URL=http://127.0.0.1:8098`, `DETECTOR_POLL_SEC=5`
- `LOG_DIR=/home/nemez/project_root/logs`

**Панель (`dd5ka-panel.service`):**
- `SNAPSHOT_MAX_SIDE=int` — деф. 960 (длинная сторона)
- `OVERLAY_MAX_SIDE=int` — деф. 640
- `OVERLAY_FPS=int` — деф. 4 (частота отдачи кадров)
- `OVERLAY_CAPTURE_FPS=int` — деф. 2 (частота реального захвата)
- `OVERLAY_DET_MAX_AGE_MS=int` — деф. 4000 (макс. «возраст» события для отрисовки)

## 4) Переменные окружения и конфиги

Файл с переменными: `configs/.env`  
Пример: `configs/.env.example`

Ключевые переменные:
- `DD5KA_BACKEND=cpu|hailo` — бэкенд инференса
- `DD5KA_SOURCE=mjpeg|snapshot` — источник кадров
- `YOLO_WEIGHTS=/home/nemez/yolov8n.pt` — путь к весам модели
- `PANEL_HOST=0.0.0.0` — хост Flask-панели
- `PANEL_PORT=8098` — порт Flask-панели
- `LOG_DIR=/home/nemez/project_root/logs` — директория логов
- `DETECTIONS_JSONL=/home/nemez/project_root/logs/detections.jsonl` — файл событий

Шаблон `.env.example`:
DD5KA_BACKEND=cpu
DD5KA_SOURCE=mjpeg
YOLO_WEIGHTS=/home/nemez/yolov8n.pt
PANEL_HOST=0.0.0.0
PANEL_PORT=8098
LOG_DIR=/home/nemez/project_root/logs
DETECTIONS_JSONL=/home/nemez/project_root/logs/detections.jsonl

## §6. Схема событий

**Схема события detection (detections.jsonl):**

`type: "detection"`, `backend: "cpu"`,  
`model: { path, framework: "ultralytics", version: "auto" }`  
`image: { width: <int>, height: <int> }` — размер кадра на инференсе  
`detections: [{ class_id, class_name, conf, bbox_xyxy }]` — `class_name` нормализуется к `"drone"` для синонимов  
`perf: { infer_ms: <int>, resized: [<w>, <h>] }`

## 5) Команды диагностики на Raspberry Pi

### Проверка камеры
rpicam-still -o /dev/null


### Статус панели (Flask)
systemctl --no-pager --full status dd5ka-panel.service || true
journalctl -xeu dd5ka-panel.service --no-pager -n 200 || true
tail -n 120 /home/nemez/project_root/logs/panel.log || true


### Статус детектора (YOLO)
systemctl --no-pager --full status dd5ka-detector.service || true
journalctl -xeu dd5ka-detector.service --no-pager -n 200 || true
tail -n 120 /home/nemez/project_root/logs/detector.log || true


### Проверка Hailo (если используется)
systemctl --no-pager --full status hailort.service || true


### Перезапуск сервисов
sudo systemctl restart dd5ka-panel.service dd5ka-detector.service

## §12. Диагностика

# detector
ssh nemez@<pi> 'systemctl --no-pager --full status dd5ka-detector.service'
ssh nemez@<pi> 'journalctl -xeu dd5ka-detector.service -n 120 --no-pager'
ssh nemez@<pi> 'tail -n 60 /home/nemez/DD5KA/logs/detections.jsonl'

# panel
ssh nemez@<pi> 'curl -sI http://127.0.0.1:8098/stream/overlay.mjpg | egrep -i "HTTP/|Content-Type"'
ssh nemez@<pi> 'curl -s --max-time 5 http://127.0.0.1:8098/stream/overlay.mjpg -o /dev/null -w "bytes=%{size_download}\n"'
ssh nemez@<pi> 'curl -s http://127.0.0.1:8098/api/last'

# yolo
ssh nemez@<pi> '/home/nemez/drone-env/bin/python -c "import ultralytics; print(ultralytics.__version__)"'
ssh nemez@<pi> "/home/nemez/drone-env/bin/python -c \"import sys,os; p=os.path.abspath('/home/nemez/DD5KA/src'); sys.path.insert(0,p); import detector.yolo_cpu as m; print('ok', hasattr(m,'YOLOCPUInference'))\""

## §13. Тюнинг/эксперименты

- `DETECTOR_MIN_CONF` 0.25–0.50 — подобрать под задачу/освещение.
- `IMG_MAX_SIDE` 960–1600 — больше размер = лучше мелкие цели, но медленнее.
- `DETECTOR_CLASS_IDS=0` — надёжная фильтрация по ID для класса DRON.
- Панель: `OVERLAY_MAX_SIDE=640..800`, `OVERLAY_CAPTURE_FPS=1..2` — снизить конкуренцию за камеру.

## §14. Известные грабли

- «Залипание» рамок: рисуем строго по **последнему** событию; пустые detections очищают экран; проверяем свежесть.
- ImportError относительных импортов под systemd: добавляйте `src` в `sys.path`, используйте абсолютные импорты.
- Поток есть, но bytes=0: генератор overlay обязан yield'ить кадры даже при сбое захвата (используем last_ok_frame/заглушку).
- /snapshot 500: это временные сбои `rpicam-still` — работает retry-логика, не спамить запросами.

## 6) Протокол работы ИИ-ассистента

1. Один ответ = **одна правка / одно действие / одна команда проверки**.
2. Любая правка оформляется как:
   - полный файл целиком, **или**
   - unified-diff с точным путём к файлу.
3. После правки ассистент обязан дать **одну** команду для проверки результата.
4. Никаких массовых изменений «всё сразу».  
   Итерации только пошаговые.
5. Любые изменения архитектуры (структуры каталогов, логики работы сервисов, модели ИИ, пайплайна обработки) — **только по RFC**, а не в обычном ответе.
6. Все изменения, которые касаются окружения или конфигурации, **обязательно** фиксируются в этом файле.
7. Если ассистент не понимает текущее состояние — он сначала выполняет диагностику (через команды из раздела 5), а уже потом предлагает правку.

**1.6 Атомарность действий**
Каждая выдача ассистента содержит одно логическое действие (один завершённый результат).
Примеры:
- «деплой systemd-сервиса» = одно действие, даже если внутри несколько команд,
- «pull+restart на RPi» = одно действие,
- «git add+commit+push (на Mac)» = одно действие,
- «smoke-test /health → /snapshot → /api/last» = одно действие.
Но: patch для Cursor → commit/push → pull/restart → проверка — это разные действия и идут отдельными шагами.

**1.7 Формат команд для RPi**
Все команды для RPi всегда выдаются в формате:
`ssh nemez@<RPi_IP> '<команда>'`
По умолчанию работа ведётся с Mac, локальный терминал — Mac.

**1.8 Краткое назначение перед командой**
Перед блоком команд — одна строка с назначением шага («Перезапускаем …», «Диагностика …», «Деплой …»).

**1.9 Объединение совместимых команд**
Если несколько команд относятся к одному логическому действию и не конфликтуют, их допустимо выдавать одним блоком (например: `systemctl daemon-reload && systemctl restart ...`). Но длинная цепочка разных действий (patch → commit → pull → restart → проверка) всегда делится на отдельные шаги.

## 7) Протокол входа нового ИИ-ассистента

Перед началом работы ассистент обязан:

1. Прочитать `PROMPT_MASTER.md` полностью.
2. Синхронизироваться с текущим кодом:
   - `src/panel/` — Flask-панель
   - `src/detector/` — YOLO-детектор
3. Если какие-либо файлы не были показаны в чате — запросить вывод (`cat`, `ls`, `diff`).
4. Сформировать краткий обзор: что уже реализовано и в каком состоянии.
5. Только после синхронизации с кодовой базой — предлагать правки.
6. Запрещено создавать новую реализацию “с нуля”, если код уже существует.
7. Все изменения — пошагово, по протоколу «один ответ = одна правка + одна команда проверки».
- Все команды, относящиеся к RPi, предоставляются в ssh-формате: `ssh nemez@<RPi_IP> '<команда>'` (рабочий терминал — Mac).
- Стандартный первичный smoke-test связи сервисов: `/api/health` → `/snapshot` → `/api/last` (в указанной последовательности).

**Чек-лист CH5:**
- Проверить симлинк `/home/nemez/project_root -> /home/nemez/DD5KA`
- Проверить модель: `/home/nemez/DD5KA/models/cpu/best.pt`
- Выставить `DD5KA_BACKEND=cpu` (drop-in), перезапустить детектор
- Убедиться: в `logs/detections.jsonl` идут `type:"detection"`
- Проверить панель: `/snapshot` (200 и >10KB), `/api/last`, `/stream/overlay.mjpg` (200 и поток)
- При ImportError относительных импортов под systemd — добавить `src` в `sys.path` и перейти на абсолютные импорты

## 8) Протокол передачи чата новому ассистенту

Когда создаётся новый чат или подключается новый агент, пользователь обязан:

1. Указать, что проект ведётся по `PROMPT_MASTER.md`.
2. Сообщить путь к этому файлу (локально или в репозитории).
3. Сформулировать задачу в стиле:  
   «Прочитай `PROMPT_MASTER.md`, синхронизируйся с текущим кодом и продолжи работу пошагово».

Ассистент обязан:

1. Сначала подтвердить чтение `PROMPT_MASTER.md`.
2. Затем запросить текущее состояние кода (если нужно — вывод `ls`/`cat` отдельных файлов).
3. Сделать краткий обзор реального состояния (что уже есть, что не реализовано).
4. Только после синхронизации предложить первую минимальную правку.
5. Соблюдать правило: **никакого переписывания с нуля** при существующем коде.


## 9) Break-glass / восстановление ассистента

Если ассистент начал:
- терять контекст,
- игнорировать протокол,
- предлагать переписывание с нуля,
- или выдавать нерелевантные ответы,

то пользователь выполняет «перезагрузку ассистента» без потери проекта:

**Процедура восстановления:**

1. Сообщить ассистенту фразу:  
   «Восстанови контекст: прочитай PROMPT_MASTER.md и выполни входной протокол».
2. Ассистент обязан перечитать документ и синхронизироваться с кодовой базой.
3. Ассистент обязан выдать краткий обзор текущего состояния (snapshot).
4. После этого работа продолжается с того же места.

**Важно:**  
Перезагрузка ассистента не даёт права переписывать проект или структуру заново.  
Если предлагаются масштабные изменения — только через RFC.

## 10) Версионирование и синхронизация документа

Этот файл является частью кода и версионируется через git.

Правила обновления:
1. Любое изменение конфигурации, структуры, сервисов, путей или переменных окружения обязано быть отражено здесь.
2. Перед коммитом проверяется, что PROMPT_MASTER.md актуализирован.
3. Каждое значимое изменение фиксируется отдельным коммитом с сообщением вида:
   `docs: update PROMPT_MASTER (<что изменено>)`.
4. Новый ассистент не считается «вошедшим в проект», пока не подтвердит чтение актуальной версии файла из репозитория.

## 11) Ссылка на репозиторий (единый источник правды)

Публичный URL (read-only для ассистентов):
https://github.com/nemezkarl-source/DD5KA

Правило: любой новый ассистент начинает работу с чтения `docs/PROMPT_MASTER.md` в этом репозитории, затем — синхронизация с текущим кодом по разделу «Протокол входа», и только после этого — минимальные правки по протоколу «один ответ = одна правка + одна команда проверки».

## 12) RAW-доступ к репозиторию (для ассистентов)

PROMPT_MASTER (RAW):
https://raw.githubusercontent.com/nemezkarl-source/DD5KA/main/docs/PROMPT_MASTER.md

Шаблон для любого файла:
https://raw.githubusercontent.com/nemezkarl-source/DD5KA/main/<путь/к/файлу>

Готовые ссылки на ключевые файлы проекта:

- Flask-панель (app.py)
  https://raw.githubusercontent.com/nemezkarl-source/DD5KA/main/src/panel/app.py

- systemd unit панели
  https://raw.githubusercontent.com/nemezkarl-source/DD5KA/main/configs/dd5ka-panel.service

Детектор:
- пока отсутствует в репозитории (разработка на RPi)
- ассистент НЕ должен генерировать новый код без синхронизации с фактической версией
- если потребуется, ассистент может запросить RAW-ссылку после того, как файл окажется в репозитории

Правила RAW-доступа:
1. Ассистент обязан читать файлы через RAW-ссылки, а не через HTML UI GitHub.
2. Пользователь не обязан присылать код вручную.
3. Запросы “пришли файл src/panel/app.py” считаются ошибочным входом.
4. Если файл отсутствует в репозитории — ассистент обязан запросить загрузку, а не пытаться писать с нуля.

## 13) Разделение ролей ассистентов (Cursor = исполнитель, чат = постановщик)

Архитектурное правило CH4:
Код генерирует и изменяет **Cursor в IDE**, чат-ассистент только **инициирует patch-задачи**.

Роли:

- Чат-ассистент (ChatGPT / LLM-чат):
  · Анализирует состояние проекта
  · Формирует задачу в виде промпта для Cursor
  · НЕ пишет код напрямую
  · Контролирует результат и проверку
  · При необходимости формирует уточняющий запрос к Cursor

- Cursor (IDE-агент):
  · Единственный исполнитель кода
  · Генерирует патчи (diff или полный файл)
  · Выдаёт одну команду проверки
  · Не решает ЧТО делать — только исполняет сформулированную задачу

Поведение по умолчанию:
1. Если чат по ошибке начинает писать код — он обязан **немедленно преобразовать попытку в корректный Cursor-промпт**.
2. Любая правка = patch в Cursor → затем применение на RPi → проверка.
3. Запрещено генерировать код в чате напрямую (минуя Cursor).
4. Пользователь не обязан вручную пересылать код — Cursor и RAW-доступ обеспечивают синхронизацию.

Цель:
Гарантировать воспроизводимость и отсутствие рассинхронизации между чатами, IDE и реальным кодом устройства.

## 14) Контур обновления кода (Mac → GitHub → RPi)

Проект DD-5KA использует строгий односторонний CI/CD-контур поставки кода:

1. Разработка выполняется **только на Mac**. Raspberry Pi не является средой разработки.
2. Код генерируется и правится **только через Cursor** (IDE), а не в чате.
3. После генерации патчей Cursor → изменения коммитятся и пушатся в GitHub.
4. GitHub является единым источником правды. Raspberry Pi = runtime-копия репозитория.
5. Обновление на устройстве выполняется ТОЛЬКО через:
git pull
systemctl restart <service>
6. Запрещено вносить правки вручную на RPi (nano, vim, python -c и т.п.).
7. Любой запрос «правь файл на Raspberry Pi» = нарушение протокола.

---

## §8. Команды развёртывания

**Mac → GitHub:**

git add <files> && git commit -m "<msg>" && git push

**RPi (pull):**

ssh nemez@<pi> "cd /home/nemez/DD5KA && git pull"

**RPi (env + restart, пример — детектор):**

ssh nemez@<pi> 'sudo tee /etc/systemd/system/dd5ka-detector.service.d/override.conf >/dev/null <<EOF
[Service]
Environment=DD5KA_BACKEND=cpu
Environment=DETECTOR_MIN_CONF=0.35
Environment=DETECTOR_CLASS_ALLOW=dron,drone
Environment=DETECTOR_CLASS_IDS=0
Environment=IMG_MAX_SIDE=1600
EOF
sudo systemctl daemon-reload && sudo systemctl restart dd5ka-detector.service'

**RPi (env + restart, пример — панель):**

ssh nemez@<pi> 'sudo tee /etc/systemd/system/dd5ka-panel.service.d/override.conf >/dev/null <<EOF
[Service]
Environment=SNAPSHOT_MAX_SIDE=960
Environment=OVERLAY_MAX_SIDE=640
Environment=OVERLAY_FPS=4
Environment=OVERLAY_CAPTURE_FPS=2
Environment=OVERLAY_DET_MAX_AGE_MS=4000
EOF
sudo systemctl daemon-reload && sudo systemctl restart dd5ka-panel.service'

## §9. Фикс импортов под systemd

**Фикс импортов при запуске через systemd:** в скриптах-энтрипоинтах добавлять `src` в `sys.path` и использовать абсолютные импорты.

Применено:
- `src/detector/daemon.py` → `sys.path` + `from detector.yolo_cpu import ...`
- `src/panel/app.py` → `sys.path` + `from panel.overlay import ...`
- Создавать `__init__.py` для пакетов при необходимости.

## §10. Захват кадра (панель)

Helper: `src/panel/camera.py`
- `capture_jpeg(max_side, timeout_ms, retries)` вызывает `rpicam-still`:
  `-n -o - -t <timeout_ms> --quality 70 --thumb none --width <w> --height <h>`
- Размеры считаются по аспекту сенсора 4056×3040; ретраи при сбоях.

## §11. Overlay-поток

`GET /stream/overlay.mjpg`:
- Кадр берётся напрямую через `panel.camera.capture_jpeg` (не через `/snapshot`).
- Рисуем рамки по **последнему** событию `detections.jsonl`; пустые события очищают рамки.
- Anti-stale: `event.ts` проверяется против `OVERLAY_DET_MAX_AGE_MS`.
- Масштабирование bbox: из `event.image(w,h)` в `frame(w,h)`.
- Разделены `OVERLAY_FPS` (отдача) и `OVERLAY_CAPTURE_FPS` (захват); используется `last_ok_frame`.
- При отсутствии кадра — чёрная заглушка «NO FRAME».
- Лог: `overlay frame: dets=<N>, age_ms=<int>, draw_ms=<int>, fresh=<True|False>`.

## 15) Интерфейс применения правок (обязательная последовательность)

Каждая правка кода завершается **выдачей полного набора команд для применения**:

1. Обновление репозитория:
git add ...
git commit ...
git push

2. Применение на Raspberry Pi (ассистент обязан выдать эти команды полностью):
ssh nemez@pi-drone.local
cd /home/nemez/project_root
git pull
sudo systemctl restart <service>

3. Проверка результата (пример):
curl http://pi-drone.local:8098/healthz
или `tail -n 50 logs/...`

Правка считается завершённой **ТОЛЬКО** после:
- push в GitHub
- pull на RPi
- restart службы
- подтверждённой проверки.

Чат-ассистент обязан всегда выдавать эти команды автоматически.

- Панель (`dd5ka-panel.service`) и детектор (`dd5ka-detector.service`) независимы. Перезапуски выполняются адресно: при изменениях панели — только панель; при изменениях детектора — только детектор (если не оговорено иначе).
- При отладке панели допускается временная остановка детектора для исключения конкуренции за камеру.
- Политика «hard»: при отладке стабильности `/snapshot` детектор должен быть остановлен до восстановления стабильной 200-ответности панели.

Семантика `camera` в `/api/health`:
- `ok` — камера доступна (нет блокирующих rpicam-процессов, media-устройства читаются),
- `busy` — обнаружен висящий или конкурирующий rpicam-процесс (камера занята, требуется очистка процессов/пайплайна),
- `error` — медиа-устройства недоступны или иная системная ошибка.

## CHANGELOG

- `docs: PROMPT_MASTER — уточнены атомарность действий, ssh-формат для RPi, smoke-test, политика мульти-сервисной отладки (hard)`
