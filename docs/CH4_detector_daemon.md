# DD-5KA · CH4 Detector Daemon

## Описание

Демон детектора выполняет heartbeat-опрос панели через HTTP GET `/snapshot` для проверки доступности камеры и системы.

## Логи

- `logs/detector.log` — основной лог демона с heartbeat-событиями
- `logs/detections.jsonl` — JSON-события детекции (heartbeat, ошибки)

## Переменные окружения

- `PANEL_BASE_URL` (default: `http://127.0.0.1:8098`) — URL панели
- `DETECTOR_POLL_SEC` (default: `5`, range: 1-60) — интервал опроса в секундах
- `LOG_DIR` (default: `logs`) — директория логов

## Локальный запуск

```bash
python src/detector/daemon.py
```

С переменными окружения:
```bash
PANEL_BASE_URL=http://127.0.0.1:8098 DETECTOR_POLL_SEC=2 LOG_DIR=logs python src/detector/daemon.py
```

## Systemd

Сервис: `dd5ka-detector.service`
