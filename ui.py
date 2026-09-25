"""
Jinnah Metadata Generator - User Interface Module (ui.py)
Contains Tkinter GUI dialogs, layout components, dynamic workflow loop, and application entry point.
"""

import os
import csv
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread
from PIL import Image, ImageTk

import engine as _engine_module

# Import dependencies and constants from engine module
from engine import (
    CONFIG, COLOR_BG, COLOR_CARD, COLOR_SIDEBAR, COLOR_SIDEBAR_ACTIVE,
    COLOR_TEXT, COLOR_SUBTEXT, COLOR_CONSOLE_BG, COLOR_ACCENT, COLOR_SUCCESS,
    COLOR_ERROR, COLOR_WARNING, AGENCY_HEADERS, psutil, append_to_file_log,
    save_config, calculate_sha256, get_processed_files_from_folder,
    generate_metadata_single_key, save_failed_queue_csv,
    validate_image_file, DynamicAPIManager, MAX_BACKOFF_SECONDS, reset_failed_jobs,
    check_eps_exists, report_missing_eps, extract_design_number,
    ThreadSafeCSVManager, get_design_number_for_file, validate_csv_headers,
    get_csv_headers, update_progress_status
)

# Bug Fix (Theme Mode): CONFIG["theme"] ("Dark"/"Light") was being saved from
# Settings but never actually applied — COLOR_BG/COLOR_CARD/etc. above were
# always engine.py's fixed dark-theme values, so switching themes did
# nothing even after restarting. Overriding these module-level names here,
# before the GUI is built, makes every widget constructed below (and in
# APIKeyDialog, which reads these same names at call time) pick up the
# correct palette for the saved theme.
globals().update(_engine_module.get_theme_colors(CONFIG.get("theme", "Dark")))

# ==========================================
# 1. API KEY MANAGER DIALOG
# ==========================================
class APIKeyDialog(tk.Toplevel):
    def __init__(self, parent, title="New API Key", item_data=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("400x260")
        self.configure(bg=COLOR_CARD)
        self.resizable(False, False)
        self.result = None

        tk.Label(self, text="🔑 API Key Configuration", font=("Arial", 11, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(pady=10)

        f_inputs = tk.Frame(self, bg=COLOR_CARD)
        f_inputs.pack(fill="x", padx=20, pady=5)

        tk.Label(f_inputs, text="Name:", fg=COLOR_TEXT, bg=COLOR_CARD, font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w", pady=5)
        self.ent_name = tk.Entry(f_inputs, bg=COLOR_BG, fg=COLOR_TEXT, insertbackground=COLOR_TEXT, width=30)
        self.ent_name.grid(row=0, column=1, pady=5)

        tk.Label(f_inputs, text="API Key:", fg=COLOR_TEXT, bg=COLOR_CARD, font=("Arial", 9, "bold")).grid(row=1, column=0, sticky="w", pady=5)
        self.ent_key = tk.Entry(f_inputs, bg=COLOR_BG, fg=COLOR_TEXT, insertbackground=COLOR_TEXT, width=30, show="*")
        self.ent_key.grid(row=1, column=1, pady=5)

        tk.Label(f_inputs, text="Status:", fg=COLOR_TEXT, bg=COLOR_CARD, font=("Arial", 9, "bold")).grid(row=2, column=0, sticky="w", pady=5)
        self.status_var = tk.StringVar(value="Enable")
        self.cmb_status = ttk.Combobox(f_inputs, textvariable=self.status_var, values=["Enable", "Disable"], state="readonly", width=28)
        self.cmb_status.grid(row=2, column=1, pady=5)

        if item_data:
            self.ent_name.insert(0, item_data.get("name", ""))
            self.ent_key.insert(0, item_data.get("key", ""))
            self.status_var.set(item_data.get("status", "Enable"))

        f_btns = tk.Frame(self, bg=COLOR_CARD)
        f_btns.pack(pady=15)

        tk.Button(f_btns, text="Save", bg=COLOR_SUCCESS, fg="#000", font=("Arial", 9, "bold"), width=10, command=self.on_save).pack(side="left", padx=10)
        tk.Button(f_btns, text="Cancel", bg=COLOR_SIDEBAR_ACTIVE, fg=COLOR_TEXT, font=("Arial", 9, "bold"), width=10, command=self.destroy).pack(side="left", padx=10)

        self.transient(parent)
        self.grab_set()
        parent.wait_window(self)

    def on_save(self):
        name = self.ent_name.get().strip()
        key = self.ent_key.get().strip()
        status = self.status_var.get()
        if not name or not key:
            messagebox.showwarning("Warning", "Name and API Key fields are required!", parent=self)
            return
        self.result = {"name": name, "key": key, "status": status}
        self.destroy()

# ==========================================
# 2. GUI USER INTERFACE
# ==========================================
class JinnahMetadataGeneratorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Jinnah Metadata Generator - Multi-Site Edition")
        self.root.geometry("1300x860")
        self.root.configure(bg=COLOR_BG)

        self.is_running = False
        self.is_paused = False
        # Bug Fix (Duplicate Concurrent Runs): holds the background Thread
        # running run_dynamic_workflow. Stop/Clear re-enable "Start Engine"
        # right away even though the old thread can still be winding down
        # (executor.shutdown(wait=True) waits for in-flight requests, which
        # can take a while with 429/503 backoffs in play). Clicking Start
        # again in that window used to launch a SECOND run_dynamic_workflow
        # on the same folder — each with its own independent API-rotation
        # state — doubling every request, causing the two engines to fight
        # each other for the same rate limits, and producing duplicated
        # "Engine Finished" / "CSV Export Completed" log lines once both
        # finished. start_engine() now checks this thread and refuses to
        # start a second run while the previous one is still alive.
        self._workflow_thread = None
        # Bug Fix (UI Freeze on Start): lets the dispatcher skip rendering a
        # preview that's already been superseded by a newer one (see
        # run_dynamic_workflow's dispatch loop / _apply_preview_data).
        self._preview_seq = 0
        self.selected_folder = ""
        self.api_call_count = 0
        self.site_vars = {}
        self._last_req_category = None
        self._last_resp_category = None

        self.setup_styles()
        self.build_main_layout()
        self.start_system_monitor()

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=COLOR_CARD, foreground=COLOR_TEXT, padding=[12, 6], font=('Arial', 9, 'bold'))
        style.map("TNotebook.Tab", background=[("selected", COLOR_ACCENT)], foreground=[("selected", "#000000")])
        
        style.configure("Treeview", background=COLOR_CARD, foreground=COLOR_TEXT, fieldbackground=COLOR_CARD, rowheight=25)
        style.configure("Treeview.Heading", background=COLOR_SIDEBAR, foreground=COLOR_ACCENT, font=('Arial', 9, 'bold'))
        style.map("Treeview", background=[('selected', COLOR_ACCENT)], foreground=[('selected', '#000000')])

    def build_main_layout(self):
        self.sidebar = tk.Frame(self.root, bg=COLOR_SIDEBAR, width=200)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        lbl_logo = tk.Label(self.sidebar, text="⚡ JINNAH METADATA", font=("Arial", 11, "bold"), fg=COLOR_ACCENT, bg=COLOR_SIDEBAR)
        lbl_logo.pack(pady=(15, 20))

        self.nav_buttons = {}
        pages = [
            ("📊 Dashboard", "dash"),
            ("🔴 Failed Queue", "failed"),
            ("💻 Live Console", "console"),
            ("⚙ Settings", "settings")
        ]

        for text, page_id in pages:
            btn = tk.Button(
                self.sidebar, text=text, font=("Arial", 9, "bold"), fg=COLOR_TEXT, bg=COLOR_SIDEBAR,
                activebackground=COLOR_SIDEBAR_ACTIVE, activeforeground=COLOR_ACCENT, bd=0, anchor="w",
                padx=15, pady=8, command=lambda p=page_id: self.show_page(p)
            )
            btn.pack(fill="x")
            self.nav_buttons[page_id] = btn

        self.content_area = tk.Frame(self.root, bg=COLOR_BG)
        self.content_area.pack(side="top", fill="both", expand=True)

        self.pages = {}
        for _, page_id in pages:
            frame = tk.Frame(self.content_area, bg=COLOR_BG)
            self.pages[page_id] = frame

        self.build_dashboard_page()
        self.build_failed_page()
        self.build_console_page()
        self.build_settings_page()

        self.build_status_bar()
        self.show_page("dash")

    def show_page(self, page_id):
        for pid, btn in self.nav_buttons.items():
            if pid == page_id:
                btn.config(bg=COLOR_SIDEBAR_ACTIVE, fg=COLOR_ACCENT)
            else:
                btn.config(bg=COLOR_SIDEBAR, fg=COLOR_TEXT)
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[page_id].pack(fill="both", expand=True)

    def build_dashboard_page(self):
        dash = self.pages["dash"]
        
        folder_bar = tk.Frame(dash, bg=COLOR_CARD)
        folder_bar.pack(fill="x", padx=10, pady=8)
        
        btn_select = tk.Button(folder_bar, text="📂 Select Design Folder", font=("Arial", 9, "bold"), bg=COLOR_ACCENT, fg="#000000", command=self.browse_folder)
        btn_select.pack(side="left", padx=10, pady=5)

        self.lbl_folder_path = tk.Label(folder_bar, text="Selected: None", font=("Arial", 9), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_folder_path.pack(side="left", padx=10)

        ext_bar = tk.Frame(dash, bg=COLOR_CARD, bd=1, relief="solid")
        ext_bar.pack(fill="x", padx=10, pady=(0, 8))

        tk.Label(ext_bar, text="🏷 Export File Extension:", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(side="left", padx=10, pady=8)

        self.ext_var = tk.StringVar(value=".eps")
        self.cmb_ext = ttk.Combobox(ext_bar, textvariable=self.ext_var, values=[".eps", ".jpg", ".ai", ".png", ".svg", ".jpeg"], width=10, font=("Arial", 9, "bold"))
        self.cmb_ext.pack(side="left", padx=5)

        tk.Label(ext_bar, text="(Choose or type target extension for CSV filename column)", font=("Arial", 8, "italic"), fg=COLOR_SUBTEXT, bg=COLOR_CARD).pack(side="left", padx=10)

        site_bar = tk.Frame(dash, bg=COLOR_CARD, bd=1, relief="solid")
        site_bar.pack(fill="x", padx=10, pady=(0, 8))

        tk.Label(site_bar, text="🎯 Target Stock Sites:", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(side="left", padx=10, pady=8)

        for site_name in AGENCY_HEADERS.keys():
            var = tk.BooleanVar(value=True)
            self.site_vars[site_name] = var
            chk = tk.Checkbutton(site_bar, text=site_name, variable=var, font=("Arial", 9, "bold"), fg=COLOR_TEXT, bg=COLOR_CARD, selectcolor=COLOR_BG, activebackground=COLOR_CARD, activeforeground=COLOR_ACCENT)
            chk.pack(side="left", padx=15)

        cards_frame = tk.Frame(dash, bg=COLOR_BG)
        cards_frame.pack(fill="x", padx=10, pady=5)

        self.card_labels = {}
        card_definitions = [
            ("Total Images", "0", 0, 0), ("Completed", "0", 0, 1), ("Remaining", "0", 0, 2), ("Failed", "0", 0, 3),
            ("Average QA", "0%", 1, 0), ("API Requests", "0 / 1000", 1, 1), ("Speed", "0.0 img/m", 1, 2), ("Timer", "00:00:00", 1, 3)
        ]

        for title, default_val, r, c in card_definitions:
            c_box = tk.Frame(cards_frame, bg=COLOR_CARD, bd=1, relief="solid")
            c_box.grid(row=r, column=c, padx=5, pady=5, sticky="nsew")
            cards_frame.grid_columnconfigure(c, weight=1)

            tk.Label(c_box, text=title, font=("Arial", 8, "bold"), fg=COLOR_SUBTEXT, bg=COLOR_CARD).pack(anchor="w", padx=10, pady=(5, 0))
            val_lbl = tk.Label(c_box, text=default_val, font=("Arial", 12, "bold"), fg=COLOR_ACCENT if "QA" in title or "Speed" in title else COLOR_TEXT, bg=COLOR_CARD)
            val_lbl.pack(anchor="w", padx=10, pady=(0, 5))
            self.card_labels[title] = val_lbl

        mid_split = tk.Frame(dash, bg=COLOR_BG)
        mid_split.pack(fill="both", expand=True, padx=10, pady=5)

        left_col = tk.Frame(mid_split, bg=COLOR_BG)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 5))

        right_col = tk.Frame(mid_split, bg=COLOR_BG, width=380)
        right_col.pack(side="right", fill="y", padx=(5, 0))
        right_col.pack_propagate(False)

        curr_box = tk.Frame(left_col, bg=COLOR_CARD, bd=1, relief="solid")
        curr_box.pack(fill="x", pady=(0, 5), ipady=5)

        tk.Label(curr_box, text="⚙ CURRENT PROCESSING PANEL", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(anchor="w", padx=10, pady=5)
        
        self.lbl_curr_file = tk.Label(curr_box, text="File: Idle", font=("Arial", 9, "bold"), fg=COLOR_TEXT, bg=COLOR_CARD)
        self.lbl_curr_file.pack(anchor="w", padx=10)
        
        self.lbl_curr_title = tk.Label(curr_box, text="Title: Waiting to start...", font=("Arial", 9), fg=COLOR_SUBTEXT, bg=COLOR_CARD, wraplength=450)
        self.lbl_curr_title.pack(anchor="w", padx=10)

        self.lbl_curr_status = tk.Label(curr_box, text="Status: Standby", font=("Arial", 8, "bold"), fg=COLOR_WARNING, bg=COLOR_CARD)
        self.lbl_curr_status.pack(anchor="w", padx=10, pady=2)

        self.curr_progress = ttk.Progressbar(curr_box, orient="horizontal", mode="determinate")
        self.curr_progress.pack(fill="x", padx=10, pady=5)

        ctrl_frame = tk.Frame(left_col, bg=COLOR_BG)
        ctrl_frame.pack(fill="x", pady=5)

        self.btn_start = tk.Button(ctrl_frame, text="▶ Start Engine", font=("Arial", 9, "bold"), bg=COLOR_SUCCESS, fg="#000", width=15, command=self.start_engine)
        self.btn_start.pack(side="left", padx=5)

        self.btn_pause = tk.Button(ctrl_frame, text="Ⅱ Pause", font=("Arial", 9, "bold"), bg=COLOR_WARNING, fg="#000", width=12, state="disabled", command=self.pause_engine)
        self.btn_pause.pack(side="left", padx=5)

        self.btn_stop = tk.Button(ctrl_frame, text="■ Stop", font=("Arial", 9, "bold"), bg=COLOR_ERROR, fg="#FFF", width=12, state="disabled", command=self.stop_engine)
        self.btn_stop.pack(side="left", padx=5)

        self.btn_clear_window = tk.Button(ctrl_frame, text="🧹 Clear Window", font=("Arial", 9, "bold"), bg=COLOR_SIDEBAR_ACTIVE, fg=COLOR_TEXT, width=14, command=self.clear_processing_window)
        self.btn_clear_window.pack(side="left", padx=5)

        adobe_panel = tk.Frame(left_col, bg=COLOR_CARD, bd=1, relief="solid")
        adobe_panel.pack(fill="both", expand=True, pady=5)

        tk.Label(adobe_panel, text="📌 MULTI-SITE UPLOAD ASSISTANT PANEL", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(anchor="w", padx=10, pady=5)
        
        self.lbl_adobe_eps = tk.Label(adobe_panel, text="Current EPS: N/A", font=("Arial", 8), fg=COLOR_TEXT, bg=COLOR_CARD)
        self.lbl_adobe_eps.pack(anchor="w", padx=10)
        self.lbl_adobe_jpg = tk.Label(adobe_panel, text="Matched JPG: N/A", font=("Arial", 8), fg=COLOR_TEXT, bg=COLOR_CARD)
        self.lbl_adobe_jpg.pack(anchor="w", padx=10)
        self.lbl_adobe_meta = tk.Label(adobe_panel, text="Metadata: NOT READY", font=("Arial", 8, "bold"), fg=COLOR_ERROR, bg=COLOR_CARD)
        self.lbl_adobe_meta.pack(anchor="w", padx=10, pady=2)

        self.lbl_adobe_cat = tk.Label(adobe_panel, text="Category: None", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_adobe_cat.pack(anchor="w", padx=10)
        self.lbl_adobe_pp = tk.Label(adobe_panel, text="People/Property: NO", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_adobe_pp.pack(anchor="w", padx=10)
        self.lbl_adobe_kw = tk.Label(adobe_panel, text="Keywords Count: 0 / 50", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_adobe_kw.pack(anchor="w", padx=10)
        self.lbl_adobe_qa = tk.Label(adobe_panel, text="QA Score: 0%", font=("Arial", 8, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD)
        self.lbl_adobe_qa.pack(anchor="w", padx=10, pady=2)

        prev_box = tk.Frame(right_col, bg=COLOR_CARD, bd=1, relief="solid")
        prev_box.pack(fill="x", pady=(0, 5))

        tk.Label(prev_box, text="🖼 PREVIEW & METADATA", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(anchor="w", padx=10, pady=3)
        self.lbl_large_preview = tk.Label(prev_box, text="No Preview Selected", bg=COLOR_BG, fg=COLOR_SUBTEXT)
        self.lbl_large_preview.pack(padx=10, pady=5)

        self.lbl_meta_eps_ex = tk.Label(prev_box, text="EPS Exists: ❌", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_meta_eps_ex.pack(anchor="w", padx=10)
        self.lbl_meta_jpg_ex = tk.Label(prev_box, text="JPG Exists: ❌", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_meta_jpg_ex.pack(anchor="w", padx=10)
        self.lbl_meta_sha = tk.Label(prev_box, text="SHA256: N/A", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_meta_sha.pack(anchor="w", padx=10)
        self.lbl_meta_res = tk.Label(prev_box, text="Resolution: N/A", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_meta_res.pack(anchor="w", padx=10)
        self.lbl_meta_size = tk.Label(prev_box, text="File Size: N/A", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_meta_size.pack(anchor="w", padx=10, pady=(0, 5))

        api_box = tk.Frame(right_col, bg=COLOR_CARD, bd=1, relief="solid")
        api_box.pack(fill="x", pady=5)

        tk.Label(api_box, text="📡 GEMINI API MONITOR", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_CARD).pack(anchor="w", padx=10, pady=3)
        self.lbl_api_conn = tk.Label(api_box, text="Status: Async Dynamic Queue Active", font=("Arial", 8, "bold"), fg=COLOR_SUCCESS, bg=COLOR_CARD)
        self.lbl_api_conn.pack(anchor="w", padx=10)
        self.lbl_api_today = tk.Label(api_box, text="Total Requests: 0", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_CARD)
        self.lbl_api_today.pack(anchor="w", padx=10, pady=(0, 5))

    def build_failed_page(self):
        page = self.pages["failed"]
        tk.Label(page, text="🔴 FAILED FILES QUEUE & RECOVERY", font=("Arial", 11, "bold"), fg=COLOR_ERROR, bg=COLOR_BG).pack(anchor="w", padx=10, pady=10)

        btn_row = tk.Frame(page, bg=COLOR_BG)
        btn_row.pack(fill="x", padx=10)
        # Feature Fix #23 (Failed Queue "retry later" option): permanently
        # failed jobs are excluded from re-processing so they don't loop
        # forever, but the user needs an explicit way to clear that
        # exclusion and try them again.
        tk.Button(btn_row, text="🔁 Retry All Failed Jobs", command=self.retry_failed_jobs,
                   bg=COLOR_ACCENT, fg="#000000", font=("Arial", 9, "bold"), relief="flat", padx=10, pady=4).pack(side="left", pady=5)

        cols = ("Filename", "Reason / Error Code", "Action")
        self.failed_tree = ttk.Treeview(page, columns=cols, show="headings")
        for c in cols:
            self.failed_tree.heading(c, text=c)
            self.failed_tree.column(c, width=200 if c != "Reason / Error Code" else 400)
        self.failed_tree.pack(fill="both", expand=True, padx=10, pady=5)

    def retry_failed_jobs(self):
        """Feature Fix #23: clears the permanent-failure record (both the
        DynamicQueueEngine's per-folder engine_state.json and failed_queue.csv)
        so those images are picked up again on the next run instead of being
        skipped forever. Does not touch already-completed rows in the site
        CSVs."""
        folder_path = getattr(self, "selected_folder", None)
        if not folder_path or not os.path.isdir(folder_path):
            messagebox.showwarning("No Folder", "Select a valid design folder first.")
            return
        cleared = reset_failed_jobs(folder_path)
        for row in self.failed_tree.get_children():
            self.failed_tree.delete(row)
        self.log(f"Cleared {cleared} permanently-failed job record(s) for retry. Start the batch again to reprocess them.", "success")
        messagebox.showinfo("Retry Queued", f"{cleared} failed job(s) cleared. Run the batch again to retry them.")

    def build_console_page(self):
        page = self.pages["console"]

        # Requirement #1/#2 (Live Console Layout): Request Console on top,
        # Response Console on bottom, with a draggable splitter (sash)
        # between them whose position is saved to config.json and restored
        # on next launch — a vertical tk.PanedWindow gives both for free.
        self.console_pane = tk.PanedWindow(
            page, orient="vertical", bg=COLOR_BG, sashwidth=6, sashrelief="raised",
            bd=0, showhandle=False
        )
        self.console_pane.pack(fill="both", expand=True, padx=5, pady=5)

        # ---- Request Console (top) ----
        req_frame = tk.Frame(self.console_pane, bg=COLOR_BG)
        tk.Label(req_frame, text="📤 REQUEST CONSOLE", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_BG).pack(anchor="w", pady=(0, 3))

        req_text_frame = tk.Frame(req_frame, bg=COLOR_CONSOLE_BG, bd=1, relief="solid")
        req_text_frame.pack(fill="both", expand=True)
        self.request_log_text = tk.Text(req_text_frame, bg=COLOR_CONSOLE_BG, font=("Consolas", 9), wrap="word", state="disabled", fg=COLOR_TEXT, bd=0, highlightthickness=0)
        req_scroll = ttk.Scrollbar(req_text_frame, orient="vertical", command=self.request_log_text.yview)
        self.request_log_text.config(yscrollcommand=req_scroll.set)
        req_scroll.pack(side="right", fill="y")
        self.request_log_text.pack(side="left", fill="both", expand=True)

        self.request_log_text.tag_config("info", foreground=COLOR_TEXT)
        self.request_log_text.tag_config("request_sent", foreground="#4FC3F7")
        self.request_log_text.tag_config("cooldown", foreground=COLOR_ERROR)
        self.request_log_text.tag_config("queue_returned", foreground="#FFA726")
        self.request_log_text.tag_config("warning", foreground=COLOR_WARNING)
        self.request_log_text.tag_config("success", foreground=COLOR_SUCCESS)

        self.console_pane.add(req_frame, minsize=80)

        # ---- Response Console (bottom) ----
        resp_frame = tk.Frame(self.console_pane, bg=COLOR_BG)
        tk.Label(resp_frame, text="📥 RESPONSE CONSOLE", font=("Arial", 9, "bold"), fg=COLOR_ACCENT, bg=COLOR_BG).pack(anchor="w", pady=(0, 3))

        resp_text_frame = tk.Frame(resp_frame, bg=COLOR_CONSOLE_BG, bd=1, relief="solid")
        resp_text_frame.pack(fill="both", expand=True)

        resp_scroll = ttk.Scrollbar(resp_text_frame, orient="vertical", command=lambda *a: self.response_log_text.yview(*a))
        resp_scroll.pack(side="right", fill="y")
        self.response_log_text = tk.Text(resp_text_frame, bg=COLOR_CONSOLE_BG, font=("Consolas", 9), wrap="word", state="disabled", fg=COLOR_TEXT, bd=0, highlightthickness=0)
        self.response_log_text.config(yscrollcommand=resp_scroll.set)
        self.response_log_text.pack(side="left", fill="both", expand=True)

        self.response_log_text.tag_config("info", foreground=COLOR_ACCENT)
        self.response_log_text.tag_config("success", foreground=COLOR_SUCCESS)
        self.response_log_text.tag_config("error", foreground=COLOR_ERROR)
        self.response_log_text.tag_config("warning", foreground=COLOR_WARNING)

        self.console_pane.add(resp_frame, minsize=80)

        # Requirement #2: restore the saved splitter position (deferred
        # until the window has actually laid out and knows its real height).
        saved_pos = CONFIG.get("console_splitter_pos")
        if saved_pos:
            self.root.after(150, lambda: self._safe_sash_place(saved_pos))

        # Requirement #2 (Auto Save): persist the sash position to
        # config.json whenever the user finishes dragging it.
        self.console_pane.bind("<ButtonRelease-1>", self._save_splitter_position)

    def _safe_sash_place(self, pos):
        try:
            self.console_pane.sash_place(0, 1, pos)
        except tk.TclError:
            pass

    def _save_splitter_position(self, event=None):
        try:
            coord = self.console_pane.sash_coord(0)
            if coord:
                CONFIG["console_splitter_pos"] = coord[1]
                save_config(CONFIG)
        except (tk.TclError, IndexError):
            pass

    # Requirement (Single Line Console Logs - blank line grouping): tracks
    # the category of the last line written to each console so a blank line
    # can be inserted exactly where the spec's example shows one — before a
    # category change — and nowhere else. "glue" pairs (an error line
    # immediately followed by its Switch/Retry consequence) are the one
    # exception: no blank line between them even though the category differs.
    _GLUE_PAIRS = {("error", "switch"), ("error", "retry")}

    def _is_scrolled_to_bottom(self, widget):
        try:
            return widget.yview()[1] >= 0.999
        except tk.TclError:
            return True

    def _append_to_console(self, widget, msg, tag, category, last_category_attr):
        # Requirement (Single Line Console Logs): every log is exactly one
        # line — strip any stray newline so nothing can ever wrap into a
        # second line inside the widget.
        msg = msg.replace("\n", " ").rstrip() + "\n"

        last_category = getattr(self, last_category_attr)
        needs_blank = (
            last_category is not None
            and category != last_category
            and (last_category, category) not in self._GLUE_PAIRS
        )
        setattr(self, last_category_attr, category)

        # Requirement #7 (Auto Scroll): only follow new log lines if the
        # user is already at the bottom — if they've scrolled up to read
        # older lines, stay put until they scroll back down themselves.
        was_at_bottom = self._is_scrolled_to_bottom(widget)
        widget.config(state="normal")
        if needs_blank:
            widget.insert("end", "\n")
        widget.insert("end", msg, tag)
        # Requirement #9 (Performance): trim old lines from the widget once
        # over the visible cap.
        line_count = int(widget.index("end-1c").split(".")[0])
        if line_count > self.MAX_VISIBLE_LOG_LINES:
            widget.delete("1.0", f"{line_count - self.MAX_VISIBLE_LOG_LINES}.0")
        if was_at_bottom:
            widget.see("end")
        widget.config(state="disabled")

    # Requirement #9 (Performance): only the most recent MAX_VISIBLE_LOG_LINES
    # lines are kept in each on-screen console — older lines are trimmed from
    # the widget (NOT from the full debug log file, which keeps everything)
    # so the UI never lags even after thousands of log lines.
    MAX_VISIBLE_LOG_LINES = 1000

    def clear_request_console(self):
        """Internal helper — no standalone button (removed per Live Console
        Final Spec item #11), but Clear Window still uses this to wipe the
        visible Request Console as part of a full session reset."""
        self.request_log_text.config(state="normal")
        self.request_log_text.delete("1.0", "end")
        self.request_log_text.config(state="disabled")

    def clear_response_console(self):
        """Internal helper — no standalone button (removed per Live Console
        Final Spec item #11), but Clear Window still uses this to wipe the
        visible Response Console as part of a full session reset."""
        self.response_log_text.config(state="normal")
        self.response_log_text.delete("1.0", "end")
        self.response_log_text.config(state="disabled")

    def log_request(self, message, tag="info", category="info"):
        """Requirement #3: Request Console shows ONLY Folder Loaded,
        Scanning Folder, Engine Started, Total Images Queued, Request Sent,
        429 Rate Limit, API Switched, Retry Started, Engine Finished.
        Everything else must go through plain append_to_file_log() instead
        so it reaches the debug file but never this visible console."""
        append_to_file_log(message)
        timestamp = time.strftime("%H:%M:%S")
        msg = f"[{timestamp}] {message}\n"
        self.root.after(0, lambda: self._append_to_console(self.request_log_text, msg, tag, category, "_last_req_category"))

    @staticmethod
    def _classify_failure_reason(reason: str) -> str:
        r = str(reason).upper()
        if "429" in r:
            return "429"
        if "503" in r or "UNAVAILABLE" in r:
            return "503"
        if "NETWORK" in r or "CONNECTION" in r or "TIMEOUT" in r:
            return "network"
        if "EMPTY_RESPONSE" in r or "EMPTY_TITLE" in r:
            return "empty"
        if "INVALID" in r or "JSON" in r or "CATEGORY" in r:
            return "invalid"
        return "unknown"

    def build_settings_page(self):
        page = self.pages["settings"]
        
        nb = ttk.Notebook(page)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # Requirement #19: final tab structure is General / Gemini API /
        # CSV Header / About — the old "Advanced" tab is gone, its settings
        # (keyword blacklist etc.) now live in General (Requirement #20).
        tab_gen = tk.Frame(nb, bg=COLOR_BG)
        tab_gem = tk.Frame(nb, bg=COLOR_BG)
        tab_csv = tk.Frame(nb, bg=COLOR_BG)
        tab_about = tk.Frame(nb, bg=COLOR_BG)

        nb.add(tab_gen, text="General")
        nb.add(tab_gem, text="Gemini API")
        nb.add(tab_csv, text="CSV Header")
        nb.add(tab_about, text="About")

        # ---- GENERAL TAB ----
        tk.Label(tab_gen, text="Theme Configuration:", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(anchor="w", padx=20, pady=(20, 2))
        self.cfg_theme_var = tk.StringVar(value=CONFIG.get("theme", "Dark"))
        f_theme = tk.Frame(tab_gen, bg=COLOR_BG)
        f_theme.pack(anchor="w", padx=20)
        tk.Radiobutton(f_theme, text="Dark Theme", variable=self.cfg_theme_var, value="Dark", fg=COLOR_TEXT, bg=COLOR_BG, selectcolor=COLOR_CARD, activebackground=COLOR_BG, activeforeground=COLOR_TEXT).pack(side="left", padx=(0, 15))
        tk.Radiobutton(f_theme, text="Light Theme", variable=self.cfg_theme_var, value="Light", fg=COLOR_TEXT, bg=COLOR_BG, selectcolor=COLOR_CARD, activebackground=COLOR_BG, activeforeground=COLOR_TEXT).pack(side="left")

        # Vecteezy License column value: Pro / Free / Editorial — applies to
        # every design in the batch since it's a contributor-level setting.
        tk.Label(tab_gen, text="Vecteezy License (new 'License' CSV column):", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(anchor="w", padx=20, pady=(20, 2))
        self.cfg_vecteezy_license_var = tk.StringVar(value=CONFIG.get("vecteezy_license", "Pro"))
        f_license = tk.Frame(tab_gen, bg=COLOR_BG)
        f_license.pack(anchor="w", padx=20)
        for opt in ("Pro", "Free", "Editorial"):
            tk.Radiobutton(f_license, text=opt, variable=self.cfg_vecteezy_license_var, value=opt, fg=COLOR_TEXT, bg=COLOR_BG, selectcolor=COLOR_CARD, activebackground=COLOR_BG, activeforeground=COLOR_TEXT).pack(side="left", padx=(0, 15))

        # Requirement #20: Keyword Blacklist moved here from the old
        # Advanced tab — no existing setting/feature was dropped.
        tk.Label(tab_gen, text="Keyword Blacklist (Comma separated):", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(anchor="w", padx=20, pady=(20, 2))
        self.cfg_blacklist = tk.Text(tab_gen, bg=COLOR_CARD, fg=COLOR_TEXT, height=5, width=60, insertbackground=COLOR_TEXT)
        self.cfg_blacklist.insert("1.0", ", ".join(CONFIG.get("keyword_blacklist", [])))
        self.cfg_blacklist.pack(anchor="w", padx=20, pady=(0, 15))

        # ---- GEMINI API TAB ----
        tk.Label(tab_gem, text="Model Version (pick from the list or type a custom model name):", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(anchor="w", padx=20, pady=(15, 2))
        self.cfg_model_var = tk.StringVar(value=CONFIG.get("model", "gemini-2.5-flash"))
        
        model_options = [
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3.1-pro",
            "gemini-3.1-flash",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.7-flash"
        ]

        # Requirement #18 (Gemini Model — Dynamic Custom Input): the default
        # list is preserved untouched, but the combobox is now editable
        # (state="normal" instead of "readonly") so the user can type any
        # future model name — no source code change ever needed for new models.
        ttk.Combobox(
            tab_gem, 
            textvariable=self.cfg_model_var, 
            values=model_options, 
            state="normal", width=35
        ).pack(anchor="w", padx=20)
        tk.Label(tab_gem, text="Custom names are saved as-is and used directly by the engine.", fg=COLOR_SUBTEXT, bg=COLOR_BG, font=("Arial", 8)).pack(anchor="w", padx=20, pady=(2, 0))

        # API Keys Management Section
        tk.Label(tab_gem, text="API Key Management:", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(anchor="w", padx=20, pady=(20, 5))
        
        btn_key_frame = tk.Frame(tab_gem, bg=COLOR_BG)
        btn_key_frame.pack(anchor="w", padx=20, pady=(0, 5))
        
        tk.Button(btn_key_frame, text="➕ New API Key", bg=COLOR_ACCENT, fg="#000", font=("Arial", 8, "bold"), command=self.add_api_key_dialog).pack(side="left", padx=(0, 5))
        tk.Button(btn_key_frame, text="✏ Edit Selected", bg=COLOR_CARD, fg=COLOR_TEXT, font=("Arial", 8), command=self.edit_api_key_dialog).pack(side="left", padx=5)
        tk.Button(btn_key_frame, text="🗑 Delete Selected", bg=COLOR_ERROR, fg="#FFF", font=("Arial", 8), command=self.delete_api_key).pack(side="left", padx=5)

        cols_api = ("Name", "API Key", "Status")
        self.api_tree = ttk.Treeview(tab_gem, columns=cols_api, show="headings", height=6)
        self.api_tree.heading("Name", text="Name")
        self.api_tree.heading("API Key", text="API Key")
        self.api_tree.heading("Status", text="Status")
        self.api_tree.column("Name", width=120)
        self.api_tree.column("API Key", width=250)
        self.api_tree.column("Status", width=100)
        self.api_tree.pack(anchor="w", padx=20, fill="x")

        self.api_keys_data = list(CONFIG.get("api_keys", []))
        self.refresh_api_tree()

        # ---- CSV HEADER TAB (Requirement #17) ----
        tk.Label(tab_csv, text="CSV Column Headers (per site):", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(anchor="w", padx=20, pady=(15, 2))
        tk.Label(
            tab_csv,
            text="Sites cannot be added or removed. Columns can be renamed, added, deleted, and reordered.\nEmpty or duplicate column names are not allowed.",
            fg=COLOR_SUBTEXT, bg=COLOR_BG, font=("Arial", 8), justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 10))

        # Working copy — only committed to CONFIG on "Save All Settings",
        # exactly like every other setting on this page.
        self.csv_headers_data = {site: list(cols) for site, cols in get_csv_headers().items()}

        site_bar = tk.Frame(tab_csv, bg=COLOR_BG)
        site_bar.pack(anchor="w", padx=20, pady=(0, 10))
        tk.Label(site_bar, text="Site:", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 9, "bold")).pack(side="left", padx=(0, 8))
        self.csv_header_site_var = tk.StringVar(value="Adobe Stock")
        site_combo = ttk.Combobox(site_bar, textvariable=self.csv_header_site_var, values=["Adobe Stock", "Shutterstock", "Vecteezy"], state="readonly", width=20)
        site_combo.pack(side="left")
        site_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_csv_header_listbox())

        csv_body = tk.Frame(tab_csv, bg=COLOR_BG)
        csv_body.pack(anchor="w", padx=20, fill="both", expand=True)

        self.csv_header_listbox = tk.Listbox(csv_body, bg=COLOR_CARD, fg=COLOR_TEXT, height=10, width=40, selectbackground=COLOR_ACCENT, selectforeground="#000000", exportselection=False)
        self.csv_header_listbox.pack(side="left", fill="y")

        btn_col = tk.Frame(csv_body, bg=COLOR_BG)
        btn_col.pack(side="left", padx=15, fill="y")

        tk.Button(btn_col, text="➕ Add Column", bg=COLOR_ACCENT, fg="#000", font=("Arial", 8, "bold"), command=self.add_csv_header_column, width=16).pack(pady=3)
        tk.Button(btn_col, text="✏ Rename Selected", bg=COLOR_CARD, fg=COLOR_TEXT, font=("Arial", 8), command=self.rename_csv_header_column, width=16).pack(pady=3)
        tk.Button(btn_col, text="🗑 Delete Selected", bg=COLOR_ERROR, fg="#FFF", font=("Arial", 8), command=self.delete_csv_header_column, width=16).pack(pady=3)
        tk.Button(btn_col, text="⬆ Move Up", bg=COLOR_CARD, fg=COLOR_TEXT, font=("Arial", 8), command=lambda: self.move_csv_header_column(-1), width=16).pack(pady=3)
        tk.Button(btn_col, text="⬇ Move Down", bg=COLOR_CARD, fg=COLOR_TEXT, font=("Arial", 8), command=lambda: self.move_csv_header_column(1), width=16).pack(pady=3)
        tk.Button(btn_col, text="↺ Reset to Default", bg=COLOR_WARNING, fg="#000", font=("Arial", 8), command=self.reset_csv_header_column, width=16).pack(pady=(15, 3))

        self.refresh_csv_header_listbox()

        # ---- ABOUT TAB (Requirement #21) ----
        tk.Label(tab_about, text="⚡ Jinnah Metadata Generator", fg=COLOR_ACCENT, bg=COLOR_BG, font=("Arial", 14, "bold")).pack(anchor="w", padx=20, pady=(25, 2))
        tk.Label(tab_about, text="Multi-Site Stock Metadata Automation Suite", fg=COLOR_TEXT, bg=COLOR_BG, font=("Arial", 10)).pack(anchor="w", padx=20, pady=(0, 15))

        info_frame = tk.Frame(tab_about, bg=COLOR_CARD)
        info_frame.pack(anchor="w", padx=20, pady=5, fill="x")
        about_rows = [
            ("Software:", "Jinnah Metadata Generator"),
            ("Developer:", "MD ALI JINNAH"),
            ("Supported Sites:", "Adobe Stock, Shutterstock, Vecteezy"),
            ("Engine:", "Dynamic Multi-API Queue with Rate-Limit-Aware Scheduling"),
        ]
        for i, (label, value) in enumerate(about_rows):
            tk.Label(info_frame, text=label, fg=COLOR_SUBTEXT, bg=COLOR_CARD, font=("Arial", 9, "bold"), anchor="w", width=14).grid(row=i, column=0, sticky="w", padx=15, pady=6)
            tk.Label(info_frame, text=value, fg=COLOR_TEXT, bg=COLOR_CARD, font=("Arial", 9), anchor="w", wraplength=420, justify="left").grid(row=i, column=1, sticky="w", padx=5, pady=6)

        tk.Label(
            tab_about,
            text="Designed for high-speed microstock tagging, quality assurance, and dynamic multi-key processing.",
            fg=COLOR_SUBTEXT, bg=COLOR_BG, wraplength=520, justify="left"
        ).pack(anchor="w", padx=20, pady=(15, 5))

        btn_save = tk.Button(page, text="💾 Save All Settings", bg=COLOR_SUCCESS, fg="#000", font=("Arial", 10, "bold"), padx=20, pady=5, command=self.save_settings_action)
        btn_save.pack(anchor="e", padx=20, pady=10)

    def refresh_csv_header_listbox(self):
        self.csv_header_listbox.delete(0, tk.END)
        site = self.csv_header_site_var.get()
        for h in self.csv_headers_data.get(site, []):
            self.csv_header_listbox.insert(tk.END, h)

    def add_csv_header_column(self):
        site = self.csv_header_site_var.get()
        name = simpledialog.askstring("New Column", f"Enter new column name for {site}:", parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("Invalid", "Column name cannot be empty.")
            return
        if any(c.strip().lower() == name.lower() for c in self.csv_headers_data[site]):
            messagebox.showwarning("Duplicate", f"'{name}' already exists in {site}.")
            return
        self.csv_headers_data[site].append(name)
        self.refresh_csv_header_listbox()

    def rename_csv_header_column(self):
        site = self.csv_header_site_var.get()
        sel = self.csv_header_listbox.curselection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a column to rename.")
            return
        idx = sel[0]
        old_name = self.csv_headers_data[site][idx]
        new_name = simpledialog.askstring("Rename Column", f"Rename '{old_name}' to:", initialvalue=old_name, parent=self.root)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name:
            messagebox.showwarning("Invalid", "Column name cannot be empty.")
            return
        if any(i != idx and c.strip().lower() == new_name.lower() for i, c in enumerate(self.csv_headers_data[site])):
            messagebox.showwarning("Duplicate", f"'{new_name}' already exists in {site}.")
            return
        self.csv_headers_data[site][idx] = new_name
        self.refresh_csv_header_listbox()
        self.csv_header_listbox.selection_set(idx)

    def delete_csv_header_column(self):
        site = self.csv_header_site_var.get()
        sel = self.csv_header_listbox.curselection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a column to delete.")
            return
        if len(self.csv_headers_data[site]) <= 1:
            messagebox.showwarning("Not Allowed", f"{site} must keep at least one column.")
            return
        idx = sel[0]
        del self.csv_headers_data[site][idx]
        self.refresh_csv_header_listbox()

    def move_csv_header_column(self, direction):
        site = self.csv_header_site_var.get()
        sel = self.csv_header_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        new_idx = idx + direction
        cols = self.csv_headers_data[site]
        if 0 <= new_idx < len(cols):
            cols[idx], cols[new_idx] = cols[new_idx], cols[idx]
            self.refresh_csv_header_listbox()
            self.csv_header_listbox.selection_set(new_idx)

    def reset_csv_header_column(self):
        site = self.csv_header_site_var.get()
        if messagebox.askyesno("Reset", f"Reset {site} headers to default?"):
            self.csv_headers_data[site] = list(_engine_module.AGENCY_HEADERS[site])
            self.refresh_csv_header_listbox()

    def refresh_api_tree(self):
        for item in self.api_tree.get_children():
            self.api_tree.delete(item)
        for api in self.api_keys_data:
            key_masked = api["key"][:4] + "********" if len(api["key"]) > 4 else "********"
            self.api_tree.insert("", "end", values=(api["name"], key_masked, api["status"]))

    def _is_duplicate_api_key(self, key: str, exclude_idx: int = -1) -> bool:
        """Requirement #4 (Duplicate API Key Bug): the same API key must not
        be addable twice. Comparison strips leading/trailing whitespace so
        'ABC123 ' and 'ABC123' are correctly treated as the same key."""
        key_clean = key.strip()
        for i, api in enumerate(self.api_keys_data):
            if i == exclude_idx:
                continue
            if api.get("key", "").strip() == key_clean:
                return True
        return False

    def add_api_key_dialog(self):
        dlg = APIKeyDialog(self.root, title="New API Key")
        if dlg.result:
            if self._is_duplicate_api_key(dlg.result["key"]):
                messagebox.showwarning(
                    "Duplicate API Key",
                    "This API key is already in your list. Duplicate keys are not allowed."
                )
                return
            self.api_keys_data.append(dlg.result)
            self.refresh_api_tree()

    def edit_api_key_dialog(self):
        sel = self.api_tree.selection()
        if not sel:
            messagebox.showwarning("Warning", "Select an API key to edit!")
            return
        idx = self.api_tree.index(sel[0])
        item = self.api_keys_data[idx]
        dlg = APIKeyDialog(self.root, title="Edit API Key", item_data=item)
        if dlg.result:
            if self._is_duplicate_api_key(dlg.result["key"], exclude_idx=idx):
                messagebox.showwarning(
                    "Duplicate API Key",
                    "This API key is already used by another entry. Duplicate keys are not allowed."
                )
                return
            self.api_keys_data[idx] = dlg.result
            self.refresh_api_tree()

    def delete_api_key(self):
        sel = self.api_tree.selection()
        if not sel:
            messagebox.showwarning("Warning", "Select an API key to delete!")
            return
        idx = self.api_tree.index(sel[0])
        del self.api_keys_data[idx]
        self.refresh_api_tree()

    def save_settings_action(self):
        try:
            old_theme = CONFIG.get("theme", "Dark")
            new_theme = self.cfg_theme_var.get()
            requires_restart = old_theme != new_theme
            
            CONFIG["theme"] = new_theme
            CONFIG["vecteezy_license"] = self.cfg_vecteezy_license_var.get().strip() or "Pro"
            CONFIG["model"] = self.cfg_model_var.get().strip()
            CONFIG["api_keys"] = self.api_keys_data

            # Requirement #17: validate before persisting — reject empty
            # headers, duplicate headers, or any attempt to add/remove a
            # site, and abort the ENTIRE save (not just this field) so a
            # bad CSV Header edit can't silently corrupt other settings too.
            ok, reason = validate_csv_headers(self.csv_headers_data)
            if not ok:
                messagebox.showerror("Invalid CSV Headers", reason)
                return
            CONFIG["csv_headers"] = self.csv_headers_data

            raw_bl = self.cfg_blacklist.get("1.0", tk.END).strip()
            CONFIG["keyword_blacklist"] = [b.strip().lower() for b in raw_bl.split(",") if b.strip()]

            save_config(CONFIG)
            
            if requires_restart:
                messagebox.showinfo("Theme Changed", "Settings saved successfully!\n\nPlease restart the application to apply Theme changes.")
            else:
                messagebox.showinfo("Success", "All engine settings successfully saved!")
                
            self.log("System settings updated successfully.", "success")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save settings: {str(e)}")

    def build_status_bar(self):
        self.status_bar = tk.Frame(self.root, bg=COLOR_SIDEBAR, height=25)
        self.status_bar.pack(side="bottom", fill="x")

        self.lbl_stat_sys = tk.Label(self.status_bar, text="CPU: 0% | RAM: 0%", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_SIDEBAR)
        self.lbl_stat_sys.pack(side="left", padx=10)

        self.lbl_stat_net = tk.Label(self.status_bar, text="Internet: Connected", font=("Arial", 8), fg=COLOR_SUCCESS, bg=COLOR_SIDEBAR)
        self.lbl_stat_net.pack(side="left", padx=10)

        self.lbl_stat_csv = tk.Label(self.status_bar, text="CSV Engine: Multi-Site Active", font=("Arial", 8), fg=COLOR_SUBTEXT, bg=COLOR_SIDEBAR)
        self.lbl_stat_csv.pack(side="right", padx=10)

    def start_system_monitor(self):
        def monitor_loop():
            while True:
                if psutil:
                    cpu = psutil.cpu_percent()
                    ram = psutil.virtual_memory().percent
                    sys_str = f"CPU: {cpu}% | RAM: {ram}%"
                else:
                    sys_str = "CPU: --% | RAM: --%"
                self.root.after(0, lambda: self.lbl_stat_sys.config(text=sys_str))
                time.sleep(3)
        Thread(target=monitor_loop, daemon=True).start()

    def log(self, message, tag="info", category="info"):
        """Response Console: every outcome is logged in one unified
        'Response Received | filename | STATUS' format (Requirement: single
        line, API name always shown, same style as Request Console). Also
        used for general non-processing status messages."""
        append_to_file_log(message)
        timestamp = time.strftime("%H:%M:%S")
        msg = f"[{timestamp}] {message}\n"
        self.root.after(0, lambda: self._append_to_console(self.response_log_text, msg, tag, category, "_last_resp_category"))

    def browse_folder(self):
        folder = filedialog.askdirectory(title="Select Design Folder")
        if folder:
            self.selected_folder = folder
            # Bug Fix (Split engine.log Location): set_active_log_folder()
            # used to only be called once "Start Engine" was pressed (inside
            # run_dynamic_workflow). That meant this very first "Folder
            # Loaded" line was written to engine.log next to the app itself
            # (or wherever the process happened to be run from), while every
            # later line for the same session went to a DIFFERENT
            # engine.log inside the selected design folder — so the app's
            # own engine.log ended up with just this one line and looked
            # broken/empty, while the real activity silently landed
            # somewhere else. Pointing logging at the folder immediately on
            # selection keeps the whole session in one file.
            _engine_module.set_active_log_folder(folder)
            self.lbl_folder_path.config(text=f"Selected: {os.path.basename(folder)}")
            self.log_request(f"Folder Loaded | {folder}", "info", category="lifecycle")

    def start_engine(self):
        if not self.selected_folder:
            messagebox.showwarning("Notice", "Select a design folder first!")
            return
            
        selected_sites = [site for site, var in self.site_vars.items() if var.get()]
        if not selected_sites:
            messagebox.showwarning("Notice", "Please select at least one stock site!")
            return

        enabled_keys = [k for k in CONFIG.get("api_keys", []) if k.get("status") == "Enable" and k.get("key")]
        if not enabled_keys:
            messagebox.showwarning("Notice", "No Enabled API Keys found! Please add/enable API Keys in Settings.")
            return

        # Bug Fix (Duplicate Concurrent Runs): Stop/Clear Window re-enable
        # this button immediately, but the previous run_dynamic_workflow
        # thread may still be alive (draining in-flight requests). Starting
        # a new one on top of it would process the same folder twice at
        # once. Refuse instead of silently double-running.
        if self._workflow_thread is not None and self._workflow_thread.is_alive():
            messagebox.showwarning(
                "Notice",
                "The previous batch is still finishing up in the background. "
                "Please wait a moment for it to fully stop before starting a new one."
            )
            return

        self.is_running = True
        self.is_paused = False
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.btn_stop.config(state="normal")

        self._workflow_thread = Thread(target=self.run_dynamic_workflow, args=(self.selected_folder, selected_sites, enabled_keys), daemon=True)
        self._workflow_thread.start()

    def pause_engine(self):
        self.is_paused = not self.is_paused
        self.btn_pause.config(text="▶ Resume" if self.is_paused else "Ⅱ Pause")

    def stop_engine(self):
        self.is_running = False
        self.btn_start.config(state="normal")
        self.btn_pause.config(state="disabled")
        self.btn_stop.config(state="disabled")

    def clear_processing_window(self):
        """Requirement #13-16: 'Clear Window' resets the current processing
        session only — dashboard stats, current-file panel, preview/
        metadata panel, API monitor, and both consoles all go back to their
        idle/standby defaults. It does NOT touch the selected folder, export
        extension, target sites, settings, API keys, theme, or config.json —
        those are left completely alone.
        """
        if self.is_running:
            confirmed = messagebox.askyesno(
                "Clear Window",
                "Clear Current Processing Window?\n\n"
                "All current processing progress, runtime data and dashboard "
                "information will be cleared.",
            )
            if not confirmed:
                return
            # Requirement #16 (If Yes): Stop Engine + Stop Workers. In-flight
            # worker threads check self.is_running on their next loop tick
            # and exit on their own — the same graceful stop stop_engine()
            # already relies on, just triggered here too.
            self.is_running = False

        self.btn_start.config(state="normal")
        self.btn_pause.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.is_paused = False
        self.btn_pause.config(text="Ⅱ Pause")

        # Requirement #16: Clear Queue + Reset Runtime. There's no separate
        # persistent in-memory queue object outside the run_dynamic_workflow
        # closure to clear directly — it's abandoned along with the stopped
        # worker threads — so "reset runtime" here means the visible/tracked
        # state below, not a live data structure.
        self.api_call_count = 0

        # Requirement #14: Dashboard Statistics -> defaults.
        self.card_labels["Total Images"].config(text="0")
        self.card_labels["Completed"].config(text="0")
        self.card_labels["Remaining"].config(text="0")
        self.card_labels["Failed"].config(text="0")
        self.card_labels["Average QA"].config(text="0%")
        self.card_labels["API Requests"].config(text="0 / 1000")
        self.card_labels["Speed"].config(text="0.0 img/m")
        self.card_labels["Timer"].config(text="00:00:00")
        self.curr_progress.config(value=0)

        # Requirement #14: Current Processing Panel -> Standby.
        self.lbl_curr_file.config(text="File: Idle")
        self.lbl_curr_title.config(text="Title: Waiting to start...")
        self.lbl_curr_status.config(text="Status: Standby")

        # Requirement #14: Preview & Metadata Panel -> No Preview Selected.
        self.lbl_large_preview.config(image="", text="No Preview Selected")
        self.preview_img = None
        self.lbl_meta_eps_ex.config(text="EPS Exists: ❌")
        self.lbl_meta_jpg_ex.config(text="JPG Exists: ❌")
        self.lbl_meta_sha.config(text="SHA256: N/A")
        self.lbl_meta_res.config(text="Resolution: N/A")
        self.lbl_meta_size.config(text="File Size: N/A")

        # Requirement #14: Multi-Site Upload Assistant Panel -> defaults.
        self.lbl_adobe_eps.config(text="Current EPS: N/A")
        self.lbl_adobe_jpg.config(text="Matched JPG: N/A")
        self.lbl_adobe_meta.config(text="Metadata: NOT READY", fg=COLOR_ERROR)
        self.lbl_adobe_cat.config(text="Category: None")
        self.lbl_adobe_pp.config(text="People/Property: NO")
        self.lbl_adobe_kw.config(text="Keywords Count: 0 / 50")
        self.lbl_adobe_qa.config(text="QA Score: 0%")

        # Requirement #14: Gemini API Monitor -> Idle.
        self.lbl_api_conn.config(text="Status: Idle", fg=COLOR_WARNING)
        self.lbl_api_today.config(text="Total Requests: 0")

        # Requirement #14: Failed Queue (visual/session list only — the
        # persisted failed_queue.csv on disk is untouched, matching
        # Requirement #15's "don't reset config/persistent data" intent).
        for row in self.failed_tree.get_children():
            self.failed_tree.delete(row)

        # Reverted per user request: Clear Window goes back to clearing both
        # Live Console panels too, as part of a full session reset — only
        # settings/folder/API keys/theme/config.json stay untouched. The
        # standalone per-console Clear buttons remain removed (not re-added
        # as separate UI buttons); this clears them internally instead.
        self.clear_request_console()
        self.clear_response_console()

        self.log("Processing window cleared. Ready for a new run.", "info")

    def _prepare_preview_data(self, jpg_path, eps_path):
        """Bug Fix (UI Freeze on Start): the old update_large_preview() did
        ALL of its work — opening the full image, resizing it, hashing the
        entire file for the SHA256 display — inside a callback scheduled
        with root.after(0, ...), meaning it ran directly on the Tk main
        thread. The dispatcher loop schedules one of these per image with
        only a 0.05s stagger, so for a 25-image batch, 25 full file reads +
        hashes + image decodes queued up back-to-back on the ONE thread that
        also has to keep the window responsive — freezing/lagging the whole
        app for as long as that took to churn through.

        This half does all of that heavy, blocking work (file I/O, PIL
        decode/resize, hashing) and is called directly from the background
        dispatcher thread — never on the main thread — so it never blocks
        the UI. It hands back a plain dict of already-computed values for
        _apply_preview_data() to render cheaply."""
        try:
            with Image.open(jpg_path) as img:
                res = f"{img.width}x{img.height}"
                thumb = img.copy()
                thumb.thumbnail((320, 220))
            return {
                "thumb": thumb,
                "res": res,
                "fsize": f"{os.path.getsize(jpg_path) / (1024 * 1024):.2f} MB",
                "sha": calculate_sha256(jpg_path)[:12] + "...",
                "eps_exists": os.path.exists(eps_path),
                "error": None,
            }
        except Exception as e:
            # Requirement #30: preview must never depend on EPS, and must
            # never crash the UI — but a swallowed exception here (e.g. a
            # genuinely corrupt image slipping past preflight) shouldn't
            # vanish silently either.
            return {"error": str(e), "filename": os.path.basename(jpg_path)}

    def _apply_preview_data(self, data):
        """Cheap, main-thread-only half of preview rendering. Only turns an
        already-decoded/resized PIL thumbnail into a Tk PhotoImage and
        updates labels — no file I/O, no hashing — so it's safe to run via
        root.after(0, ...) without lagging the UI."""
        if data.get("error"):
            self.log(f"Preview render failed for {data.get('filename', '?')}: {data['error']}", "warning")
            return
        self.preview_img = ImageTk.PhotoImage(data["thumb"])
        self.lbl_large_preview.config(image=self.preview_img, text="")
        self.lbl_meta_eps_ex.config(text=f"EPS Exists: {'✅' if data['eps_exists'] else '❌'}")
        self.lbl_meta_jpg_ex.config(text="JPG Exists: ✅")
        self.lbl_meta_sha.config(text=f"SHA256: {data['sha']}")
        self.lbl_meta_res.config(text=f"Resolution: {data['res']}")
        self.lbl_meta_size.config(text=f"File Size: {data['fsize']}")

    # =========================================================================
    # ASYNC RATE-LIMIT DISPATCHER & ORDERED RESULT WORKFLOW
    # =========================================================================
    def run_dynamic_workflow(self, folder_path, target_sites, enabled_api_keys):
        # Bug Fix (Logging consistency): route engine.log into this design
        # folder, same as engine_state.json / failed_queue.csv / etc.
        _engine_module.set_active_log_folder(folder_path)

        target_ext = self.ext_var.get().strip()
        if target_ext and not target_ext.startswith("."):
            target_ext = "." + target_ext

        self.log_request("Scanning Folder...", "info", category="lifecycle")
        processed_files = get_processed_files_from_folder(folder_path)

        # Bug Fix: this scan only looked for .jpg/.jpeg, silently ignoring any
        # .png designs even though the rest of the pipeline (engine.py) treats
        # .png as a valid input extension.
        # Bug Fix (Logic): plain sorted() lexicographically orders filenames
        # (1, 10, 11, 2, 20, 3, ...) instead of numerically. This never
        # affected *correctness* of the final CSV (rows are always placed by
        # embedded filename number, never by processing order), but it made
        # progress/log/dispatch order confusing. Sort numerically to match
        # engine.py's DynamicQueueEngine behavior.
        all_jpg_files = sorted(
            [f for f in os.listdir(folder_path) if f.lower().endswith(('.jpg', '.jpeg', '.png'))],
            key=lambda f: (extract_design_number(f) is None, extract_design_number(f) or 0, f)
        )
        
        # 1. PERMANENT INDEXING BEFORE REQUEST DISPATCH
        indexed_items = []
        permanent_index = 1

        for file in all_jpg_files:
            base_filename = os.path.splitext(file)[0]
            csv_export_filename = f"{base_filename}{target_ext}"

            if file.lower() in processed_files or base_filename.lower() in processed_files or csv_export_filename.lower() in processed_files:
                self.log(f"Skipping (Already in CSV): {file}", "warning")
                continue

            jpg_path = os.path.join(folder_path, file)

            # Preflight Checker: reject corrupt/unreadable images up front
            # instead of burning a paid API call discovering it later.
            is_valid, reason = validate_image_file(jpg_path)
            if not is_valid:
                self.log(f"Preflight rejected {file}: {reason}", "error")
                save_failed_queue_csv(folder_path, [(csv_export_filename, reason)])
                self.root.after(0, lambda f=csv_export_filename, s=reason: self.failed_tree.insert("", "end", values=(f, s, "Retry")))
                continue

            # Bug Fix #21: detect (not block on) a missing companion EPS file.
            if not check_eps_exists(folder_path, file):
                report_missing_eps(folder_path, file)

            indexed_items.append({
                "permanent_index": permanent_index, # Permanent Index Allocated
                "filename": file,
                "csv_filename": csv_export_filename,
                "jpg_path": jpg_path,
                "eps_path": os.path.join(folder_path, base_filename + ".eps")
            })
            permanent_index += 1

        total_images = len(indexed_items)
        grand_total = len(all_jpg_files)
        # Requirement (crash/resume visibility): files already recorded in
        # the CSVs from a previous run (skipped above) count as already
        # completed for progress-reporting purposes.
        already_done_before_this_run = grand_total - total_images

        if total_images == 0:
            self.log("No new images to process!", "success")
            self.stop_engine()
            return

        self.log_request("Engine Started", "info", category="lifecycle")
        self.log_request(f"{total_images} Images Queued", "info", category="lifecycle")
        if already_done_before_this_run > 0:
            self.log(f"Resuming: {already_done_before_this_run}/{grand_total} design(s) were already completed in a previous run.", "info")

        update_progress_status(
            folder_path,
            total_images=grand_total,
            completed=already_done_before_this_run,
            failed=0,
            remaining=total_images,
            last_processed_design_number=None,
            last_processed_filename=None,
            status="IN_PROGRESS"
        )

        # Bug Fix (CRITICAL — Requirements #2, #32, #33 — Job Completed Rule):
        # a single shared ThreadSafeCSVManager is created up front and each
        # item's design number (from its filename, NOT dispatch/response
        # order — Requirement #3) is resolved now. Previously this workflow
        # collected every result in RAM and wrote the CSV ONCE at the very
        # end via save_batch_sorted_csv — meaning "Completed" was reported
        # the instant Gemini responded, with the actual disk write (and any
        # possible failure of it) invisible until minutes later. Now each
        # item's row is written AND verified on disk immediately, and only
        # that verified write is allowed to count as "completed".
        csv_manager = ThreadSafeCSVManager(folder_path, [it["filename"] for it in indexed_items], target_sites=target_sites)
        for it in indexed_items:
            it["design_number"] = get_design_number_for_file(folder_path, it["filename"], csv_manager)

        failed_items = []
        stats_lock = Lock()

        completed_count = 0
        failed_count = 0
        qa_sum = 0
        start_time = time.time()

        # Bug Fix: previously each request just round-robined the raw key
        # list with zero awareness of per-key rate limits or 429 cooldowns,
        # and generate_metadata_single_key's "retries" argument was accepted
        # but never actually used to retry anything — a single transient
        # error permanently failed the image. DynamicAPIManager (the same
        # class the queue engine uses) now provides rate-limit-aware key
        # selection + 429 cooldown tracking, and the retry loop below
        # actually retries with a capped exponential backoff.
        api_manager = DynamicAPIManager(
            CONFIG.get("api_keys", []),
            CONFIG.get("max_requests_per_minute", 10)
        )
        max_retries = CONFIG.get("max_retries", 4)

        # Requirement #6/#7 (Scheduler Switch / Waiting State logging): with
        # many worker threads calling get_available_api() concurrently, this
        # shared, lock-protected tracker lets the Request Console log
        # rotation/wait transitions exactly once — not once per thread —
        # avoiding duplicate spam across 25 concurrent workers.
        rotation_lock = Lock()
        rotation_state = {"active_name": None, "waiting_logged": False}

        def update_dashboard():
            # Bug Fix (Requirement: replace ETA with a running Timer): ETA
            # was a rough estimate based on average speed, which is often
            # inaccurate and confusing. The dashboard now shows a live
            # elapsed-time Timer instead (see tick_timer below) — this
            # function only updates the count/speed cards.
            elapsed = time.time() - start_time
            done_total = completed_count + failed_count
            speed = (done_total / elapsed) * 60 if elapsed > 0 else 0
            avg_qa = int(qa_sum / max(1, completed_count))
            self.root.after(0, lambda c=completed_count, f=failed_count, rem=total_images-done_total, q=avg_qa, s=speed, p=int((done_total/total_images)*100): [
                self.card_labels["Total Images"].config(text=str(total_images)),
                self.card_labels["Completed"].config(text=str(c)),
                self.card_labels["Remaining"].config(text=str(rem)),
                self.card_labels["Failed"].config(text=str(f)),
                self.card_labels["Average QA"].config(text=f"{q}%"),
                self.card_labels["Speed"].config(text=f"{s:.1f} img/m"),
                self.curr_progress.config(value=p)
            ])

        def tick_timer():
            # Requirement: a live Timer that counts UP from process start
            # (e.g. started at 4:00, keeps counting) until the whole batch
            # finishes — replacing the old estimated-time-remaining ETA.
            if not self.is_running:
                return
            elapsed = int(time.time() - start_time)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            self.card_labels["Timer"].config(text=f"{h:02d}:{m:02d}:{s:02d}")
            self.root.after(1000, tick_timer)

        self.root.after(0, tick_timer)

        # 2. ASYNCHRONOUS RESPONSE WORKER (CONSUMER)
        def async_response_handler(item):
            nonlocal completed_count, failed_count, qa_sum
            try:
                resolved = False  # guards against double-counting in the except handler below
                genuine_error_attempts = 0  # only genuine (non-429) errors ever count toward max_retries
                last_status = "UNKNOWN_ERROR"
                meta_data = None
                api_name = "API"

                # Bug Fix (Live Console Final Spec, Item #5): "Processing File"
                # and any Index/Request-Index number are debug-file-only now —
                # never shown in either visible console.
                append_to_file_log(f"Processing File #{item['permanent_index']}: {item['filename']}")

                # ---- Phase 1: get metadata from Gemini. Single active API +
                # queue-reinsert on 429 (Requirement #2/#3/#4) — 429 NEVER counts
                # toward max_retries/permanent failure and has no retry counter
                # or backoff of its own; only genuine (non-429) errors use the
                # capped exponential backoff below. ----
                while True:
                    api_key, req_id = None, None
                    while api_key is None:
                        if not self.is_running:
                            return
                        api_key, req_id = api_manager.get_available_api()
                        if api_key is None:
                            # Bug Fix (Item #5): "Waiting for Response"/wait-state
                            # detail is debug-file-only now, not a visible console
                            # category in the Final spec's Request Console list.
                            with rotation_lock:
                                if not rotation_state["waiting_logged"]:
                                    append_to_file_log("All APIs are currently in cooldown")
                                    append_to_file_log("Waiting for available API...")
                                    rotation_state["waiting_logged"] = True
                            time.sleep(0.3)
                        else:
                            # Bug Fix (Item #5): "API READY"/"Returned To
                            # Available API Pool"/"Resuming Processing" are
                            # debug-file-only now.
                            with rotation_lock:
                                if rotation_state["waiting_logged"]:
                                    ready_name = api_manager.get_api_name(api_key)
                                    append_to_file_log(f"API {ready_name} cooldown finished")
                                    append_to_file_log(f"API {ready_name} READY")
                                    append_to_file_log("Returned to available API pool")
                                    append_to_file_log("Resuming processing...")
                                    rotation_state["waiting_logged"] = False

                    api_entry = next((a for a in api_manager.apis if a["key"] == api_key), None)
                    api_name = api_entry["name"] if api_entry else "API"

                    with stats_lock:
                        self.api_call_count += 1
                        self.root.after(0, self.inc_api)

                    # Requirement #3 (Request Console: "API Switched"): only log
                    # the explicit "Ali1 -> Ali2" switch line when the active
                    # API actually changed since the last request.
                    with rotation_lock:
                        if rotation_state["active_name"] not in (None, api_name):
                            self.log_request(f"API Switched | {rotation_state['active_name']} \u2192 {api_name}", "queue_returned", category="switch")
                        rotation_state["active_name"] = api_name

                    # Bug Fix (Item #5): "API Selected" and "Waiting for
                    # Response" are debug-file-only now — not in the Final
                    # spec's Request Console list. Only "Request Sent" is.
                    append_to_file_log(f"API {api_name} Selected")
                    # Requirement (Single Line Console Logs): store the exact
                    # API name that sent THIS request on the job itself — the
                    # Response Console must reuse this stored name later rather
                    # than ever re-deriving/guessing it from anything else.
                    item["sent_by_api"] = api_name
                    self.log_request(f"[{api_name}] Request Sent | {item['filename']}", "request_sent", category="request_sent")
                    append_to_file_log(f"[{api_name}] Waiting for Response...")

                    meta_data, status, is_429 = generate_metadata_single_key(
                        item["jpg_path"], api_key, CONFIG.get("model", "gemini-2.5-flash")
                    )

                    # Bug Fix (Item #5): the generic "Response Received - status"
                    # line is now debug-file-only; the Response Console's own
                    # unified "Response Received | file | STATUS" line fires
                    # only once the file's outcome is final (success/permanent
                    # fail below), using item["sent_by_api"] — never a guess.
                    append_to_file_log(f"[{api_name}] Response Received: {item['filename']} - {status}")

                    if status == "SUCCESS" and meta_data:
                        # Bug Fix (Item #5): "Metadata Generated" stays debug-file
                        # only. The visible "Success" line (Requirement #4) fires
                        # once in Phase 2 below, after the CSV write is verified —
                        # that's the file's actual final outcome, not just the
                        # Gemini call succeeding.
                        append_to_file_log(f"METADATA_GENERATED: {item['filename']}")
                        break

                    last_status = status

                    if is_429:
                        is_fresh_cooldown = api_manager.trigger_429_cooldown(api_key, request_id=req_id)
                        append_to_file_log(f"RATE_LIMIT_429: {item['filename']}")

                        if is_fresh_cooldown:
                            # Requirement (Single Line Console Logs): pipe-separated,
                            # single line, API name included. Per the example, a 429
                            # is immediately followed by "API Switched" (glued, no
                            # blank line) — NOT "Retry Started"; that line is
                            # reserved for genuine (non-429) errors below.
                            self.log_request(f"[{api_name}] 429 Rate Limit | {item['filename']}", "cooldown", category="error")
                            cooldown_end = time.strftime("%H:%M:%S", time.localtime(time.time() + 60))
                            append_to_file_log(f"API {api_name} entered cooldown (60s)")
                            append_to_file_log(f"Cooldown ends at {cooldown_end}")
                        else:
                            # Bug Fix (Live/stale 429 burst): several requests
                            # were already in flight on this key before the
                            # FIRST 429 came back and switched us away from it.
                            # Those late-arriving 429s are real, but the key is
                            # already known to be cooling down — logging another
                            # "429 Rate Limit" + implying another switch for each
                            # one is just noise that makes it look like the
                            # engine keeps sending NEW requests to an already-
                            # rate-limited key (it isn't — these are old
                            # responses catching up). File-log only.
                            append_to_file_log(f"[{api_name}] 429 (stale, already cooling down): {item['filename']}")

                        # Bug Fix (Item #5): the detailed queue-return/scheduler-
                        # selecting narration is debug-file-only.
                        append_to_file_log(f"File returned to processing queue: {item['filename']}")
                        append_to_file_log("Scheduler selecting next available API...")
                        # Bug Fix: do NOT reset rotation_state["active_name"] to
                        # None here — the switch-detection check below treats
                        # None as "no previous API, nothing to compare", which
                        # would silently SKIP logging "API Switched" for the very
                        # rotation this 429 just caused. Leaving the old name in
                        # place lets the next pick's natural
                        # old-name != new-name comparison fire correctly.
                        continue  # No counter, no backoff sleep — 429 can never permanently fail this file.

                    # Genuine (non-429) error — 503, network, empty/invalid AI
                    # response, etc. Unlike 429, these CAN eventually permanently
                    # fail after max_retries, per the Permanent Failure Rules.
                    # Requirement (Request Console): EVERY failed request shows
                    # its error category immediately, live — not just 429/503.
                    error_category = self._classify_failure_reason(status)
                    error_labels = {
                        "429": "429 Rate Limit",
                        "503": "503 Service Unavailable",
                        "network": "Network Error",
                        "empty": "Empty AI Response",
                        "invalid": "Invalid AI Response",
                        "unknown": "Unknown Error",
                    }
                    error_label = error_labels.get(error_category, "Unknown Error")
                    # Per the example format: 429/503 show "[ApiName] Category",
                    # other categories show just the category with no API prefix.
                    if error_category in ("429", "503"):
                        self.log_request(f"[{api_name}] {error_label} | {item['filename']}", "cooldown", category="error")
                    else:
                        self.log_request(f"{error_label} | {item['filename']}", "cooldown", category="error")
                    append_to_file_log(f"[{api_name}] Error ({status}): {item['filename']}")

                    genuine_error_attempts += 1
                    if genuine_error_attempts <= max_retries:
                        backoff = min(2 ** genuine_error_attempts, MAX_BACKOFF_SECONDS)
                        self.log_request(f"Retry Started | {item['filename']}", "warning", category="retry")
                        append_to_file_log(f"RETRY: {item['filename']} API retry #{genuine_error_attempts} in {backoff}s ({status})")
                        time.sleep(backoff)
                        continue

                    # Retries exhausted for a GENUINE (non-429) error only.
                    with stats_lock:
                        failed_count += 1
                    failed_items.append((item["csv_filename"], last_status))
                    resolved = True
                    # Requirement (unified format): Response Console always uses
                    # "Response Received | file | STATUS", with the SAME API
                    # name that sent the request — stored on the job, never
                    # re-derived/guessed here.
                    self.log(f"[{item['sent_by_api']}] Response Received | {item['filename']} | PERMANENT FAIL", "error", category="response")
                    self.root.after(0, lambda f=item["csv_filename"], s=last_status: self.failed_tree.insert("", "end", values=(f, s, "Retry")))
                    append_to_file_log(f"JOB_FAILED: {item['filename']} - {last_status}")
                    update_progress_status(
                        folder_path,
                        completed=already_done_before_this_run + completed_count,
                        failed=failed_count,
                        remaining=total_images - completed_count - failed_count,
                        last_processed_design_number=item["design_number"],
                        last_processed_filename=item["filename"],
                        status="IN_PROGRESS"
                    )
                    update_dashboard()
                    return

                metadata_dict = {
                    "original_filename": item["csv_filename"],
                    "title": meta_data["title"],
                    "description": meta_data["description"],
                    "category": meta_data["category"],
                    "keywords": meta_data["keywords"],
                    "people_property": meta_data["people_property"],
                }

                # ---- Phase 2: write + verify CSV (Requirement #2/#32/#33: Job
                # Completed Rule). Metadata being generated is NOT enough — only
                # a verified on-disk write may mark this job completed. On write
                # failure, retry ONLY the write (never re-call Gemini for data
                # already generated and validated). ----
                write_attempt = 0
                while True:
                    append_to_file_log(f"CSV_WRITE_STARTED: {item['filename']} (Row {item['design_number'] + 1})")
                    csv_manager.update_exact_row_in_memory(item["design_number"], metadata_dict)
                    write_ok, write_reason = csv_manager.flush_and_verify(item["design_number"], item["csv_filename"])

                    if write_ok:
                        append_to_file_log(f"CSV_WRITE_SUCCESS: {item['filename']} (Row {item['design_number'] + 1})")
                        with stats_lock:
                            completed_count += 1
                            qa_sum += meta_data["qa_score"]
                        # Bug Fix (QA Score CSV organization): stage the QA score
                        # under the SAME design_number key as the main site CSVs
                        # instead of an append-in-completion-order list, so
                        # qa_score.csv gets the identical Row N+1 == design
                        # number N positional alignment as Adobe/Shutterstock/
                        # Vecteezy — not "whichever file finished first".
                        csv_manager.update_qa_score(item["design_number"], item["csv_filename"], meta_data["qa_score"])
                        append_to_file_log(f"JOB_COMPLETED: {item['filename']} (Design #{item['design_number']})")
                        resolved = True
                        # Bug Fix (Item #5): "Metadata Generated"/"CSV Updated"
                        # per-file detail moved to debug-file-only above/below;
                        # Requirement #4's single visible outcome is "Success".
                        self.log(f"[{item['sent_by_api']}] Response Received | {item['filename']} | SUCCESS", "success", category="response")
                        append_to_file_log(f"CSV_UPDATED: {item['filename']}")
                        # Requirement (crash/resume visibility): update the
                        # progress checkpoint the INSTANT this row is verified on
                        # disk — not batched — so if the process is killed right
                        # after, this file still shows exactly how far it got.
                        update_progress_status(
                            folder_path,
                            completed=already_done_before_this_run + completed_count,
                            failed=failed_count,
                            remaining=total_images - completed_count - failed_count,
                            last_processed_design_number=item["design_number"],
                            last_processed_filename=item["filename"],
                            status="IN_PROGRESS"
                        )
                        break

                    append_to_file_log(f"CSV_WRITE_FAILED: {item['filename']} - {write_reason}")
                    write_attempt += 1
                    if write_attempt <= max_retries:
                        backoff = min(2 ** write_attempt, MAX_BACKOFF_SECONDS)
                        # Bug Fix (Item #2/#5): no "Retry N/M" counter shown —
                        # only the Request Console's "Retry Started" line.
                        self.log_request(f"Retry Started | {item['filename']}", "warning", category="retry")
                        append_to_file_log(f"RETRY: {item['filename']} CSV write retry #{write_attempt} in {backoff}s")
                        time.sleep(backoff)
                        continue

                    with stats_lock:
                        failed_count += 1
                    failed_items.append((item["csv_filename"], f"CSV_WRITE_FAILED: {write_reason}"))
                    resolved = True
                    # Requirement (unified format): same Response Received |
                    # file | PERMANENT FAIL line, same stored API name.
                    self.log(f"[{item['sent_by_api']}] Response Received | {item['filename']} | PERMANENT FAIL", "error", category="response")
                    self.root.after(0, lambda f=item["csv_filename"], s=write_reason: self.failed_tree.insert("", "end", values=(f, f"CSV_WRITE_FAILED: {s}", "Retry")))
                    append_to_file_log(f"JOB_FAILED: {item['filename']} - CSV_WRITE_FAILED: {write_reason}")
                    update_progress_status(
                        folder_path,
                        completed=already_done_before_this_run + completed_count,
                        failed=failed_count,
                        remaining=total_images - completed_count - failed_count,
                        last_processed_design_number=item["design_number"],
                        last_processed_filename=item["filename"],
                        status="IN_PROGRESS"
                    )
                    break

                # Update UI Dashboard Live
                update_dashboard()
            except Exception as e:
                # Bug Fix (CRITICAL - Final Summary accuracy / no silent
                # exceptions): previously, any unexpected exception here
                # (a bug, a library edge case, etc.) propagated straight up
                # to the ThreadPoolExecutor's Future and was only logged
                # after the fact by the dispatcher's done-callback — this
                # item was NEVER counted as completed OR failed, silently
                # vanishing from all totals. That made Final Summary's
                # numbers (Metadata Saved + Permanent Failed) not add up to
                # the actual file count. Now it's always counted as a
                # permanent failure instead of disappearing.
                #
                # Bug Fix (double-count guard): if the exception happened
                # AFTER this item was already legitimately resolved (e.g. in
                # the final update_dashboard() call, after a success or a
                # real permanent-fail branch already ran), `resolved` is
                # already True — skip re-counting it a second time here,
                # which would otherwise make (Saved + Failed) exceed the
                # actual file count in the other direction.
                if locals().get("resolved", False):
                    append_to_file_log(f"POST_RESOLUTION_EXCEPTION (ignored for counting): {item.get('filename', '?')} - {type(e).__name__}: {str(e)}")
                else:
                    with stats_lock:
                        failed_count += 1
                    failed_items.append((item.get('csv_filename', item.get('filename', '?')), f'INTERNAL_EXCEPTION: {type(e).__name__}: {str(e)}'))
                    append_to_file_log(f"WORKER_EXCEPTION: {item.get('filename', '?')} - {type(e).__name__}: {str(e)}")
                    self.log(f"[{api_name}] Response Received | {item.get('filename', '?')} | PERMANENT FAIL", "error", category="response")
                    self.root.after(0, lambda f=item.get('csv_filename', item.get('filename', '?')), s=str(e): self.failed_tree.insert("", "end", values=(f, f"INTERNAL_EXCEPTION: {s}", "Retry")))
                update_dashboard()

        # 3. REQUEST DISPATCHER (PRODUCER)
        # Bug Fix (Optimize / Reduce Idle Thread Churn): a flat 25 workers
        # were spawned regardless of the configured rate limit. With only
        # one API key active at a time and a per-key limit like 10/min,
        # most of those 25 threads had nothing to do but sit in the
        # get_available_api() polling loop (time.sleep(0.3) + a lock
        # acquisition each time) — extra CPU/lock contention for no
        # throughput benefit. Size the pool the same way engine.py's own
        # DynamicQueueEngine does: enough workers to use the biggest
        # per-key limit configured, capped at 20.
        worker_count = max(1, min(max((api.get("limit", 10) for api in api_manager.apis), default=10), 20))
        executor = ThreadPoolExecutor(max_workers=worker_count)

        for item in indexed_items:
            if not self.is_running:
                break

            while self.is_paused:
                time.sleep(0.5)

            if not self.is_running:
                break

            # Update live UI preview/status. The actual "Request Sent" log
            # line (with the resolved API name) is written inside
            # async_response_handler once an API key is picked — logging it
            # here too, before dispatch, would show no API name yet and
            # create a duplicate/misleading entry.
            #
            # Bug Fix (UI Freeze on Start): the heavy part (file read,
            # decode, resize, SHA256 hash — see _prepare_preview_data) now
            # runs right here, in this background dispatcher thread, never
            # on the Tk main thread. Only the cheap label/PhotoImage updates
            # go through root.after(0, ...).
            self._preview_seq += 1
            seq = self._preview_seq
            preview_data = self._prepare_preview_data(item['jpg_path'], item['eps_path'])

            def _show_preview(it=item, seq=seq, data=preview_data):
                self.lbl_curr_file.config(text=f"File: {it['filename']}")
                self.lbl_curr_status.config(text=f"Queued Index #{it['permanent_index']}")
                # A newer image was already dispatched by the time the main
                # thread got to this one — only the latest preview is ever
                # visible anyway, so skip the redraw instead of wasting it.
                if seq == self._preview_seq:
                    self._apply_preview_data(data)

            self.root.after(0, _show_preview)

            # Dispatch Request ASYNCHRONOUSLY to worker pool (DO NOT WAIT FOR RESPONSE)
            future = executor.submit(async_response_handler, item)
            # Bug Fix: log any exception raised inside a worker thread instead
            # of letting the discarded Future swallow it silently.
            future.add_done_callback(
                lambda f, it=item: (f.exception() and append_to_file_log(
                    f"WORKER EXCEPTION on Index #{it['permanent_index']} ({it['filename']}): {f.exception()}"
                ))
            )

            # Brief delay between rapid request dispatches
            time.sleep(0.05)

        append_to_file_log("All Queue Requests Dispatched. Awaiting final pending responses...")
        executor.shutdown(wait=True)

        # Bug Fix (CRITICAL — Requirement #2/#32/#33): each row was already
        # written AND verified on disk the instant it completed above; this
        # final flush is just a harmless safety net (idempotent — flush_to_disk
        # no-ops if nothing is dirty), not the primary persistence mechanism.
        # This single call also covers qa_score.csv now (Bug Fix: QA Score
        # CSV organization) — update_qa_score() above already staged every
        # score under its design_number, so flush_to_disk() writes it out
        # with the identical Row N+1 == design number N alignment as the
        # Adobe Stock/Shutterstock/Vecteezy CSVs, in one atomic pass.
        csv_manager.flush_to_disk()
        append_to_file_log(f"CSV Export Complete: {completed_count} row(s) written and verified.")
        self.log("CSV Export Completed", "success", category="response")

        # Bug Fix #27: failed_items was collected but never actually written
        # anywhere — persist it to failed_queue.csv so it survives a restart.
        if failed_items:
            save_failed_queue_csv(folder_path, failed_items)
            append_to_file_log(f"Logged {len(failed_items)} permanently failed image(s) to failed_queue.csv")

        update_progress_status(
            folder_path,
            completed=already_done_before_this_run + completed_count,
            failed=failed_count,
            remaining=total_images - completed_count - failed_count,
            status="COMPLETED" if self.is_running else "STOPPED_BY_USER"
        )

        # Requirement #3 (Request Console: "Engine Finished").
        elapsed_total = time.time() - start_time
        m, s = divmod(int(elapsed_total), 60)
        append_to_file_log(f"Engine Finished: {completed_count} Success, {failed_count} Failed, {m}m {s}s")
        self.log_request("Engine Finished", "success", category="lifecycle")

        self.stop_engine()
        avg_qa = int(qa_sum / max(1, completed_count))
        self.root.after(0, lambda: messagebox.showinfo("Finished", f"Workflow Completed!\nProcessed: {completed_count}\nFailed: {failed_count}\nAverage QA: {avg_qa}%"))

    def inc_api(self):
        self.card_labels["API Requests"].config(text=f"{self.api_call_count} / 1000")
        self.lbl_api_today.config(text=f"Total Requests: {self.api_call_count}")

# ==========================================
# 3. MAIN APPLICATION ENTRY POINT
# ==========================================
if __name__ == "__main__":
    root = tk.Tk()
    app = JinnahMetadataGeneratorGUI(root)
    root.mainloop()