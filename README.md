# TGMaster

Telegram Account Manager на Python (Tkinter + Telethon).

## Возможности

- Загрузка session/json/zip (tdata) файлов через диалог или drag-and-drop.
- Автоматическая фоновая проверка аккаунтов через Telethon.
- Карточки аккаунтов со статусами и быстрыми действиями.
- Конвертер форматов (демо-интерфейс).
- Запуск Telegram Desktop с выбранной сессией (tdata копируется автоматически).

## Запуск

```bash
python tgmaster.py
```

## Зависимости

```bash
pip install -r requirements.txt
```

Опционально:
- `ttkbootstrap` для современного интерфейса.
- `tkinterdnd2` для drag-and-drop.

## Настройки

В интерфейсе укажите `API_ID` и `API_HASH` для Telethon и путь к Telegram Desktop.

> Примечание: Конвертер форматов и запуск сессий реализованы как демо-шаблон. Для
> полноценной конвертации используйте Telethon и доп. логику сохранения tdata.
