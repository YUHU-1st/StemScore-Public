from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import __version__
from .constants import APP_NAME, DEFAULT_PROJECTS_ROOT, STAGE_NAMES
from .model_server import start_background as start_model_manager
from .runtime_repair import RuntimeRepairer, is_repairable_runtime_error
from .training.server import start_background
from .workflow import ALL_DEREVERB_ROLES, DEFAULT_DEREVERB_ROLES, Workflow, find_project


STATUS_TEXT = {
    "locked": "锁定",
    "ready": "待运行",
    "running": "处理中",
    "review_required": "等待人工审查",
    "reviewed": "已审查通过",
    "failed": "失败",
    "skipped": "已跳过",
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("1160x760")
        self.minsize(920, 640)
        self.workflow: Workflow | None = None
        self.current_stage = tk.IntVar(value=1)
        self.status = tk.StringVar(value="新建项目后，从第 1 步开始。")
        self.dereverb_vars = {
            role: tk.BooleanVar(value=role in DEFAULT_DEREVERB_ROLES)
            for role in ALL_DEREVERB_ROLES
        }
        self.dereverb_checks: list[ttk.Checkbutton] = []
        self._training_server = None
        self._model_server = None
        self._repair_stage: int | None = None
        self._build()

    def _build(self) -> None:
        self.configure(bg="#09111f")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#09111f")
        style.configure("TLabel", background="#09111f", foreground="#dce8f8", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 22, "bold"), foreground="#f4f8ff")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=8)
        style.configure("Accent.TButton", background="#2f6fec", foreground="white")
        style.configure("Treeview", background="#101c2d", fieldbackground="#101c2d", foreground="#e5eefb", rowheight=28)
        style.configure("Treeview.Heading", background="#182943", foreground="#ffffff")

        header = ttk.Frame(self, padding=(22, 18, 22, 10))
        header.pack(fill="x")
        ttk.Label(header, text="StemScore", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="本地 AI 分轨 · 去混响 · 扒谱", foreground="#82adff").pack(side="left", padx=16, pady=(8, 0))
        ttk.Button(header, text="新建项目", command=self._new_project).pack(side="right")
        ttk.Button(header, text="打开项目", command=self._open_project).pack(side="right", padx=8)
        ttk.Button(header, text="训练中心", command=self._open_training).pack(side="right")
        ttk.Button(header, text="模型管理", command=self._open_models).pack(side="right", padx=8)

        body = ttk.Frame(self, padding=(22, 8, 22, 14))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=280)
        left.pack(side="left", fill="y", padx=(0, 16))
        ttk.Label(left, text="五步工作流", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(0, 10))
        self.stage_list = tk.Listbox(
            left,
            width=34,
            height=10,
            bg="#101c2d",
            fg="#e5eefb",
            selectbackground="#2f6fec",
            selectforeground="white",
            relief="flat",
            font=("Microsoft YaHei UI", 10),
            activestyle="none",
        )
        self.stage_list.pack(fill="x")
        self.stage_list.bind("<<ListboxSelect>>", self._select_stage)
        dereverb_frame = ttk.LabelFrame(left, text="第 4 步去混响角色", padding=8)
        dereverb_frame.pack(fill="x", pady=(12, 0))
        role_labels = {
            "lead_vocal": "主唱",
            "harmony_vocal": "和声",
            "bass": "贝斯",
            "drums": "鼓",
            "guitar": "吉他",
            "piano": "钢琴",
            "other": "其他",
        }
        for index, role in enumerate(ALL_DEREVERB_ROLES):
            check = ttk.Checkbutton(
                dereverb_frame,
                text=role_labels[role],
                variable=self.dereverb_vars[role],
                command=self._save_dereverb_selection,
            )
            check.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=1)
            self.dereverb_checks.append(check)
        ttk.Label(
            dereverb_frame,
            text="默认仅主唱；未勾选轨保留原分轨供 MIDI 使用。",
            foreground="#9fb3ce",
            wraplength=235,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Separator(left).pack(fill="x", pady=16)
        self.run_button = ttk.Button(left, text="运行当前步骤", style="Accent.TButton", command=self._run_selected)
        self.run_button.pack(fill="x", pady=4)
        self.direct_button = ttk.Button(left, text="直接运行所选步骤", command=self._run_direct)
        self.direct_button.pack(fill="x", pady=4)
        self.approve_button = ttk.Button(left, text="审查通过，开始下一步", command=self._approve_and_next)
        self.approve_button.pack(fill="x", pady=4)
        ttk.Button(left, text="重试当前步骤", command=self._retry).pack(fill="x", pady=4)
        self.skip_button = ttk.Button(left, text="跳过当前步骤", command=self._skip_stage)
        self.skip_button.pack(fill="x", pady=4)
        self.repair_button = ttk.Button(left, text="一键修复运行环境", command=self._repair_runtime)
        self.repair_button.pack(fill="x", pady=4)
        self.repair_button.configure(state="disabled")
        self.analysis_button = ttk.Button(
            left,
            text="生成音乐分析 / Music 3 提示词",
            command=self._run_music_analysis,
        )
        self.analysis_button.pack(fill="x", pady=4)
        ttk.Button(left, text="打开项目目录", command=self._open_folder).pack(fill="x", pady=4)
        ttk.Label(left, text="每一步完成后都会停下。试听或检查文件，确认后再进入下一步。", foreground="#ffc766", wraplength=245).pack(anchor="w", pady=16)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        title_row = ttk.Frame(right)
        title_row.pack(fill="x")
        self.project_label = ttk.Label(title_row, text="未打开项目", font=("Microsoft YaHei UI", 13, "bold"))
        self.project_label.pack(side="left")
        ttk.Label(title_row, textvariable=self.status, foreground="#9fb3ce").pack(side="right")

        self.artifacts = ttk.Treeview(right, columns=("role", "type", "size", "path"), show="headings", height=9)
        for key, label, width in (
            ("role", "内容", 150),
            ("type", "类型", 90),
            ("size", "大小", 90),
            ("path", "文件", 520),
        ):
            self.artifacts.heading(key, text=label)
            self.artifacts.column(key, width=width, anchor="w")
        self.artifacts.pack(fill="x", pady=(12, 8))
        self.artifacts.bind("<Double-1>", lambda _event: self._play_selected())
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(0, 8))
        ttk.Button(row, text="播放 / 打开所选文件", command=self._play_selected).pack(side="left")
        ttk.Button(row, text="查看步骤审计 JSON", command=self._open_audit).pack(side="left", padx=8)

        ttk.Label(right, text="执行日志", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.log = tk.Text(
            right,
            bg="#070d17",
            fg="#cedbef",
            insertbackground="white",
            relief="flat",
            font=("Consolas", 9),
            padx=12,
            pady=10,
        )
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self._refresh()

    def _append_log(self, text: str) -> None:
        def update() -> None:
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
        self.after(0, update)

    def _new_project(self) -> None:
        source = filedialog.askopenfilename(
            title="选择音乐文件",
            filetypes=(("音频", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif *.aiff"), ("全部文件", "*.*")),
        )
        if not source:
            return
        root = filedialog.askdirectory(title="选择项目保存目录", initialdir=str(DEFAULT_PROJECTS_ROOT.parent))
        if not root:
            root = str(DEFAULT_PROJECTS_ROOT)
        try:
            self.workflow = Workflow.create(
                Path(source),
                Path(root),
                dereverb_roles=self._selected_dereverb_roles(),
            )
            self._repair_stage = None
            self.current_stage.set(1)
            self._refresh()
            self._run_stage(1)
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))

    def _open_project(self) -> None:
        path = filedialog.askopenfilename(title="打开 StemScore 项目", filetypes=(("StemScore 项目", "stemscore-project.json"), ("JSON", "*.json")))
        if not path:
            return
        try:
            self.workflow = Workflow.load(find_project(Path(path)))
            self._repair_stage = None
            self._load_dereverb_selection()
            selected = next((stage.number for stage in self.workflow.manifest.stages if stage.status not in {"reviewed", "skipped", "locked"}), 5)
            self.current_stage.set(selected)
            self._refresh()
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))

    def _run_stage(self, number: int, force: bool = False) -> None:
        if not self.workflow:
            return
        try:
            self.workflow.prepare_stage(number, allow_unreviewed=force)
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))
            self._refresh()
            return
        self.status.set(f"第 {number} 步处理中…")
        self._refresh()
        self.update_idletasks()

        def work() -> None:
            try:
                assert self.workflow is not None
                self.workflow.run_stage(number, self._append_log, allow_unreviewed=force)
                self.after(0, lambda: self._stage_done(number))
            except Exception as error:
                self.after(0, lambda error=error: self._stage_failed(number, error))

        threading.Thread(target=work, daemon=True).start()

    def _stage_done(self, number: int) -> None:
        self._repair_stage = None
        self.status.set(f"第 {number} 步完成，等待人工审查。")
        self.bell()
        self._refresh()
        messagebox.showinfo(APP_NAME, f"第 {number} 步“{STAGE_NAMES[number]}”已完成。\n\n请试听并检查产物；确认无误后点击“审查通过，开始下一步”。")

    def _stage_failed(self, number: int, error: Exception) -> None:
        repairable = is_repairable_runtime_error(error)
        self._repair_stage = number if repairable else None
        self.status.set(f"第 {number} 步失败，可一键修复运行环境。" if repairable else f"第 {number} 步失败。")
        self._append_log(f"错误：{error}")
        self._refresh()
        if repairable:
            messagebox.showerror(APP_NAME, f"{error}\n\n可点击左侧“一键修复运行环境”，StemScore 将只从固定的合法公开源下载并校验缺失组件；修复完成后会自动重试当前步骤。")
        else:
            messagebox.showerror(APP_NAME, str(error))

    def _repair_runtime(self) -> None:
        if not self.workflow or self._repair_stage is None:
            return
        number = self._repair_stage
        self.status.set("正在一键修复运行环境；首次安装需要下载约 1.4 GB 模型和 GPU 依赖…")
        self.repair_button.configure(state="disabled")
        self._append_log("开始一键修复。仅使用固定版本的官方公开源，并对下载资产执行 SHA-256 校验。")

        def work() -> None:
            try:
                assert self.workflow is not None
                RuntimeRepairer(self.workflow.runtime).repair(self._append_log)
                self.after(0, lambda: self._repair_done(number))
            except Exception as error:
                self.after(0, lambda error=error: self._repair_failed(error))

        threading.Thread(target=work, daemon=True).start()

    def _repair_done(self, number: int) -> None:
        if not self.workflow:
            return
        self._repair_stage = None
        self.status.set("运行环境修复完成，正在自动重试失败步骤…")
        if self.workflow.manifest.stage(number).status == "failed":
            self.workflow.retry(number)
        self._refresh()
        self._run_stage(number)

    def _repair_failed(self, error: Exception) -> None:
        self.status.set("运行环境一键修复失败；可查看日志后再次重试。")
        self._append_log(f"一键修复失败：{error}")
        self._refresh()
        messagebox.showerror(APP_NAME, f"运行环境一键修复失败：\n\n{error}")

    def _run_selected(self) -> None:
        if self.workflow:
            self._run_stage(self.current_stage.get())

    def _run_direct(self) -> None:
        if not self.workflow:
            return
        number = self.current_stage.get()
        stage = self.workflow.manifest.stage(number)
        if stage.status == "running":
            messagebox.showinfo(APP_NAME, f"第 {number} 步已经在处理中。")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"直接运行第 {number} 步“{STAGE_NAMES[number]}”？\n\n"
            "这会跳过前置步骤的人工审查状态检查，但仍要求真正需要的上游文件已经存在。",
        ):
            return
        self._run_stage(number, force=True)

    def _skip_stage(self) -> None:
        if not self.workflow:
            return
        number = self.current_stage.get()
        if not messagebox.askyesno(APP_NAME, f"确定跳过第 {number} 步“{STAGE_NAMES[number]}”？"):
            return
        try:
            self.workflow.skip(number)
            if number < 5:
                self.current_stage.set(number + 1)
            self.status.set(f"第 {number} 步已跳过。")
            self._refresh()
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))

    def _approve_and_next(self) -> None:
        if not self.workflow:
            return
        number = self.current_stage.get()
        try:
            self.workflow.approve(number)
            if number < 5:
                self.current_stage.set(number + 1)
                self._refresh()
                self._run_stage(number + 1)
            else:
                self.status.set("五步流程已全部审查通过。")
                self._refresh()
                messagebox.showinfo(APP_NAME, "项目已完成并通过全部人工审查。")
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))

    def _retry(self) -> None:
        if not self.workflow:
            return
        number = self.current_stage.get()
        try:
            self.workflow.retry(number)
            self._refresh()
            self._run_stage(number)
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))

    def _run_music_analysis(self) -> None:
        if not self.workflow:
            return
        self.status.set("正在本地分析 BPM、曲风、配器和编曲结构…")
        self.analysis_button.configure(state="disabled")

        def work() -> None:
            try:
                assert self.workflow is not None
                artifacts = self.workflow.run_music_analysis(self._append_log)
                self.after(0, lambda: self._music_analysis_done(artifacts))
            except Exception as error:
                self.after(0, lambda error=error: self._music_analysis_failed(error))

        threading.Thread(target=work, daemon=True).start()

    def _music_analysis_done(self, artifacts) -> None:
        self.status.set("音乐分析报告与 Music 3 提示词已生成。")
        self.bell()
        self._refresh()
        output = Path(artifacts[0].path).parent
        messagebox.showinfo(
            APP_NAME,
            f"已在本地生成分析报告、可解释审计文件和 MiniMax Music 3 提示词。\n\n{output}",
        )
        os.startfile(output)

    def _music_analysis_failed(self, error: Exception) -> None:
        self.status.set("音乐分析失败。")
        self._append_log(f"错误：{error}")
        self._refresh()
        messagebox.showerror(APP_NAME, str(error))

    def _select_stage(self, _event=None) -> None:
        selected = self.stage_list.curselection()
        if selected:
            self.current_stage.set(selected[0] + 1)
            self._refresh_artifacts()

    def _selected_dereverb_roles(self) -> list[str]:
        return [role for role in ALL_DEREVERB_ROLES if self.dereverb_vars[role].get()]

    def _load_dereverb_selection(self) -> None:
        if not self.workflow:
            return
        selected = set(self.workflow.manifest.settings.get("dereverb_roles", DEFAULT_DEREVERB_ROLES))
        for role in ALL_DEREVERB_ROLES:
            self.dereverb_vars[role].set(role in selected)

    def _save_dereverb_selection(self) -> None:
        if not self.workflow:
            return
        stage = self.workflow.manifest.stage(4)
        if stage.status not in {"locked", "ready", "failed"}:
            self._load_dereverb_selection()
            return
        self.workflow.manifest.settings["dereverb_roles"] = self._selected_dereverb_roles()
        self.workflow.manifest.save()

    def _refresh(self) -> None:
        self.stage_list.delete(0, "end")
        if not self.workflow:
            for number in range(1, 6):
                self.stage_list.insert("end", f"{number}. {STAGE_NAMES[number]}  ·  锁定")
            self.run_button.configure(state="disabled")
            self.direct_button.configure(state="disabled")
            self.approve_button.configure(state="disabled")
            self.skip_button.configure(state="disabled")
            self.analysis_button.configure(state="disabled")
            self.repair_button.configure(state="disabled")
            for check in self.dereverb_checks:
                check.configure(state="normal")
            return
        self.project_label.configure(text=self.workflow.manifest.title)
        for stage in self.workflow.manifest.stages:
            self.stage_list.insert("end", f"{stage.number}. {stage.name}  ·  {STATUS_TEXT[stage.status]}")
        number = self.current_stage.get()
        self.stage_list.selection_set(number - 1)
        stage = self.workflow.manifest.stage(number)
        self.run_button.configure(state="normal" if stage.status == "ready" else "disabled")
        self.direct_button.configure(state="disabled" if stage.status == "running" else "normal")
        self.approve_button.configure(state="normal" if stage.status == "review_required" else "disabled")
        self.skip_button.configure(state="disabled" if stage.status in {"running", "reviewed", "skipped"} else "normal")
        self.repair_button.configure(
            state="normal" if self._repair_stage is not None and stage.number == self._repair_stage else "disabled"
        )
        self.analysis_button.configure(
            state="normal" if self.workflow.manifest.stage(5).status == "reviewed" else "disabled"
        )
        dereverb_state = "normal" if self.workflow.manifest.stage(4).status in {"locked", "ready", "failed"} else "disabled"
        for check in self.dereverb_checks:
            check.configure(state=dereverb_state)
        self._refresh_artifacts()

    def _refresh_artifacts(self) -> None:
        for item in self.artifacts.get_children():
            self.artifacts.delete(item)
        if not self.workflow:
            return
        stage = self.workflow.manifest.stage(self.current_stage.get())
        for artifact in stage.artifacts:
            self.artifacts.insert("", "end", values=(artifact.role, artifact.media_type, self._size(artifact.size), artifact.path))

    @staticmethod
    def _size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return str(size)

    def _selected_path(self) -> Path | None:
        selection = self.artifacts.selection()
        if not selection:
            return None
        return Path(self.artifacts.item(selection[0], "values")[3])

    def _play_selected(self) -> None:
        path = self._selected_path()
        if path and path.exists():
            os.startfile(path)

    def _open_audit(self) -> None:
        if self.workflow:
            path = Path(self.workflow.manifest.root) / "audit" / f"stage-{self.current_stage.get()}.json"
            if path.exists():
                os.startfile(path)

    def _open_folder(self) -> None:
        if self.workflow:
            os.startfile(self.workflow.manifest.root)

    def _open_training(self) -> None:
        if self._training_server is None:
            workspace = (
                Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[1]
            )
            self._training_server, url = start_background(workspace)
        else:
            host, port = self._training_server.server_address
            url = f"http://{host}:{port}/"
        webbrowser.open(url)

    def _open_models(self) -> None:
        if self._model_server is None:
            self._model_server, url = start_model_manager()
        else:
            host, port = self._model_server.server_address
            url = f"http://{host}:{port}/"
        webbrowser.open(url)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()

