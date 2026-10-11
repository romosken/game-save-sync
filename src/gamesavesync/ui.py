"""Settings window for profiles, cloud login, and manual sync."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from gamesavesync.config import AppConfig, ConfigError, ConfigStore, Profile, SaveFolder
from gamesavesync.logging_setup import log_path, setup_logging
from gamesavesync.rclone import Rclone, RcloneError
from gamesavesync.shortcuts import create_desktop_shortcut
from gamesavesync.state import StateStore, attention_names, forget_profile, rename_profile, status_text
from gamesavesync.sync import ProfileSyncResult, pull_profile, push_profile, sync_all, sync_profile_both

PROVIDERS = (
    ("Google Drive", "drive"),
    ("OneDrive", "onedrive"),
    ("Dropbox", "dropbox"),
)


def _tree_color_map(style: ttk.Style, option: str):
    return [
        item
        for item in style.map("Treeview", query_opt=option)
        if item[:2] != ("!disabled", "!selected")
    ]


def run_ui() -> None:
    setup_logging()
    try:
        app = App()
    except ConfigError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Game Save Sync", str(exc), parent=root)
        root.destroy()
        return
    app.mainloop()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Game Save Sync")
        self.geometry("1000x680")
        self.minsize(860, 560)
        self.config_store = ConfigStore()
        self.state_store = StateStore()
        self.app_config = self.config_store.load()
        self.app_state = self.state_store.load()
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._buttons: list[ttk.Button] = []
        self._build()
        self._reload_fields()
        self._refresh_profiles()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        style = ttk.Style(self)
        if self.tk.call("tk", "windowingsystem") == "win32" and "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        # Themed treeviews ignore row colors unless this map is cleared.
        style.map(
            "Treeview",
            foreground=_tree_color_map(style, "foreground"),
            background=_tree_color_map(style, "background"),
        )

        self.banner = tk.Frame(self, bg="#fde8e8")
        self.banner_label = tk.Label(self.banner, bg="#fde8e8", fg="#8a1c1c", anchor="w")
        self.banner_label.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        banner_button = ttk.Button(self.banner, text="Sync all", command=self._sync_all)
        banner_button.pack(side="right", padx=12, pady=6)
        self._buttons.append(banner_button)
        self.banner.grid(row=0, column=0, sticky="ew")
        self.banner.grid_remove()

        body = ttk.Frame(self, padding=12)
        body.grid(row=1, column=0, sticky="nsew")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("profile", "application", "status")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("profile", text="Profile")
        self.tree.heading("application", text="Application")
        self.tree.heading("status", text="Status")
        self.tree.column("profile", width=160, stretch=True)
        self.tree.column("application", width=140, stretch=True)
        self.tree.column("status", width=180, stretch=True)
        self.tree.tag_configure("failed", foreground="#b00020")
        self.tree.tag_configure("pending", foreground="#a15c00")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_details())
        self.tree.bind("<Double-1>", lambda _event: self._edit_profile())

        left_buttons = ttk.Frame(left)
        left_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._add_button(left_buttons, "Add", self._add_profile).pack(side="left")
        self._add_button(left_buttons, "Edit", self._edit_profile).pack(side="left", padx=(8, 0))
        self._add_button(left_buttons, "Delete", self._delete_profile).pack(side="left", padx=(8, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(4, weight=1)
        self.detail_title = tk.StringVar(value="No profile selected")
        self.detail_meta = tk.StringVar(value="")
        self.detail_error = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.detail_title, font=("", 13, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(right, textvariable=self.detail_meta, wraplength=340, justify="left").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            right,
            textvariable=self.detail_error,
            wraplength=340,
            justify="left",
            foreground="#b00020",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(right, text="Last folder results").grid(row=3, column=0, sticky="w", pady=(10, 4))
        folder_columns = ("folder", "result", "down", "up", "backup")
        self.folder_tree = ttk.Treeview(
            right, columns=folder_columns, show="headings", height=6, selectmode="browse"
        )
        for key, heading, width in (
            ("folder", "Folder", 90),
            ("result", "Result", 70),
            ("down", "Down", 50),
            ("up", "Up", 50),
            ("backup", "Backup", 60),
        ):
            self.folder_tree.heading(key, text=heading)
            self.folder_tree.column(key, width=width, stretch=True)
        self.folder_tree.tag_configure("failed", foreground="#b00020")
        self.folder_tree.grid(row=4, column=0, sticky="nsew")

        actions = ttk.Frame(right)
        actions.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.sync_one = self._add_button(actions, "Sync now", self._sync_selected)
        self.sync_one.pack(side="left")
        self._add_button(actions, "Pull now", lambda: self._sync_selected("pull")).pack(
            side="left", padx=(8, 0)
        )
        self._add_button(actions, "Push now", lambda: self._sync_selected("push")).pack(
            side="left", padx=(8, 0)
        )
        more = ttk.Frame(right)
        more.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self._add_button(more, "Create shortcut", self._create_shortcut).pack(side="left")
        self._add_button(more, "View log", self._view_log).pack(side="left", padx=(8, 0))

        bottom = ttk.Frame(self, padding=(12, 0, 12, 12))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)
        self.status_var = tk.StringVar(value="Ready")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ttk.Label(bottom, textvariable=self.status_var).grid(row=1, column=0, columnspan=4, sticky="w")

        settings = ttk.Frame(bottom)
        settings.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self._add_button(settings, "Sync all", self._sync_all).pack(side="left")
        self._add_button(settings, "Connect cloud", self._connect_cloud).pack(side="left", padx=(8, 0))
        ttk.Label(settings, text="Remote").pack(side="left", padx=(16, 4))
        self.remote_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.remote_var, width=16).pack(side="left")
        ttk.Label(settings, text="Backups to keep").pack(side="left", padx=(16, 4))
        self.retention_var = tk.StringVar(value="10")
        ttk.Spinbox(settings, from_=0, to=100, textvariable=self.retention_var, width=5).pack(
            side="left"
        )
        self._add_button(settings, "Save settings", self._save_settings).pack(side="left", padx=(8, 0))

    def _add_button(self, parent, text: str, command) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command)
        self._buttons.append(button)
        return button

    def _on_close(self) -> None:
        if self._busy:
            messagebox.showinfo("Game Save Sync", "Wait for the current sync to finish.", parent=self)
            return
        self.destroy()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._buttons:
            button.configure(state=state)

    def _reload_fields(self) -> None:
        self.remote_var.set(self.app_config.remote)
        self.retention_var.set(str(self.app_config.backup_retention))

    def _selected_name(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            return None
        iid = str(selection[0])
        prefix = "profile:"
        if iid.startswith(prefix):
            return iid[len(prefix) :]
        return iid

    def _selected_profile(self) -> Profile | None:
        name = self._selected_name()
        if not name:
            return None
        return self.app_config.get_profile(name)

    def _refresh_profiles(self) -> None:
        selected = self._selected_name()
        self.tree.delete(*self.tree.get_children())
        for profile in self.app_config.profiles:
            status = self.app_state.profiles.get(profile.name)
            tags: tuple[str, ...] = ()
            if status and status.last_result == "failed":
                tags = ("failed",)
            elif status and status.pending_push:
                tags = ("pending",)
            self.tree.insert(
                "",
                "end",
                iid=f"profile:{profile.name}",
                values=(profile.name, Path(profile.exe).name, status_text(status)),
                tags=tags,
            )
        if selected and self.tree.exists(f"profile:{selected}"):
            self.tree.selection_set(f"profile:{selected}")
            self.tree.focus(f"profile:{selected}")
        self._refresh_banner()
        self._show_details()

    def _refresh_banner(self) -> None:
        names = attention_names([profile.name for profile in self.app_config.profiles], self.app_state)
        if not names:
            self.banner.grid_remove()
            return
        count = len(names)
        if count == 1:
            text = "1 profile needs attention"
        else:
            text = f"{count} profiles need attention"
        self.banner_label.configure(text=text)
        self.banner.grid()

    def _show_details(self) -> None:
        self.folder_tree.delete(*self.folder_tree.get_children())
        profile = self._selected_profile()
        if profile is None:
            self.detail_title.set("No profile selected")
            self.detail_meta.set("")
            self.detail_error.set("")
            self.sync_one.configure(text="Sync now")
            return
        status = self.app_state.profiles.get(profile.name)
        self.detail_title.set(profile.name)
        folders = ", ".join(folder.label for folder in profile.folders) or "no folders"
        when = status_text(status)
        self.detail_meta.set(f"{profile.exe}\nFolders: {folders}\n{when}")
        error = ""
        if status and status.last_result == "failed":
            error = status.last_error
        elif status and status.pending_push:
            error = status.last_error or "A previous upload did not finish."
        self.detail_error.set(error)
        if status and (status.last_result == "failed" or status.pending_push):
            self.sync_one.configure(text="Retry")
        else:
            self.sync_one.configure(text="Sync now")
        if status:
            for label, folder_status in status.folders.items():
                tags = ("failed",) if folder_status.result == "failed" else ()
                self.folder_tree.insert(
                    "",
                    "end",
                    values=(
                        label,
                        folder_status.result,
                        folder_status.downloaded,
                        folder_status.uploaded,
                        folder_status.backed_up,
                    ),
                    tags=tags,
                )
                if folder_status.error and not error:
                    self.detail_error.set(folder_status.error)

    def _read_settings(self) -> bool:
        remote = self.remote_var.get().strip()
        try:
            retention = int(self.retention_var.get())
        except ValueError:
            messagebox.showerror("Game Save Sync", "Backups to keep must be a whole number.", parent=self)
            return False
        self.app_config.remote = remote
        self.app_config.backup_retention = retention
        try:
            self.app_config.validate()
        except ConfigError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            self.app_config = self.config_store.load()
            self._reload_fields()
            return False
        self.config_store.save(self.app_config)
        return True

    def _save_settings(self) -> None:
        if self._busy:
            return
        if self._read_settings():
            self.status_var.set("Settings saved")

    def _require_profile(self) -> Profile | None:
        profile = self._selected_profile()
        if profile is None:
            messagebox.showinfo("Game Save Sync", "Select a profile first.", parent=self)
        return profile

    def _add_profile(self) -> None:
        if self._busy:
            return
        dialog = ProfileDialog(self, None)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self.app_config.profiles.append(dialog.result)
        if not self._persist_profiles():
            return
        self._refresh_profiles()
        self.tree.selection_set(f"profile:{dialog.result.name}")

    def _edit_profile(self) -> None:
        if self._busy:
            return
        current = self._require_profile()
        if current is None:
            return
        dialog = ProfileDialog(self, current)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        index = self.app_config.profiles.index(current)
        previous_name = current.name
        self.app_config.profiles[index] = dialog.result
        if not self._persist_profiles():
            return
        if previous_name != dialog.result.name:
            rename_profile(self.state_store, previous_name, dialog.result.name)
            self.app_state = self.state_store.load()
        self._refresh_profiles()
        if self.tree.exists(f"profile:{dialog.result.name}"):
            self.tree.selection_set(f"profile:{dialog.result.name}")

    def _delete_profile(self) -> None:
        if self._busy:
            return
        profile = self._require_profile()
        if profile is None:
            return
        if not messagebox.askyesno(
            "Game Save Sync",
            f"Remove {profile.name} from this PC? Cloud saves are left in place.",
            parent=self,
        ):
            return
        self.app_config.profiles = [
            item for item in self.app_config.profiles if item.name != profile.name
        ]
        if not self._persist_profiles():
            return
        forget_profile(self.state_store, profile.name)
        self.app_state = self.state_store.load()
        self._refresh_profiles()

    def _persist_profiles(self) -> bool:
        try:
            self.config_store.save(self.app_config)
        except ConfigError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            self.app_config = self.config_store.load()
            self._reload_fields()
            self._refresh_profiles()
            return False
        return True

    def _sync_all(self) -> None:
        if self._busy or not self._read_settings():
            return
        config = self.app_config
        store = self.state_store

        def work() -> list[ProfileSyncResult]:
            def on_progress(percent: float, message: str) -> None:
                self._queue.put(("progress", (percent, 100, message)))

            results = sync_all(config, Rclone(), store, on_progress=on_progress)
            self._queue.put(("progress", (100, 100, "Sync finished")))
            return results

        self._start_worker(work, "sync-done")

    def _sync_selected(self, direction: str = "both") -> None:
        if self._busy or not self._read_settings():
            return
        profile = self._require_profile()
        if profile is None:
            return
        config = self.app_config
        store = self.state_store
        label = {"pull": "Pulling", "push": "Pushing"}.get(direction, "Syncing")

        def work() -> list[ProfileSyncResult]:
            def on_progress(percent: float, message: str) -> None:
                self._queue.put(("progress", (percent, 100, message)))

            self._queue.put(("progress", (0, 100, f"{label} {profile.name}...")))
            rclone = Rclone()
            if direction == "pull":
                result = pull_profile(profile, config, rclone, store, on_progress=on_progress)
            elif direction == "push":
                result = push_profile(profile, config, rclone, store, on_progress=on_progress)
            else:
                result = sync_profile_both(
                    profile,
                    config,
                    rclone,
                    store,
                    skip_if_running=True,
                    on_progress=on_progress,
                )
            self._queue.put(("progress", (100, 100, "Sync finished")))
            return [result]

        self._start_worker(work, "sync-done")

    def _connect_cloud(self) -> None:
        if self._busy:
            return
        dialog = ConnectDialog(
            self,
            self.remote_var.get().strip() or "gdrive",
            client_id=self.app_config.drive_client_id,
            client_secret=self.app_config.drive_client_secret,
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return
        remote_name, backend, client_id, client_secret = dialog.result
        self.remote_var.set(remote_name)
        if backend == "drive":
            self.app_config.drive_client_id = client_id
            self.app_config.drive_client_secret = client_secret
        if not self._read_settings():
            return

        def work() -> str:
            self._queue.put(("progress", (0, 1, "Waiting for the cloud login in your browser...")))
            rclone = Rclone()
            rclone.connect(
                remote_name,
                backend,
                client_id=client_id,
                client_secret=client_secret,
            )
            rclone.check_connection(remote_name)
            return remote_name

        self._start_worker(work, "connected")

    def _create_shortcut(self) -> None:
        profile = self._require_profile()
        if profile is None:
            return
        try:
            path = create_desktop_shortcut(profile.name, profile.exe)
        except OSError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            return
        messagebox.showinfo("Game Save Sync", f"Shortcut created:\n{path}", parent=self)

    def _view_log(self) -> None:
        LogWindow(self)

    def _start_worker(self, work, kind: str) -> None:
        self._set_busy(True)
        self.progress.configure(value=0, maximum=100)
        self.status_var.set("Working...")

        def runner() -> None:
            try:
                self._queue.put((kind, work()))
            except Exception as exc:
                self._queue.put(("error", exc))

        threading.Thread(target=runner, daemon=False).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    value, maximum, text = payload
                    self.progress.configure(maximum=max(float(maximum), 1), value=float(value))
                    self.status_var.set(str(text))
                elif kind == "sync-done":
                    self._finish_sync(payload)
                elif kind == "connected":
                    self._finish_connect(str(payload))
                elif kind == "error":
                    self._finish_error(payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def _finish_sync(self, results: list[ProfileSyncResult]) -> None:
        self._set_busy(False)
        self.app_state = self.state_store.load()
        self._refresh_profiles()
        failed = [item for item in results if item.result == "failed"]
        if failed:
            self.status_var.set(f"{len(failed)} profile(s) failed")
        else:
            self.status_var.set("Sync finished")
        show_summary(self, results)

    def _finish_connect(self, remote_name: str) -> None:
        self._set_busy(False)
        self.remote_var.set(remote_name)
        self.app_config.remote = remote_name
        try:
            self.config_store.save(self.app_config)
        except ConfigError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            return
        self.progress.configure(value=1, maximum=1)
        self.status_var.set(f"Connected to {remote_name}")
        messagebox.showinfo("Game Save Sync", f"Cloud remote {remote_name} is ready.", parent=self)

    def _finish_error(self, exc: BaseException) -> None:
        self._set_busy(False)
        self.app_state = self.state_store.load()
        self._refresh_profiles()
        self.status_var.set("Failed")
        if isinstance(exc, (RcloneError, ConfigError, OSError)):
            message = str(exc)
        else:
            message = str(exc) or exc.__class__.__name__
        messagebox.showerror("Game Save Sync", message, parent=self)


def show_summary(parent: tk.Misc, results: list[ProfileSyncResult]) -> None:
    window = tk.Toplevel(parent)
    window.title("Sync summary")
    window.geometry("640x360")
    window.transient(parent)
    lines = []
    if not results:
        lines.append("No profiles to sync.")
    for result in results:
        suffix = f": {result.error}" if result.error else ""
        lines.append(f"{result.name}: {result.result}{suffix}")
        for folder in result.folders:
            state = "ok" if folder.ok else "failed"
            detail = folder.error or (
                f"downloaded {folder.downloaded}, uploaded {folder.uploaded}, "
                f"backed up {folder.backed_up}"
            )
            lines.append(f"    {folder.label}: {state} ({detail})")
    text = scrolledtext.ScrolledText(window, wrap="word")
    text.pack(fill="both", expand=True, padx=12, pady=12)
    text.insert("1.0", "\n".join(lines))
    text.configure(state="disabled")
    ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 12))


class ProfileDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, profile: Profile | None) -> None:
        super().__init__(master)
        self.result: Profile | None = None
        self.title("Edit profile" if profile else "Add profile")
        self.resizable(False, False)
        self.transient(master)
        self.folders: list[SaveFolder] = []
        if profile:
            self.folders = [
                SaveFolder(folder.label, folder.path, list(folder.exclude)) for folder in profile.folders
            ]
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        self.name_var = tk.StringVar(value=profile.name if profile else "")
        self.exe_var = tk.StringVar(value=profile.exe if profile else "")
        self.args_var = tk.StringVar(value=profile.args if profile else "")
        self.wait_var = tk.StringVar(value=profile.wait_process if profile else "")
        self._row(frame, 0, "Name", self.name_var)
        exe_row = self._row(frame, 1, "Application", self.exe_var, width=48)
        ttk.Button(exe_row, text="Browse", command=self._browse_exe).grid(row=0, column=2, padx=(6, 0))
        self._row(frame, 2, "Arguments", self.args_var, width=48)
        self._row(frame, 3, "Wait for process", self.wait_var, width=48)
        ttk.Label(
            frame,
            text="Leave wait-for-process empty unless the application starts another program and exits.",
            wraplength=460,
        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Save folders").grid(row=5, column=0, sticky="nw")
        self.folder_list = tk.Listbox(frame, width=62, height=6)
        self.folder_list.grid(row=5, column=1, sticky="ew")
        folder_buttons = ttk.Frame(frame)
        folder_buttons.grid(row=5, column=2, sticky="n", padx=(6, 0))
        ttk.Button(folder_buttons, text="Add", command=self._add_folder).pack(fill="x")
        ttk.Button(folder_buttons, text="Edit", command=self._edit_folder).pack(fill="x", pady=4)
        ttk.Button(folder_buttons, text="Remove", command=self._remove_folder).pack(fill="x")
        ttk.Label(
            frame,
            text="For an emulator, choose the shared save root. Every game in that folder syncs together.",
            wraplength=460,
        ).grid(row=6, column=1, columnspan=2, sticky="w", pady=(6, 8))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=1, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right", padx=(0, 8))
        self._fill_folders()
        self.grab_set()
        self.bind("<Escape>", lambda _event: self.destroy())

    def _row(self, parent, row: int, label: str, variable: tk.StringVar, width: int = 40) -> ttk.Frame:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Entry(holder, textvariable=variable, width=width).grid(row=0, column=0, sticky="ew")
        return holder

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose the application",
            filetypes=[("Applications", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.exe_var.set(path)

    def _fill_folders(self) -> None:
        self.folder_list.delete(0, "end")
        for folder in self.folders:
            exclude = f"  exclude {', '.join(folder.exclude)}" if folder.exclude else ""
            self.folder_list.insert("end", f"{folder.label}:  {folder.path}{exclude}")

    def _add_folder(self) -> None:
        dialog = FolderDialog(self, None)
        self.wait_window(dialog)
        self.grab_set()
        if dialog.result:
            self.folders.append(dialog.result)
            self._fill_folders()

    def _edit_folder(self) -> None:
        selection = self.folder_list.curselection()
        if not selection:
            return
        index = selection[0]
        dialog = FolderDialog(self, self.folders[index])
        self.wait_window(dialog)
        self.grab_set()
        if dialog.result:
            self.folders[index] = dialog.result
            self._fill_folders()

    def _remove_folder(self) -> None:
        selection = self.folder_list.curselection()
        if not selection:
            return
        del self.folders[selection[0]]
        self._fill_folders()

    def _save(self) -> None:
        profile = Profile(
            name=self.name_var.get().strip(),
            exe=self.exe_var.get().strip(),
            args=self.args_var.get(),
            wait_process=self.wait_var.get().strip(),
            folders=list(self.folders),
        )
        try:
            AppConfig(profiles=[profile]).validate()
        except ConfigError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            return
        self.result = profile
        self.destroy()


class FolderDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, folder: SaveFolder | None) -> None:
        super().__init__(master)
        self.result: SaveFolder | None = None
        self.title("Save folder")
        self.resizable(False, False)
        self.transient(master)
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0)
        self.label_var = tk.StringVar(value=folder.label if folder else "")
        self.path_var = tk.StringVar(value=folder.path if folder else "")
        self.exclude_var = tk.StringVar(value=", ".join(folder.exclude) if folder else "")
        ttk.Label(frame, text="Label").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.label_var, width=42).grid(row=0, column=1, pady=4)
        ttk.Label(frame, text="Folder").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.path_var, width=42).grid(row=1, column=1, pady=4)
        ttk.Button(frame, text="Browse", command=self._browse).grid(row=1, column=2, padx=(6, 0))
        ttk.Label(frame, text="Exclude").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.exclude_var, width=42).grid(row=2, column=1, pady=4)
        ttk.Label(frame, text="Comma-separated patterns, for example *.tmp").grid(
            row=3, column=1, sticky="w"
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=1, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right", padx=(0, 8))
        self.grab_set()

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, title="Choose the save folder")
        if path:
            self.path_var.set(path)

    def _save(self) -> None:
        exclude = [part.strip() for part in self.exclude_var.get().split(",") if part.strip()]
        folder = SaveFolder(self.label_var.get().strip(), self.path_var.get().strip(), exclude)
        try:
            AppConfig(
                profiles=[Profile(name="check", exe="game.exe", folders=[folder])]
            ).validate()
        except ConfigError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            return
        self.result = folder
        self.destroy()


class ConnectDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        remote_name: str,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        super().__init__(master)
        self.result: tuple[str, str, str, str] | None = None
        self.title("Connect cloud")
        self.resizable(False, False)
        self.transient(master)
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0)
        ttk.Label(
            frame,
            text=(
                "A browser window opens so you can sign in. The password stays with the "
                "provider. This app stores only the login token on this PC."
            ),
            wraplength=480,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Remote name").grid(row=1, column=0, sticky="w", pady=4)
        self.remote_var = tk.StringVar(value=remote_name)
        ttk.Entry(frame, textvariable=self.remote_var, width=42).grid(row=1, column=1, sticky="w")
        self.backend = tk.StringVar(value="drive")
        for index, (label, backend) in enumerate(PROVIDERS):
            ttk.Radiobutton(
                frame,
                text=label,
                value=backend,
                variable=self.backend,
                command=self._toggle_drive_fields,
            ).grid(row=2 + index, column=1, sticky="w")
        self.drive_box = ttk.Frame(frame)
        self.drive_box.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(
            self.drive_box,
            text=(
                "Google retired rclone's shared login, so Drive needs your own client ID "
                "and client secret. In Google Cloud, enable the Drive API, add the scope "
                "https://www.googleapis.com/auth/drive.file, and create a Desktop app client."
            ),
            wraplength=480,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(
            self.drive_box,
            text="Open Google Cloud Console",
            command=lambda: webbrowser.open("https://console.cloud.google.com/"),
        ).grid(row=1, column=1, sticky="w", pady=(6, 4))
        ttk.Label(self.drive_box, text="Client ID").grid(row=2, column=0, sticky="w", pady=4)
        self.client_id_var = tk.StringVar(value=client_id)
        ttk.Entry(self.drive_box, textvariable=self.client_id_var, width=42).grid(
            row=2, column=1, sticky="w"
        )
        ttk.Label(self.drive_box, text="Client secret").grid(row=3, column=0, sticky="w", pady=4)
        self.client_secret_var = tk.StringVar(value=client_secret)
        ttk.Entry(self.drive_box, textvariable=self.client_secret_var, width=42, show="*").grid(
            row=3, column=1, sticky="w"
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=1, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Connect", command=self._save).pack(side="right", padx=(0, 8))
        self._toggle_drive_fields()
        self.grab_set()

    def _toggle_drive_fields(self) -> None:
        if self.backend.get() == "drive":
            self.drive_box.grid()
        else:
            self.drive_box.grid_remove()

    def _save(self) -> None:
        name = self.remote_var.get().strip()
        client_id = self.client_id_var.get().strip()
        client_secret = self.client_secret_var.get().strip()
        try:
            AppConfig(remote=name).validate()
        except ConfigError as exc:
            messagebox.showerror("Game Save Sync", str(exc), parent=self)
            return
        if self.backend.get() == "drive" and (not client_id or not client_secret):
            messagebox.showerror(
                "Game Save Sync",
                "Paste the client ID and client secret from your Google Cloud Desktop app.",
                parent=self,
            )
            return
        self.result = (name, self.backend.get(), client_id, client_secret)
        self.destroy()


class LogWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Sync log")
        self.geometry("760x420")
        self.transient(master)
        self.text = scrolledtext.ScrolledText(self, wrap="word")
        self.text.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Button(self, text="Refresh", command=self._load).pack(pady=(0, 12))
        self._load()

    def _load(self) -> None:
        path = log_path()
        if path.is_file():
            data = path.read_text(encoding="utf-8", errors="replace")
            if len(data) > 200_000:
                data = data[-200_000:]
        else:
            data = "No log entries yet."
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", data)
        self.text.see("end")
        self.text.configure(state="disabled")
