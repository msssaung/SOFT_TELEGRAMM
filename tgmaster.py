"""TGMaster - Telegram Account Manager (Tkinter + Telethon).

This app provides a desktop UI for loading Telegram session files,
checking their status asynchronously, converting session formats,
and launching Telegram Desktop with a selected session.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox


def ensure_package(package_name: str, import_name: Optional[str] = None) -> bool:
    module_name = import_name or package_name
    try:
        __import__(module_name)
        return True
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", package_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            __import__(module_name)
            return True
        except Exception:
            return False


has_ttkbootstrap = ensure_package("ttkbootstrap")
if has_ttkbootstrap:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import BOTH, LEFT, RIGHT, TOP, X, Y
else:  # fall back to standard ttk
    from tkinter import ttk

    BOTH = tk.BOTH
    LEFT = tk.LEFT
    RIGHT = tk.RIGHT
    TOP = tk.TOP
    X = tk.X
    Y = tk.Y

if ensure_package("tkinterdnd2"):
    from tkinterdnd2 import DND_FILES, TkinterDnD
else:  # optional drag & drop dependency
    DND_FILES = None
    TkinterDnD = None

APP_NAME = "TGMaster"
CONFIG_PATH = Path.home() / ".tgmaster.json"
DATA_DIR = Path.home() / ".tgmaster"
TDATA_STORE_DIR = DATA_DIR / "tdata_store"
TDATA_REGISTRY_PATH = DATA_DIR / "tdata_registry.json"
SUPPORTED_EXTENSIONS = {".session", ".json", ".zip"}

STATUS_LABELS = {
    "live": "✅ Живой",
    "dead": "❌ Неавторизован",
    "frozen": "⏸️ Заморожен",
    "flood": "⚠️ Ограничен",
    "error": "❓ Ошибка",
    "pending": "⏳ Проверка",
    "unknown": "❔ Неизвестно",
}


def open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        messagebox.showerror("Открытие папки", f"Не удалось открыть {path}")


@dataclass
class AccountRecord:
    path: Path
    session_type: str
    display_name: str
    status: str = "pending"
    detail: str = ""
    added_at: float = field(default_factory=time.time)
    stored_path: Optional[Path] = None


class Config:
    def __init__(self) -> None:
        self.api_id: str = ""
        self.api_hash: str = ""
        self.telegram_path: str = ""

    def load(self) -> None:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.api_id = data.get("api_id", "")
            self.api_hash = data.get("api_hash", "")
            self.telegram_path = data.get("telegram_path", "")

    def load_from_json(self, path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        api_id = data.get("api_id") or data.get("API_ID")
        api_hash = data.get("api_hash") or data.get("API_HASH")
        if api_id and api_hash:
            self.api_id = str(api_id)
            self.api_hash = str(api_hash)
            return True
        return False

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    "api_id": self.api_id,
                    "api_hash": self.api_hash,
                    "telegram_path": self.telegram_path,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class SessionChecker(threading.Thread):
    def __init__(
        self,
        task_queue: "queue.Queue[AccountRecord]",
        result_queue: "queue.Queue[Tuple[AccountRecord, str, str]]",
        config: Config,
    ) -> None:
        super().__init__(daemon=True)
        self.task_queue = task_queue
        self.result_queue = result_queue
        self.config = config

    def run(self) -> None:
        while True:
            record = self.task_queue.get()
            if record is None:
                break
            status, detail = self.check_session(record)
            self.result_queue.put((record, status, detail))
            self.task_queue.task_done()

    def check_session(self, record: AccountRecord) -> Tuple[str, str]:
        if not self.config.api_id or not self.config.api_hash:
            return "unknown", "Не задан API_ID/API_HASH. Проверка пропущена."

        if not ensure_package("telethon"):
            return "error", "Telethon недоступен и не удалось установить."

        try:
            from telethon import TelegramClient
            from telethon.errors import FloodWaitError
        except Exception as exc:  # Telethon may be missing
            return "error", f"Telethon недоступен: {exc}"

        session_path = str(record.path)
        try:
            api_id = int(self.config.api_id)
        except ValueError:
            return "error", "API_ID должен быть числом"
        api_hash = self.config.api_hash

        async def _run_check() -> Tuple[str, str]:
            try:
                async with TelegramClient(session_path, api_id, api_hash) as client:
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        identifier = me.phone or str(me.id)
                        record.display_name = identifier
                        return "live", "Сессия активна"
                    return "dead", "Сессия не авторизована"
            except FloodWaitError as exc:
                return "flood", f"FloodWait: {exc.seconds} сек."
            except Exception as exc:
                return "error", f"Ошибка подключения: {exc}"

        try:
            return asyncio.run(_run_check())
        except Exception as exc:
            return "error", str(exc)


class TGMasterApp:
    def __init__(self) -> None:
        self.config = Config()
        self.config.load()
        self._ensure_data_dirs()
        self.tdata_registry = self._load_tdata_registry()

        if TkinterDnD:
            self.root = TkinterDnD.Tk()
        else:
            try:
                self.root = ttk.Window(themename="flatly")
            except Exception:
                self.root = ttk.Tk()

        self.root.title(APP_NAME)
        self.root.geometry("1200x720")
        self.root.minsize(1000, 600)

        self.accounts: List[AccountRecord] = []
        self.account_widgets: Dict[AccountRecord, ttk.Frame] = {}
        self.task_queue: "queue.Queue[AccountRecord]" = queue.Queue()
        self.result_queue: "queue.Queue[Tuple[AccountRecord, str, str]]" = queue.Queue()
        self.checker = SessionChecker(self.task_queue, self.result_queue, self.config)
        self.checker.start()

        self._build_ui()
        self._poll_results()

    def _ensure_data_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TDATA_STORE_DIR.mkdir(parents=True, exist_ok=True)

    def _load_tdata_registry(self) -> Dict[str, Dict[str, str]]:
        if not TDATA_REGISTRY_PATH.exists():
            return {}
        try:
            return json.loads(TDATA_REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_tdata_registry(self) -> None:
        TDATA_REGISTRY_PATH.write_text(
            json.dumps(self.tdata_registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root)
        header.pack(side=TOP, fill=X)

        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 18, "bold")).pack(
            side=LEFT, padx=16, pady=12
        )
        ttk.Button(header, text="Настройки", command=self._open_settings).pack(
            side=RIGHT, padx=12
        )
        ttk.Button(header, text="Выход", command=self.root.destroy).pack(
            side=RIGHT
        )

        main = ttk.Frame(self.root)
        main.pack(fill=BOTH, expand=True)

        left = ttk.Frame(main)
        left.pack(side=LEFT, fill=BOTH, expand=True, padx=(16, 8), pady=12)

        right = ttk.Frame(main, width=320)
        right.pack(side=RIGHT, fill=Y, padx=(8, 16), pady=12)

        # Left panel: account list
        self._build_account_list(left)

        # Right panel: tools
        self._build_side_panel(right)

    def _build_account_list(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=X, pady=(0, 8))
        ttk.Button(toolbar, text="Добавить аккаунты", command=self._add_accounts).pack(
            side=LEFT
        )

        drop_hint = "Перетащите session/json/zip в эту область"
        if not TkinterDnD:
            drop_hint = "Drag-and-drop недоступен (установите tkinterdnd2)"

        ttk.Label(toolbar, text=drop_hint).pack(side=LEFT, padx=12)

        self.canvas = tk.Canvas(parent, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side=RIGHT, fill=Y)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)

        self.list_container = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.list_container, anchor="nw")

        self.list_container.bind(
            "<Configure>",
            lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

        if TkinterDnD:
            self.canvas.drop_target_register(DND_FILES)
            self.canvas.dnd_bind("<<Drop>>", self._handle_drop)

    def _build_side_panel(self, parent: ttk.Frame) -> None:
        stats_frame = ttk.LabelFrame(parent, text="Статистика")
        stats_frame.pack(fill=X, pady=(0, 12))

        self.stats_label = ttk.Label(stats_frame, text="Всего: 0 | Живые: 0 | Мертвые: 0")
        self.stats_label.pack(padx=12, pady=8)

        notebook = ttk.Notebook(parent)
        notebook.pack(fill=BOTH, expand=True)

        tools_tab = ttk.Frame(notebook)
        converter_tab = ttk.Frame(notebook)
        logs_tab = ttk.Frame(notebook)

        notebook.add(tools_tab, text="Инструменты")
        notebook.add(converter_tab, text="Конвертер")
        notebook.add(logs_tab, text="Логи")

        ttk.Button(tools_tab, text="Проверить все", command=self._check_all).pack(
            fill=X, padx=12, pady=8
        )
        ttk.Button(tools_tab, text="Экспорт живых", command=self._export_live).pack(
            fill=X, padx=12, pady=8
        )

        self._build_converter(converter_tab)
        self._build_logs(logs_tab)

    def _build_converter(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Исходный файл/папка:").pack(anchor="w", padx=12, pady=(12, 4))
        row = ttk.Frame(parent)
        row.pack(fill=X, padx=12)
        self.converter_source = ttk.Entry(row)
        self.converter_source.pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="Выбрать", command=self._select_converter_source).pack(
            side=LEFT, padx=6
        )

        ttk.Label(parent, text="Целевой формат:").pack(anchor="w", padx=12, pady=(12, 4))
        self.converter_target = ttk.Combobox(
            parent,
            values=[
                "Session -> TData",
                "TData -> Session",
                "Session -> JSON",
                "JSON -> Session",
            ],
            state="readonly",
        )
        self.converter_target.current(0)
        self.converter_target.pack(fill=X, padx=12)

        ttk.Label(parent, text="Папка назначения:").pack(anchor="w", padx=12, pady=(12, 4))
        dest_row = ttk.Frame(parent)
        dest_row.pack(fill=X, padx=12)
        self.converter_dest = ttk.Entry(dest_row)
        self.converter_dest.pack(side=LEFT, fill=X, expand=True)
        ttk.Button(dest_row, text="Выбрать", command=self._select_converter_dest).pack(
            side=LEFT, padx=6
        )

        ttk.Button(parent, text="Конвертировать", command=self._convert_session).pack(
            fill=X, padx=12, pady=16
        )

        if TkinterDnD:
            parent.drop_target_register(DND_FILES)
            parent.dnd_bind("<<Drop>>", self._handle_converter_drop)
        else:
            ttk.Label(
                parent,
                text="Drag-and-drop для конвертера доступен с tkinterdnd2",
                foreground="#888",
            ).pack(padx=12, pady=6)

    def _build_logs(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(parent, height=12, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True, padx=8, pady=8)

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройки")
        dialog.geometry("420x280")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="API_ID:").pack(anchor="w", padx=12, pady=(12, 4))
        api_id_entry = ttk.Entry(dialog)
        api_id_entry.insert(0, self.config.api_id)
        api_id_entry.pack(fill=X, padx=12)

        ttk.Label(dialog, text="API_HASH:").pack(anchor="w", padx=12, pady=(12, 4))
        api_hash_entry = ttk.Entry(dialog)
        api_hash_entry.insert(0, self.config.api_hash)
        api_hash_entry.pack(fill=X, padx=12)

        ttk.Label(dialog, text="Путь к Telegram Desktop:").pack(anchor="w", padx=12, pady=(12, 4))
        tg_row = ttk.Frame(dialog)
        tg_row.pack(fill=X, padx=12)
        tg_entry = ttk.Entry(tg_row)
        tg_entry.insert(0, self.config.telegram_path)
        tg_entry.pack(side=LEFT, fill=X, expand=True)
        ttk.Button(
            tg_row,
            text="Выбрать",
            command=lambda: self._select_telegram_path(tg_entry),
        ).pack(side=LEFT, padx=6)

        def save_settings() -> None:
            self.config.api_id = api_id_entry.get().strip()
            self.config.api_hash = api_hash_entry.get().strip()
            self.config.telegram_path = tg_entry.get().strip()
            self.config.save()
            self._log("Настройки сохранены")
            dialog.destroy()

        ttk.Button(dialog, text="Сохранить", command=save_settings).pack(
            pady=16
        )

    def _select_telegram_path(self, entry: ttk.Entry) -> None:
        path = filedialog.askopenfilename(title="Выберите Telegram Desktop")
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _add_accounts(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Выберите файлы сессий",
            filetypes=[
                ("Telegram sessions", "*.session *.json *.zip"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        self._handle_new_paths([Path(p) for p in paths])

    def _handle_drop(self, event) -> None:
        if not event.data:
            return
        paths = self._parse_dnd_paths(event.data)
        self._handle_new_paths(paths)

    def _handle_converter_drop(self, event) -> None:
        if not event.data:
            return
        paths = self._parse_dnd_paths(event.data)
        if paths:
            self.converter_source.delete(0, "end")
            self.converter_source.insert(0, str(paths[0]))

    def _parse_dnd_paths(self, data: str) -> List[Path]:
        raw = data.strip().split()
        cleaned = [Path(path.strip("{}")) for path in raw]
        return cleaned

    def _handle_new_paths(self, paths: Iterable[Path]) -> None:
        for path in paths:
            if path.is_dir():
                self._handle_session_path(path)
            elif path.suffix.lower() in SUPPORTED_EXTENSIONS:
                self._handle_session_path(path)
            else:
                self._log(f"Файл {path} пропущен")

    def _handle_session_path(self, path: Path) -> None:
        if path.suffix.lower() == ".zip":
            extracted_dir = self._extract_zip(path)
            if extracted_dir:
                for child in extracted_dir.rglob("*"):
                    if child.suffix.lower() in {".session", ".json"}:
                        self._register_account(child)
            return

        session_type = self._detect_session_type(path)
        if session_type == "json" and self.config.load_from_json(path):
            self._log("API_ID/API_HASH загружены из JSON")
        record = self._register_account(path, session_type)
        if session_type == "tdata":
            stored_path = self._store_tdata(record.path)
            if stored_path:
                record.stored_path = stored_path
                self._log(f"tdata сохранена в хранилище: {stored_path}")

    def _extract_zip(self, path: Path) -> Optional[Path]:
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="tgmaster_"))
            with zipfile.ZipFile(path) as archive:
                archive.extractall(temp_dir)
            self._log(f"ZIP распакован: {path}")
            return temp_dir
        except Exception as exc:
            self._log(f"Ошибка распаковки {path}: {exc}")
            return None

    def _detect_session_type(self, path: Path) -> str:
        if path.is_dir():
            if (path / "tdata").exists() or path.name.lower() == "tdata":
                return "tdata"
            return "folder"
        if path.suffix.lower() == ".session":
            return "session"
        if path.suffix.lower() == ".json":
            return "json"
        return "unknown"

    def _register_account(self, path: Path, session_type: Optional[str] = None) -> AccountRecord:
        session_type = session_type or self._detect_session_type(path)
        record = AccountRecord(
            path=path,
            session_type=session_type,
            display_name=path.stem,
        )
        self.accounts.append(record)
        widget = self._create_account_card(record)
        self.account_widgets[record] = widget
        self.task_queue.put(record)
        self._update_stats()
        return record

    def _create_account_card(self, record: AccountRecord) -> ttk.Frame:
        frame = ttk.Frame(self.list_container, padding=12, relief="ridge")
        frame.pack(fill=X, pady=6, padx=6)

        icon = ttk.Label(frame, text=self._session_icon(record.session_type), width=3)
        icon.pack(side=LEFT)

        info = ttk.Frame(frame)
        info.pack(side=LEFT, fill=X, expand=True)
        name_label = ttk.Label(info, text=record.display_name, font=("Segoe UI", 11, "bold"))
        name_label.pack(anchor="w")
        type_label = ttk.Label(info, text=f"Тип: {record.session_type}", foreground="#777")
        type_label.pack(anchor="w")

        status_label = ttk.Label(frame, text=STATUS_LABELS[record.status])
        status_label.pack(side=LEFT, padx=8)

        action_frame = ttk.Frame(frame)
        action_frame.pack(side=RIGHT)

        ttk.Button(action_frame, text="Запуск", command=lambda: self._launch_account(record)).pack(
            side=LEFT, padx=4
        )
        ttk.Button(
            action_frame,
            text="Конвертировать",
            command=lambda: self._prefill_converter(record),
        ).pack(side=LEFT)
        ttk.Button(
            action_frame,
            text="Папка",
            command=lambda: self._open_account_folder(record),
        ).pack(side=LEFT, padx=4)

        frame.status_label = status_label  # type: ignore[attr-defined]
        frame.name_label = name_label  # type: ignore[attr-defined]
        frame.type_label = type_label  # type: ignore[attr-defined]
        frame.detail = record.detail  # type: ignore[attr-defined]

        frame.bind("<Enter>", lambda event, r=record: self._show_tooltip(event, r))
        frame.bind("<Leave>", lambda event: self._hide_tooltip())
        return frame

    def _session_icon(self, session_type: str) -> str:
        return {
            "tdata": "🗂️",
            "session": "📄",
            "json": "🧾",
        }.get(session_type, "📁")

    def _prefill_converter(self, record: AccountRecord) -> None:
        self.converter_source.delete(0, "end")
        self.converter_source.insert(0, str(record.path))

    def _open_account_folder(self, record: AccountRecord) -> None:
        target = record.path if record.path.is_dir() else record.path.parent
        if not target.exists():
            messagebox.showerror("Открытие папки", "Папка не найдена")
            return
        open_in_file_manager(target)

    def _store_tdata(self, source: Path) -> Optional[Path]:
        if not source.exists():
            return None
        tdata_source = source
        if source.name.lower() != "tdata":
            candidate = source / "tdata"
            if candidate.exists():
                tdata_source = candidate
        if not tdata_source.exists():
            self._log("tdata не найдена для сохранения")
            return None

        identifier = self._get_telethon_identity_from_tdata(tdata_source) or tdata_source.parent.name
        safe_name = "".join(c for c in identifier if c.isalnum() or c in {"_", "-"})
        target = TDATA_STORE_DIR / safe_name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(tdata_source, target)
        self.tdata_registry[safe_name] = {
            "source": str(source),
            "stored": str(target),
            "timestamp": str(int(time.time())),
        }
        self._save_tdata_registry()
        return target

    def _get_telethon_identity_from_tdata(self, tdata_path: Path) -> Optional[str]:
        if not ensure_package("opentele"):
            return None
        if not self._require_api():
            return None

        try:
            from opentele.td import TDesktop
            from opentele.api import API
        except Exception:
            return None

        api = API.TelegramDesktop(self.config.api_id, self.config.api_hash)
        tdesktop = TDesktop(str(tdata_path))
        client = tdesktop.ToTelethon(session=":memory:", api=api)

        async def _run() -> Optional[str]:
            await client.connect()
            try:
                me = await client.get_me()
                return me.phone or str(me.id)
            finally:
                await client.disconnect()

        try:
            return asyncio.run(_run())
        except Exception:
            return None

    def _check_all(self) -> None:
        for record in self.accounts:
            record.status = "pending"
            widget = self.account_widgets.get(record)
            if widget:
                widget.status_label.config(text=STATUS_LABELS[record.status])  # type: ignore[attr-defined]
            self.task_queue.put(record)
        self._update_stats()

    def _convert_session(self) -> None:
        source = self.converter_source.get().strip()
        target = self.converter_target.get()
        dest = self.converter_dest.get().strip()

        if not source or not dest:
            messagebox.showwarning("Конвертер", "Выберите источник и папку назначения")
            return

        source_path = Path(source)
        dest_path = Path(dest)
        if not source_path.exists():
            messagebox.showwarning("Конвертер", "Источник не найден")
            return
        if not dest_path.exists():
            messagebox.showwarning("Конвертер", "Папка назначения не найдена")
            return
        if source_path.suffix.lower() == ".json" and self.config.load_from_json(source_path):
            self._log("API_ID/API_HASH загружены из JSON для конвертации")

        threading.Thread(
            target=self._run_conversion,
            args=(source_path, target, dest_path),
            daemon=True,
        ).start()

    def _run_conversion(self, source: Path, target: str, dest: Path) -> None:
        self._log_threadsafe(f"Запуск конвертации: {source} -> {target}")
        try:
            if target == "Session -> JSON":
                self._convert_session_to_json(source, dest)
            elif target == "JSON -> Session":
                self._convert_json_to_session(source, dest)
            elif target == "TData -> Session":
                self._convert_tdata_to_session(source, dest)
            elif target == "Session -> TData":
                self._convert_session_to_tdata(source, dest)
            else:
                self._log_threadsafe("Неизвестный формат конвертации")
                return
            self._log_threadsafe("Конвертация завершена")
        except Exception as exc:
            self._log_threadsafe(f"Ошибка конвертации: {exc}")
            self.root.after(
                0, lambda: messagebox.showerror("Конвертер", f"Ошибка: {exc}")
            )

    def _convert_session_to_json(self, source: Path, dest: Path) -> None:
        if not self._require_api():
            return
        if not ensure_package("telethon"):
            self._log_threadsafe("Telethon недоступен для конвертации.")
            return

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        output_path = dest / f"{source.stem}.json"
        try:
            api_id = int(self.config.api_id)
        except ValueError as exc:
            raise ValueError("API_ID должен быть числом") from exc
        api_hash = self.config.api_hash

        async def _run() -> None:
            async with TelegramClient(str(source), api_id, api_hash) as client:
                session_string = StringSession.save(client.session)
                me = await client.get_me()
                payload = {
                    "type": "telethon_string",
                    "session_string": session_string,
                    "user_id": getattr(me, "id", None),
                    "phone": getattr(me, "phone", None),
                }
                output_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        asyncio.run(_run())
        self._log_threadsafe(f"JSON сохранен: {output_path}")

    def _convert_json_to_session(self, source: Path, dest: Path) -> None:
        if not self._require_api():
            return
        if not ensure_package("telethon"):
            self._log_threadsafe("Telethon недоступен для конвертации.")
            return

        from telethon import TelegramClient
        from telethon.sessions import StringSession, SQLiteSession

        data = json.loads(source.read_text(encoding="utf-8"))
        session_string = data.get("session_string")
        if not session_string:
            raise ValueError("В JSON нет session_string")

        output_path = dest / f"{source.stem}.session"
        try:
            api_id = int(self.config.api_id)
        except ValueError as exc:
            raise ValueError("API_ID должен быть числом") from exc
        api_hash = self.config.api_hash

        async def _run() -> None:
            string_session = StringSession(session_string)
            async with TelegramClient(string_session, api_id, api_hash) as client:
                sqlite_session = SQLiteSession(str(output_path.with_suffix("")))
                sqlite_session.set_dc(
                    client.session.dc_id,
                    client.session.server_address,
                    client.session.port,
                )
                sqlite_session.auth_key = client.session.auth_key
                sqlite_session.save()

        asyncio.run(_run())
        self._log_threadsafe(f"Session сохранена: {output_path}")

    def _convert_tdata_to_session(self, source: Path, dest: Path) -> None:
        if not self._require_api():
            return
        if not ensure_package("opentele"):
            self._log_threadsafe("opentele недоступен для конвертации tdata.")
            return

        from opentele.td import TDesktop
        from opentele.api import API

        api = API.TelegramDesktop(self.config.api_id, self.config.api_hash)
        tdata_path = source
        if source.name.lower() != "tdata":
            tdata_path = source / "tdata"

        if not tdata_path.exists():
            raise FileNotFoundError("tdata не найдена в источнике")

        tdesktop = TDesktop(str(tdata_path))
        output_path = dest / f"{tdata_path.parent.name}.session"
        client = tdesktop.ToTelethon(session=str(output_path), api=api)
        asyncio.run(client.connect())
        asyncio.run(client.disconnect())
        self._log_threadsafe(f"Session сохранена: {output_path}")

    def _convert_session_to_tdata(self, source: Path, dest: Path) -> None:
        if not self._require_api():
            return
        if not ensure_package("opentele"):
            self._log_threadsafe("opentele недоступен для конвертации tdata.")
            return

        from opentele.td import TDesktop
        from opentele.api import API

        api = API.TelegramDesktop(self.config.api_id, self.config.api_hash)
        output_dir = dest / f"{source.stem}_tdata"
        output_dir.mkdir(parents=True, exist_ok=True)

        tdesktop = TDesktop(str(output_dir))
        client = tdesktop.ToTelethon(session=str(source), api=api)
        asyncio.run(client.connect())
        asyncio.run(client.disconnect())
        tdesktop.SaveTData()
        stored = self._store_tdata(output_dir)
        stored_path = stored if stored else output_dir
        self._log_threadsafe(f"tdata сохранена: {stored_path}")

    def _select_converter_source(self) -> None:
        path = filedialog.askopenfilename(
            title="Исходный файл",
            filetypes=[("Sessions", "*.session *.json *.zip"), ("All files", "*.*")],
        )
        if path:
            self.converter_source.delete(0, "end")
            self.converter_source.insert(0, path)

    def _select_converter_dest(self) -> None:
        path = filedialog.askdirectory(title="Папка назначения")
        if path:
            self.converter_dest.delete(0, "end")
            self.converter_dest.insert(0, path)

    def _export_live(self) -> None:
        if not self.accounts:
            return
        path = filedialog.asksaveasfilename(
            title="Экспорт живых аккаунтов",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("CSV", "*.csv")],
        )
        if not path:
            return

        live_accounts = [acc for acc in self.accounts if acc.status == "live"]
        content = "\n".join(str(acc.path) for acc in live_accounts)
        Path(path).write_text(content, encoding="utf-8")
        self._log(f"Экспортировано {len(live_accounts)} аккаунтов")

    def _launch_account(self, record: AccountRecord) -> None:
        if not self.config.telegram_path:
            messagebox.showwarning("Запуск", "Укажите путь к Telegram Desktop в настройках")
            return

        try:
            if record.session_type == "tdata":
                source = record.stored_path or record.path
                target = Path(self.config.telegram_path).parent / "tdata"
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
                self._log(f"tdata скопирован в {target}")
                self._launch_telegram_with_cleanup(target)
                return

            self._log("Для session используйте запуск клиента вручную (демо)")
            os.startfile(self.config.telegram_path)  # type: ignore[attr-defined]
        except Exception as exc:
            self._log(f"Ошибка запуска Telegram: {exc}")
            messagebox.showerror("Запуск", f"Не удалось запустить Telegram: {exc}")

    def _launch_telegram_with_cleanup(self, tdata_path: Path) -> None:
        if not ensure_package("psutil"):
            self._log("psutil недоступен для отслеживания процесса Telegram")
            os.startfile(self.config.telegram_path)  # type: ignore[attr-defined]
            return

        import psutil

        process = subprocess.Popen([self.config.telegram_path])

        def _wait_and_cleanup() -> None:
            try:
                ps_process = psutil.Process(process.pid)
                ps_process.wait()
            except Exception:
                process.wait()
            if tdata_path.exists():
                shutil.rmtree(tdata_path, ignore_errors=True)
                self._log_threadsafe("tdata удалена после закрытия Telegram")

        threading.Thread(target=_wait_and_cleanup, daemon=True).start()

    def _show_tooltip(self, event, record: AccountRecord) -> None:
        if not record.detail:
            return
        self.tooltip = tk.Toplevel(self.root)
        self.tooltip.overrideredirect(True)
        self.tooltip.geometry(f"300x60+{event.x_root + 10}+{event.y_root + 10}")
        label = ttk.Label(self.tooltip, text=record.detail, background="#333", foreground="#fff")
        label.pack(fill=BOTH, expand=True)

    def _hide_tooltip(self) -> None:
        if hasattr(self, "tooltip"):
            self.tooltip.destroy()
            del self.tooltip

    def _poll_results(self) -> None:
        while not self.result_queue.empty():
            record, status, detail = self.result_queue.get()
            record.status = status
            record.detail = detail
            widget = self.account_widgets.get(record)
            if widget:
                widget.status_label.config(text=STATUS_LABELS.get(status, status))  # type: ignore[attr-defined]
                widget.name_label.config(text=record.display_name)  # type: ignore[attr-defined]
            self._update_stats()
            self._log(f"{record.display_name}: {STATUS_LABELS.get(status, status)}")

        self.root.after(400, self._poll_results)

    def _update_stats(self) -> None:
        total = len(self.accounts)
        live = sum(1 for acc in self.accounts if acc.status == "live")
        dead = sum(1 for acc in self.accounts if acc.status == "dead")
        self.stats_label.config(text=f"Всего: {total} | Живые: {live} | Мертвые: {dead}")

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")

    def _log_threadsafe(self, message: str) -> None:
        self.root.after(0, lambda: self._log(message))

    def _require_api(self) -> bool:
        if not self.config.api_id or not self.config.api_hash:
            self._log_threadsafe("Не задан API_ID/API_HASH для конвертации.")
            self.root.after(
                0,
                lambda: messagebox.showwarning(
                    "Конвертер", "Укажите API_ID/API_HASH в настройках"
                ),
            )
            return False
        return True

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    TGMasterApp().run()
