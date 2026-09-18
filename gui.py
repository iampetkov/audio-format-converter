"""Tkinter GUI for running the batch audio converter."""

from __future__ import annotations

import contextlib
import queue
import re
import sys
import threading
from pathlib import Path
from tkinter import END, DISABLED, NORMAL
from tkinter import Tk, filedialog, messagebox, scrolledtext, ttk, StringVar

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_SUPPORTED = True
except Exception:
    DND_FILES = ""
    TkinterDnD = None
    _DND_SUPPORTED = False

import convert


def _normalize_path(text: str) -> str:
    value = text.strip().replace("\\ ", " ")

    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1].strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].strip()
    if len(value) >= 2 and value[0] == "{" and value[-1] == "}":
        value = value[1:-1].strip()

    return value


class QueueTextWriter:
    def __init__(self, output_queue: queue.Queue[tuple[str, str]]) -> None:
        self.output_queue = output_queue
        self._buffer = ""

    def write(self, data: str) -> int:
        if not data:
            return 0

        self._buffer += data
        while "\n" in self._buffer:
            line, _, remaining = self._buffer.partition("\n")
            self._buffer = remaining
            self.output_queue.put(("line", f"{line}\n"))
        return len(data)

    def flush(self) -> None:
        if self._buffer:
            self.output_queue.put(("line", self._buffer))
            self._buffer = ""


class ConverterGUI:
    def __init__(self) -> None:
        self.dnd_enabled = _DND_SUPPORTED
        self.window = self._create_window()
        self.window.title("Audio File Converter")
        self.window.geometry("800x550")

        self.output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.process_running = False

        self._build_ui()
        self._poll_queue()
        self._maybe_auto_start()

    def _create_window(self):
        if not self.dnd_enabled:
            return Tk()

        try:
            return TkinterDnD.Tk()  # type: ignore[union-attr]
        except Exception:
            self.dnd_enabled = False
            return Tk()

    def _build_ui(self) -> None:
        self.window.columnconfigure(0, weight=1)

        ttk.Label(self.window, text="WAV Folder").pack(anchor="w", padx=12, pady=(12, 4))

        folder_row = ttk.Frame(self.window)
        folder_row.pack(fill="x", padx=12)

        self.folder_path = StringVar()
        self.path_entry = ttk.Entry(folder_row, textvariable=self.folder_path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        if self.dnd_enabled:
            self._register_drop_target(self.path_entry)

        ttk.Button(folder_row, text="Browse", command=self._browse_folder).pack(side="right", padx=(8, 0))

        if self.dnd_enabled:
            hint = "Tip: You can also drag a WAV folder onto this window"
        else:
            hint = (
                "Drag-and-drop onto the window is unavailable in this build. "
                "You can still browse, paste a path, or drag a folder onto the app icon."
            )
        ttk.Label(self.window, text=hint).pack(anchor="w", padx=12, pady=(4, 0))

        self.start_button = ttk.Button(self.window, text="Start Conversion", command=self._start_conversion)
        self.start_button.pack(pady=(12, 8))

        ttk.Label(self.window, text="Progress").pack(anchor="w", padx=12)

        progress_frame = ttk.Frame(self.window)
        progress_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.log = scrolledtext.ScrolledText(
            progress_frame,
            wrap="word",
            state=DISABLED,
            height=20,
        )
        self.log.pack(fill="both", expand=True)

        if self.dnd_enabled:
            self._register_drop_target(self.window)

    def _browse_folder(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.folder_path.set(selected)

    def _register_drop_target(self, widget) -> None:
        if not self.dnd_enabled:
            return
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", self._handle_drop)

    def _parse_drop_data(self, data: str) -> list[str]:
        paths = []
        for braced, plain in re.findall(r"\{([^}]*)\}|([^\s]+)", data):
            path = (braced or plain).strip()
            if path:
                paths.append(path)
        return paths

    def _handle_drop(self, event) -> None:
        if not getattr(event, "data", None):
            return

        dropped = self._parse_drop_data(str(event.data))
        if not dropped:
            return

        normalized = _normalize_path(dropped[0])
        candidate = Path(normalized)
        if candidate.is_dir():
            self.folder_path.set(str(candidate))

    def _start_conversion(self) -> None:
        raw_var = self.folder_path.get()
        raw_entry = self.path_entry.get()
        candidate = raw_entry if raw_entry.strip() else raw_var
        folder = _normalize_path(candidate)

        self._append_output(
            f"DEBUG path read: var={raw_var!r} entry={raw_entry!r} normalized={folder!r}\n"
        )

        if not folder:
            messagebox.showerror("Missing Folder", "Please provide a WAV folder path.")
            return

        folder_path = Path(folder)
        if not folder_path.exists() or not folder_path.is_dir():
            messagebox.showerror("Invalid Folder", "The selected path is not a valid folder.")
            return

        if self.process_running:
            return

        self._set_running_state(True)
        self._append_output(f"Starting conversion for: {folder_path}\n")

        thread = threading.Thread(target=self._run_conversion, args=(folder,), daemon=True)
        thread.start()

    def _run_conversion(self, folder: str) -> None:
        self.process_running = True
        writer = QueueTextWriter(self.output_queue)

        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                exit_code = convert.main(["convert.py", folder])
        except Exception as exc:
            writer.write(f"Error while running converter: {exc}\n")
            exit_code = 1

        writer.flush()
        self.output_queue.put(("done", str(exit_code)))

    def _poll_queue(self) -> None:
        try:
            while True:
                event, payload = self.output_queue.get_nowait()
                if event == "line":
                    self._append_output(payload)
                elif event == "done":
                    self._append_output(f"\nConversion process exited with code: {payload}\n")
                    self._set_running_state(False)
                    self.process_running = False
                    if payload == "0":
                        self.window.after(50, self._show_success_popup_when_ready)
        except queue.Empty:
            pass
        finally:
            self.window.after(50, self._poll_queue)

    def _show_success_popup_when_ready(self) -> None:
        try:
            self.window.update_idletasks()
        except Exception:
            pass
        self.window.after(50, self._show_success_popup)

    def _show_success_popup(self) -> None:
        self.window.lift()
        try:
            self.window.focus_force()
        except Exception:
            pass
        messagebox.showinfo("Conversion Complete", "All files were converted successfully.")

    def _append_output(self, text: str) -> None:
        self.log.configure(state=NORMAL)
        self.log.insert(END, text)
        self.log.see(END)
        self.log.configure(state=DISABLED)

    def _set_running_state(self, running: bool) -> None:
        if running:
            self.start_button.config(state=DISABLED)
        else:
            self.start_button.config(state=NORMAL)

    def _maybe_auto_start(self) -> None:
        if len(sys.argv) > 1:
            provided = _normalize_path(sys.argv[1])
            if provided:
                self.folder_path.set(provided)
                self._start_conversion()

    def run(self) -> None:
        self.window.mainloop()


def main() -> None:
    app = ConverterGUI()
    app.run()


if __name__ == "__main__":
    main()
