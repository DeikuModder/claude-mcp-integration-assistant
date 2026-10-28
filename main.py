# -*- coding: utf-8 -*-
"""
Claude MCP Integration Assistant

A cross-platform Desktop GUI Installer Application that automates the
installation, token authentication, and configuration of the Google NotebookLM
MCP Server (notebooklm-mcp-cli) for Anthropic's Claude Desktop.

The source file is intentionally ASCII-only (non-ASCII glyphs are written as
unicode escapes) so it is immune to encoding corruption across platforms.
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile
import threading
import logging
import platform
import hashlib
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import customtkinter as ctk
from tkinter import messagebox

# ---------------------------------------------------------------------------
# Package / command constants
# ---------------------------------------------------------------------------
PACKAGE_NAME = "notebooklm-mcp-cli"
# The unified package exposes two entry points:
#   - "nlm"            -> CLI (used for authentication: `nlm login`)
#   - "notebooklm-mcp" -> the MCP server binary referenced by Claude Desktop
CLI_BINARY = "nlm"
MCP_BINARY = "notebooklm-mcp"
# notebooklm-mcp-cli allows fastmcp<5.0 which pip currently resolves to
# fastmcp 4.1.0; that version's server code imports mcp.server.request_state
# while pip simultaneously resolves the old mcp 1.x, crashing the server at
# startup ("FastMCP server support is not installed"). fastmcp 2.x pairs with
# mcp 1.x and is the combination proven to work, so constrain it here.
FASTMCP_CONSTRAINT = "fastmcp<3"


def _read_bundled_version() -> str:
    """Read the app version injected by the build pipeline.

    CI writes the release tag into `version.txt` and bundles it next to the
    app (PyInstaller --add-data). When running from source the repo's
    placeholder file says "dev", which also disables update checks.
    """
    for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        try:
            path = Path(base) / "version.txt"
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value and value.lower() != "dev":
                    return value
        except OSError:
            continue
    return "dev"


APP_VERSION = _read_bundled_version()

# ----------------------- automatic updater configuration --------------------
# Releases are published by .github/workflows/release.yml when a v* tag is
# pushed; the app polls that public repo's latest release for newer versions.
UPDATER_REPO = "DeikuModder/claude-mcp-integration-assistant"


def _update_asset_name(version: str) -> str:
    """Asset filename produced by CI for the OS this app is running on."""
    if platform.system() == "Windows":
        return f"ClaudeMCPIntegrationAssistant-{version}-windows.exe"
    # macOS ships a zipped .app bundle; a bare unix binary cannot be
    # double-clicked in Finder and is unusable for end users.
    return f"ClaudeMCPIntegrationAssistant-{version}-macos.zip"

# Status indicator glyphs expressed as escapes to keep this source ASCII-only.
DOT_RED = "\u25cf"     # filled circle
DOT_GREEN = "\u2713"   # check mark

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('mcp_integration.log'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class MCPIntegrationApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"Claude MCP Integration Assistant v{APP_VERSION}")
        self.geometry("880x770")
        self.minsize(800, 710)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.installation_in_progress = False
        self.auth_in_progress = False
        self.config_in_progress = False
        self.auth_process: Optional[subprocess.Popen] = None
        # Cache of environment tooling paths (populated lazily).
        self._installer_info: Dict[str, Optional[str]] = {}
        self.create_widgets()
        self.log_message(f"Claude MCP Integration Assistant v{APP_VERSION}" + (" (dev build - update checks disabled)" if APP_VERSION == "dev" else ""))
        # Run the first environment/status scan in the background so the
        # window appears instantly even on slow filesystems.
        self.after(100, self._initial_scan)
        if APP_VERSION != "dev":
            self.after(1500, lambda: self._check_for_updates(manual=False))

    # ------------------------------------------------------------------ UI ---
    def create_widgets(self):
        main_frame = ctk.CTkFrame(self)
        main_frame.grid(row=0, column=0, padx=20, pady=20, sticky="nsew")
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(3, weight=1)
        title_label = ctk.CTkLabel(main_frame, text="Claude MCP Integration Assistant", font=ctk.CTkFont(size=24, weight="bold"))
        title_label.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="n")
        subtitle_label = ctk.CTkLabel(main_frame, text="Automate Google NotebookLM MCP Server setup for Claude Desktop", font=ctk.CTkFont(size=12), text_color="gray50")
        subtitle_label.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="n")
        self.create_status_bar(main_frame)
        self.tabview = ctk.CTkTabview(main_frame, height=320)
        self.tabview.grid(row=3, column=0, padx=20, pady=(0, 15), sticky="nsew")
        self.tabview.add("Google NotebookLM Connection")
        self.setup_notebooklm_tab()
        self.create_log_area(main_frame)

    def create_status_bar(self, parent):
        status_frame = ctk.CTkFrame(parent)
        status_frame.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")
        status_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        status_label = ctk.CTkLabel(status_frame, text="System Status:", font=ctk.CTkFont(weight="bold"))
        status_label.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.status_runtime = self._create_status_indicator(status_frame, "Runtime (uv/py)", 0, 1)
        self.status_package = self._create_status_indicator(status_frame, "Package", 0, 2)
        self.status_auth = self._create_status_indicator(status_frame, "Auth Token", 0, 3)
        self.status_claude = self._create_status_indicator(status_frame, "Claude JSON", 0, 4)
        self.btn_updates = ctk.CTkButton(
            status_frame, text="Check for updates", height=24, width=140,
            command=lambda: self._check_for_updates(manual=True),
            font=ctk.CTkFont(size=10),
        )
        self.btn_updates.grid(row=1, column=0, columnspan=4, padx=10, sticky="e")

    def _create_status_indicator(self, parent, label_text, row, col):
        indicator_frame = ctk.CTkFrame(parent)
        indicator_frame.grid(row=row, column=col, padx=5, pady=5, sticky="ew")
        label = ctk.CTkLabel(indicator_frame, text=label_text, font=ctk.CTkFont(size=10))
        label.grid(row=0, column=0, padx=5, pady=2, sticky="n")
        status_dot = ctk.CTkLabel(indicator_frame, text=DOT_RED, font=ctk.CTkFont(size=20), text_color="red")
        status_dot.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="n")
        return status_dot

    def setup_notebooklm_tab(self):
        """Build the 3 steps with an inline (button-on-the-right) layout.

        The layout is horizontally driven so the action buttons are always
        visible regardless of platform-specific font metrics (macOS vs Windows)
        or available vertical space.
        """
        tab = self.tabview.tab("Google NotebookLM Connection")
        tab.grid_columnconfigure(0, weight=1)

        steps = [
            ("1", "Install Server Dependencies",
             "Install the notebooklm-mcp-cli package using uv (falls back to pip).",
             "1. Install Server Dependencies", self.install_dependencies, "btn_install"),
            ("2", "Link Google Account",
             "Authenticate with Google to access your NotebookLM notebooks.",
             "2. Link Google Account", self.authenticate_google, "btn_auth"),
            ("3", "Connect to Claude Desktop",
             "Auto-detect and inject the server into claude_desktop_config.json.",
             "3. Connect to Claude Desktop", self.configure_claude, "btn_configure"),
        ]

        for idx, (num, title, desc, btn_text, cmd, attr) in enumerate(steps):
            step_frame = ctk.CTkFrame(tab)
            step_frame.grid(row=idx, column=0, padx=10, pady=6, sticky="ew")
            step_frame.grid_columnconfigure(1, weight=1)

            num_label = ctk.CTkLabel(step_frame, text=num, font=ctk.CTkFont(size=26, weight="bold"), width=44)
            num_label.grid(row=0, column=0, rowspan=2, padx=(14, 8), pady=12)

            title_label = ctk.CTkLabel(step_frame, text=title, font=ctk.CTkFont(size=15, weight="bold"), anchor="w")
            title_label.grid(row=0, column=1, padx=4, pady=(12, 0), sticky="ew")

            desc_label = ctk.CTkLabel(step_frame, text=desc, font=ctk.CTkFont(size=11), anchor="w", wraplength=360, text_color="gray60")
            desc_label.grid(row=1, column=1, padx=4, pady=(0, 12), sticky="ew")

            btn = ctk.CTkButton(step_frame, text=btn_text, height=40, width=230, command=cmd, font=ctk.CTkFont(size=12, weight="bold"))
            btn.grid(row=0, column=2, rowspan=2, padx=14, pady=12, sticky="e")
            setattr(self, attr, btn)

    def create_log_area(self, parent):
        log_frame = ctk.CTkFrame(parent)
        log_frame.grid(row=4, column=0, padx=0, pady=(6, 0), sticky="nsew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)
        log_header = ctk.CTkFrame(log_frame)
        log_header.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        log_header.grid_columnconfigure(0, weight=1)
        log_label = ctk.CTkLabel(log_header, text="Activity Log", font=ctk.CTkFont(weight="bold"))
        log_label.grid(row=0, column=0, padx=5, sticky="w")
        self.btn_clear_log = ctk.CTkButton(log_header, text="Clear", width=60, height=24, command=self.clear_log)
        self.btn_clear_log.grid(row=0, column=1, padx=5, sticky="e")
        self.log_textbox = ctk.CTkTextbox(log_frame, wrap="word", height=90, state="disabled")
        self.log_textbox.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def log_message(self, message: str, level: str = "INFO"):
        if level == "ERROR":
            logger.error(message)
        elif level == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)
        self.log_textbox.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_textbox.insert("end", f"[{timestamp}] {level}: {message}\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")
        self.log_message("Log cleared")

    # ------------------------------------------------- environment detection ---
    def _candidate_bin_dirs(self):
        """Directories where uv / pip / console scripts commonly live.

        GUI apps launched from Explorer/Finder inherit a limited PATH, and
        `uv tool install` places binaries under ~/.local/bin which is often not
        on PATH, so we probe these explicitly.
        """
        home = Path.home()
        dirs = [str(home / ".local" / "bin")]
        system = platform.system()
        if system == "Windows":
            appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
            localappdata = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
            appdata_path = Path(appdata)
            localappdata_path = Path(localappdata)
            # pip --user installs land in e.g. C:\...\Roaming\Python\Python314\Scripts
            # (note the extra Python\ level that a naive "Python*/Scripts" misses).
            for pattern in ("Python/Python*/Scripts", "Python*/Scripts"):
                for p in appdata_path.glob(pattern):
                    dirs.append(str(p))
            # Per-user Python installs, e.g. C:\...\Local\Programs\Python\Python313\Scripts
            for pattern in ("Programs/Python/Python*/Scripts", "Programs/Python/*/Scripts"):
                for p in localappdata_path.glob(pattern):
                    dirs.append(str(p))
            if os.environ.get("USERPROFILE"):
                dirs.append(str(Path(os.environ["USERPROFILE"]) / ".local" / "bin"))
        else:
            dirs.extend([
                str(home / "bin"),
                "/usr/local/bin",
                "/opt/homebrew/bin",
                "/usr/bin",
            ])
            # macOS pip --user scripts: ~/Library/Python/3.x/bin
            for p in (Path.home() / "Library" / "Python").glob("Python*/bin"):
                dirs.append(str(p))
            # python.org installer scripts
            for p in Path("/Library/Frameworks/Python.framework/Versions").glob("*/bin"):
                dirs.append(str(p))
            # Homebrew-managed pythons (Apple Silicon + Intel layouts)
            for p in Path("/opt/homebrew/opt").glob("python@*/bin"):
                dirs.append(str(p))
            for p in Path("/usr/local/opt").glob("python@*/bin"):
                dirs.append(str(p))

        # Also probe the Scripts/bin directory belonging to whatever python/pip
        # we already located (covers interpreters whose Scripts dir isn't in the
        # patterns above, or isn't on PATH for a GUI-launched process).
        info = self.__dict__.get("_installer_info") or {}
        for key in ("python", "pip"):
            exe = info.get(key)
            if not exe:
                continue
            parent = Path(exe).parent
            dirs.append(str(parent))
            if parent.name.lower() != "scripts":
                dirs.append(str(parent / "Scripts"))
        return dirs

    def _find_executable(self, name: str) -> Optional[str]:
        """Locate an executable on PATH or in common install directories."""
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
        exts = [""]
        if platform.system() == "Windows":
            exts = [".exe", ".cmd", ".bat", ""]
        for d in self._candidate_bin_dirs():
            for ext in exts:
                cand = Path(d) / f"{name}{ext}"
                try:
                    if cand.is_file():
                        return str(cand)
                except OSError:
                    continue
        return None

    def detect_installers(self) -> Dict[str, Optional[str]]:
        """Detect uv / python / pip available on the machine."""
        info = {
            "uv": self._find_executable("uv"),
            "python": self._find_executable("python") or self._find_executable("python3"),
            "pip": self._find_executable("pip") or self._find_executable("pip3"),
        }
        # On Windows the `py` launcher is a reliable python entry point.
        if info["python"] is None and platform.system() == "Windows":
            info["python"] = self._find_executable("py")
        self._installer_info = info
        return info

    def _initial_scan(self):
        """Background scan performed once at startup."""
        self.update_status_indicators()

    # ------------------------------------------------------ status updates ---
    def update_status_indicators(self):
        """Refresh all four status indicators and Step 1 button state."""
        info = self.detect_installers()
        runtime_ok = bool(info.get("uv") or info.get("python"))
        self._update_indicator(self.status_runtime, runtime_ok)

        mcp_installed = self._find_executable(MCP_BINARY) is not None
        self._update_indicator(self.status_package, mcp_installed)

        auth_ok = self._check_auth_token_exists()
        self._update_indicator(self.status_auth, auth_ok)

        claude_ok = self._check_claude_config_exists()
        self._update_indicator(self.status_claude, claude_ok)

        self._update_step1_state(mcp_installed)

    def _update_indicator(self, indicator, ok: bool):
        if ok:
            indicator.configure(text=DOT_GREEN, text_color="green")
        else:
            indicator.configure(text=DOT_RED, text_color="red")

    def _update_step1_state(self, installed: bool):
        """Reflect whether the server package is already installed.

        When the package is detected, the install button is replaced with a
        clear confirmation so duplicates are not installed.
        """
        if self.installation_in_progress:
            return
        if installed:
            version = self._get_package_version()
            label = "Installed" + (f" ({version})" if version else "")
            self.btn_install.configure(
                state="disabled",
                text=f"\u2713 Server already installed",
                fg_color="#2e7d32",
                hover_color="#2e7d32",
            )
            # Keep a secondary way to force a reinstall/upgrade.
            self._installed_label = label
        else:
            self.btn_install.configure(
                state="normal",
                text="1. Install Server Dependencies",
                fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"],
            )

    def _get_package_version(self) -> Optional[str]:
        """Best-effort version string for the installed CLI."""
        exe = self._find_executable(CLI_BINARY) or self._find_executable(MCP_BINARY)
        if not exe:
            return None
        for args in ([exe, "--version"],):
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=15)
                out = (r.stdout or r.stderr).strip()
                if out:
                    return out.splitlines()[0].strip()[:60]
            except Exception:
                pass
        return None

    def _check_auth_token_exists(self) -> bool:
        """Fast, offline check for saved credentials (no network/CLI call).

        The CLI stores state under ~/.notebooklm-mcp-cli/ (config.toml, auth.json,
        profiles/). A full validation is done separately via `nlm login --check`.
        """
        home = Path.home()
        storage = home / ".notebooklm-mcp-cli"
        possible_paths = [
            storage / "auth.json",
            storage / "profiles",
            home / ".notebooklm-mcp" / "auth.json",
        ]
        for p in possible_paths:
            try:
                if p.is_dir():
                    if any(p.iterdir()):
                        return True
                elif p.exists():
                    return True
            except OSError:
                continue
        return False

    def _check_claude_config_exists(self) -> bool:
        try:
            return Path(self.get_claude_config_path()).exists()
        except Exception:
            return False

    # ------------------------------------------------------ path resolving ---
    def _get_claude_data_dirs(self) -> List[Path]:
        r"""Return candidate Claude Desktop data directories, best first.

        Windows has two Claude Desktop editions with different data locations:
          - Standard installer -> %APPDATA%\Claude
          - Microsoft Store (MSIX / packaged) edition -> AppData is virtualized,
            so its real config lives under
            %LOCALAPPDATA%\Packages\<Package>\LocalCache\Roaming\Claude
        The MSIX location wins when present because the Store edition reads
        neither %APPDATA%\Claude nor anything else outside its package.
        """
        candidates: List[Path] = []
        localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        packages_root = Path(localappdata) / "Packages"
        if packages_root.is_dir():
            # Store edition package ids look like "Claude_<publisher-hash>"; the
            # hash varies per distribution, hence the wildcard globs.
            seen_pkgs = set()
            for pattern in ("Claude_*", "AnthropicClaude_*"):
                for pkg in packages_root.glob(pattern):
                    if pkg.name in seen_pkgs:
                        continue
                    seen_pkgs.add(pkg.name)
                    candidates.append(pkg / "LocalCache" / "Roaming" / "Claude")

        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        candidates.append(Path(appdata) / "Claude")

        if platform.system() == "Darwin":
            candidates.append(Path.home() / "Library" / "Application Support" / "Claude")
        else:
            candidates.append(Path.home() / ".config" / "Claude")

        deduped: List[Path] = []
        for d in candidates:
            if str(d) not in {str(x) for x in deduped}:
                deduped.append(d)
        return deduped

    def get_claude_config_path(self) -> str:
        # Prefer a candidate that already holds a config file (proof that this
        # is the edition the user actually runs).
        for d in self._get_claude_data_dirs():
            cfg = d / "claude_desktop_config.json"
            if cfg.is_file():
                return str(cfg)
        # Otherwise prefer a dir with other Claude runtime data present.
        for d in self._get_claude_data_dirs():
            try:
                if d.is_dir() and any(d.iterdir()):
                    return str(d / "claude_desktop_config.json")
            except OSError:
                continue
        # Fallback: standard location (created on demand).
        return str(self._get_claude_data_dirs()[0] / "claude_desktop_config.json")

    def find_notebooklm_mcp_executable(self) -> Optional[str]:
        result = self._find_executable(MCP_BINARY)
        if result:
            self.log_message(f"Found {MCP_BINARY} at: {result}")
            return result
        self.log_message(f"{MCP_BINARY} executable not found", level="WARNING")
        return None

    # ---------------------------------------------------------- step 1: install ---
    def install_dependencies(self):
        if self.installation_in_progress:
            return
        if self._find_executable(MCP_BINARY):
            self.log_message(f"{PACKAGE_NAME} is already installed; nothing to do.")
            self.update_status_indicators()
            messagebox.showinfo("Already installed", f"{PACKAGE_NAME} is already installed on this system.")
            return
        if not self._ensure_runtime_available():
            return
        self.installation_in_progress = True
        self.btn_install.configure(state="disabled", text="Installing...")
        thread = threading.Thread(target=self._install_worker, daemon=True)
        thread.start()

    def _ensure_runtime_available(self) -> bool:
        """Make sure uv or python/pip exists; offer to install uv if not.

        Returns True if an installer is available (or was provisioned), False if
        the user declined and setup should abort.
        """
        info = self.detect_installers()
        if info.get("uv"):
            return True
        if info.get("pip") or info.get("python"):
            return True
        # Nothing usable. Explain and offer to bootstrap uv (which can also
        # provision a private Python interpreter, so the user needs no system
        # Python at all).
        proceed = messagebox.askyesno(
            "Python / uv not found",
            "Neither 'uv' nor 'python'/'pip' was found on this system.\n\n"
            "The NotebookLM MCP server needs one of them. We can install 'uv', "
            "a small standalone tool that manages Python packages (and can even "
            "fetch its own Python) automatically.\n\n"
            "Install uv now?",
        )
        if not proceed:
            self.log_message("Cannot continue: no Python/uv available and user declined uv install.", level="ERROR")
            messagebox.showwarning(
                "Setup cannot continue",
                "Please install Python 3.11+ (python.org) or uv (https://docs.astral.sh/uv/), "
                "then reopen this assistant and try again.",
            )
            return False
        self.installation_in_progress = True
        self.btn_install.configure(state="disabled", text="Installing uv...")
        success = self._install_uv()
        self.installation_in_progress = False
        if not success:
            self.btn_install.configure(state="normal", text="1. Install Server Dependencies")
            messagebox.showerror("uv install failed", "Could not install uv automatically. Please install it manually from https://docs.astral.sh/uv/.")
            return False
        info = self.detect_installers()
        return bool(info.get("uv") or info.get("python") or info.get("pip"))

    def _install_uv(self) -> bool:
        """Install uv using the official installer for the current platform."""
        system = platform.system()
        if system == "Windows":
            cmd = [
                "powershell", "-NoProfile", "-ExecutionPolicy", "ByPass",
                "-Command", "irm https://astral.sh/uv/install.ps1 | iex",
            ]
        else:
            cmd = ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]
        self.log_message("Installing uv via the official installer...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.stdout:
                self.log_message(result.stdout.strip()[:500])
            if result.returncode == 0:
                self.log_message("uv installed successfully.")
                return True
            self.log_message(f"uv installer exited with code {result.returncode}", level="ERROR")
            if result.stderr:
                self.log_message(result.stderr.strip()[:500], level="ERROR")
        except Exception as exc:
            self.log_message(f"Failed to install uv: {exc}", level="ERROR")
        return False

    def _install_worker(self):
        try:
            info = self.detect_installers()
            commands: List[Tuple[str, List[str]]] = []
            if info.get("uv"):
                commands.append(("uv tool install", [info["uv"], "tool", "install", PACKAGE_NAME, "--with", FASTMCP_CONSTRAINT]))
                commands.append(("uv pip install --system", [info["uv"], "pip", "install", "--system", PACKAGE_NAME, FASTMCP_CONSTRAINT]))
            if info.get("pip"):
                commands.append(("pip install", [info["pip"], "install", PACKAGE_NAME, FASTMCP_CONSTRAINT]))
            if info.get("python"):
                commands.append(("python -m pip install", [info["python"], "-m", "pip", "install", PACKAGE_NAME, FASTMCP_CONSTRAINT]))

            if not commands:
                raise RuntimeError("No installer (uv/pip/python) available")

            last_error = ""
            for label, cmd in commands:
                self.log_message(f"Attempting: {label}")
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                except Exception as exc:
                    last_error = f"{label} raised {exc}"
                    self.log_message(last_error, level="WARNING")
                    continue
                if result.stdout:
                    for line in result.stdout.strip().splitlines()[-8:]:
                        self.log_message(line)
                if result.returncode == 0:
                    self.log_message(f"Installation succeeded via: {label}")
                    warning = self._validate_server_install()
                    self.after(0, lambda: self.on_install_complete(True, warning_text=warning))
                    return
                last_error = result.stderr.strip() or f"{label} exited with {result.returncode}"
                self.log_message(f"{label} failed: {last_error[:300]}", level="WARNING")

            raise RuntimeError(last_error or "All installation methods failed")
        except Exception as exc:
            message = str(exc)
            self.log_message(f"Installation error: {message}", level="ERROR")
            self.after(0, lambda: self.on_install_complete(False, message))

    def _server_selfcheck(self, mcp_exe: Optional[str] = None) -> Tuple[bool, str]:
        """Send an MCP initialize handshake to the server binary and verify it
        answers. Catches dependency crashes that only appear at runtime, e.g.
        "FastMCP server support is not installed"."""
        exe = mcp_exe or self._find_executable(MCP_BINARY)
        if not exe:
            return (False, f"{MCP_BINARY} binary not found")
        msg = json.dumps({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "claude-mcp-assistant", "version": "1"}
            },
        }) + "\n"
        try:
            p = subprocess.Popen(
                [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            out, err = p.communicate(msg, timeout=30)
            p.kill()
            first = out.strip().splitlines()[0] if out.strip() else ""
            try:
                resp = json.loads(first) if first else {}
                if resp.get("result", {}).get("serverInfo", {}).get("name"):
                    return (True, f"Server responding as {resp['result']['serverInfo'].get('name')}")
            except (json.JSONDecodeError, IndexError, StopIteration):
                pass
            return (False, (err or out or "no response to initialize").strip()[:400])
        except Exception as exc:
            return (False, str(exc))

    def _heal_server_dependencies(self) -> bool:
        """Repair the fastmcp/mcp resolution when the installed pair crashes.

        Known signature: fastmcp 4.x imported from the old mcp package.
        Upgrading mcp to 2.x (or relaxing fastmcp to 2.x) fixes it.
        """
        self.log_message("Server self-test failed; attempting to repair fastmcp/mcp dependencies...", level="WARNING")
        info = self.detect_installers()
        commands: List[List[str]] = []
        if info.get("pip"):
            commands.append([info["pip"], "install", "--upgrade", "mcp>=2.0.0"])
        if info.get("python"):
            commands.append([info["python"], "-m", "pip", "install", "--upgrade", "mcp>=2.0.0"])
        for cmd in commands:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    self.log_message("Dependency repair succeeded: " + " ".join(cmd))
                    return True
                self.log_message("Heal attempt failed: " + (r.stderr or "").strip()[:300], level="WARNING")
            except Exception as exc:
                self.log_message(f"Heal attempt failed: {exc}", level="WARNING")
        return False

    def _validate_server_install(self) -> str:
        """Run the server self-test and, if it crashes with the known fastmcp
        signature, attempt an automatic repair. Returns a user-facing warning
        string (empty when everything is fine)."""
        mcp_exe = self._find_executable(MCP_BINARY)
        if not mcp_exe:
            return ""
        ok, detail = self._server_selfcheck(mcp_exe)
        if ok:
            return ""
        self.log_message(f"Server self-test failed: {detail}", level="WARNING")
        if ("FastMCP server support" in detail
                or "request_state" in detail
                or "ImportError" in detail
                or "ModuleNotFoundError" in detail):
            if self._heal_server_dependencies():
                ok2, detail2 = self._server_selfcheck(mcp_exe)
                if ok2:
                    self.log_message(f"Self-test passes after repair: {detail2}")
                    return ""
                detail = detail2
        return (
            f"The server was installed, but a self-test could not start it and it may fail in Claude Desktop.\n\n"
            f"Details: {detail[:300]}"
        )

    def on_install_complete(self, success: bool, error: str = "", warning_text: str = ""):
        self.installation_in_progress = False
        if success:
            self.log_message("Step 1 complete: server dependencies installed.")
            self.update_status_indicators()
            if warning_text:
                self.log_message(warning_text, level="WARNING")
                messagebox.showwarning("Installed with warning", warning_text)
            else:
                messagebox.showinfo("Success", f"{PACKAGE_NAME} installed successfully.\n\nProceed to Step 2 to authenticate Google.")
        else:
            self.btn_install.configure(state="normal", text="1. Install Server Dependencies")
            messagebox.showerror("Installation failed", f"Could not install {PACKAGE_NAME}.\n\n{error}")

    # ------------------------------------------------------------- step 2: auth ---
    def authenticate_google(self):
        if self.auth_in_progress:
            return
        if not self._find_executable(CLI_BINARY):
            messagebox.showwarning("Server not installed", "Please complete Step 1 first so the 'nlm' command is available.")
            return
        self.auth_in_progress = True
        self.btn_auth.configure(state="disabled", text="Authenticating...")
        self.log_message("Starting Google authentication (nlm login)...")
        thread = threading.Thread(target=self._auth_worker, daemon=True)
        thread.start()

    def _auth_worker(self):
        try:
            cli = self._find_executable(CLI_BINARY)
            if not cli:
                raise RuntimeError("The 'nlm' command could not be found. Complete Step 1 first.")

            if self._check_auth_status(cli):
                self.log_message("Existing valid credentials detected; no login needed.")
                self.after(0, lambda: self.on_auth_complete(True, "Already authenticated."))
                return

            # Spawn the interactive login. The CLI launches a managed browser
            # (Chrome via CDP) itself and blocks until the user completes the
            # flow, so run it non-blocking. `--storage protected` skips the
            # interactive storage-mode question; stdin is closed so that any
            # unexpected prompt errors out instead of silently hanging the GUI.
            self.auth_process = subprocess.Popen(
                [cli, "login", "--storage", "protected"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.after(0, self._show_auth_guidance)
            try:
                output, _ = self.auth_process.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                self.auth_process.kill()
                self.auth_process.communicate()
                self.after(0, lambda: self.on_auth_complete(False, "Authentication timed out after 10 minutes."))
                return
            finally:
                self.auth_process = None

            if output:
                for line in output.strip().splitlines()[-6:]:
                    self.log_message(line)

            if self._check_auth_status(cli):
                self.after(0, lambda: self.on_auth_complete(True, "Authentication successful."))
            else:
                self.after(0, lambda: self.on_auth_complete(False, "Authentication did not complete. Please try again."))
        except Exception as exc:
            message = str(exc)
            self.log_message(f"Authentication error: {message}", level="ERROR")
            self.after(0, lambda: self.on_auth_complete(False, message))

    def _check_auth_status(self, cli: Optional[str] = None) -> bool:
        """Return True if the CLI already holds valid credentials."""
        cli = cli or self._find_executable(CLI_BINARY)
        if not cli:
            return False
        try:
            result = subprocess.run([cli, "login", "--check"], capture_output=True, text=True, timeout=30)
            return result.returncode == 0
        except Exception:
            return False

    def _show_auth_guidance(self):
        self.log_message("A browser window should open for Google sign-in.")
        messagebox.showinfo(
            "Complete sign-in",
            "A browser window will open so you can sign in with your Google account.\n\n"
            "Please complete the sign-in there. This assistant is waiting for it to finish.",
        )

    def on_auth_complete(self, success: bool, message: str = ""):
        self.auth_in_progress = False
        self.btn_auth.configure(state="normal", text="2. Authenticate Google")
        if success:
            self.log_message(f"Step 2 complete: {message}")
            self.update_status_indicators()
            messagebox.showinfo("Success", "Google authentication completed. Proceed to Step 3.")
        else:
            messagebox.showerror("Authentication failed", message or "Could not authenticate with Google.")

    # ---------------------------------------------------------- step 3: config ---
    def configure_claude(self):
        if self.config_in_progress:
            return
        mcp_exe = self.find_notebooklm_mcp_executable()
        if not mcp_exe:
            messagebox.showwarning("Server not installed", "Please complete Step 1 first so the MCP server binary exists.")
            return
        self.config_in_progress = True
        self.btn_configure.configure(state="disabled", text="Configuring...")
        thread = threading.Thread(target=self._config_worker, args=(mcp_exe,), daemon=True)
        thread.start()

    def _cleanup_stray_config(self, chosen_path: Path):
        """Remove a minimal config we previously wrote to the standard (wrong,
        for Store editions) location, but only if it holds nothing but our own
        'notebooklm' entry."""
        for d in self._get_claude_data_dirs():
            candidate = d / "claude_desktop_config.json"
            if candidate.resolve() == chosen_path.resolve() or not candidate.is_file():
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                servers = data.get("mcpServers", {})
                if set(data.keys()) == {"mcpServers"} and set(servers.keys()) == {"notebooklm"}:
                    candidate.unlink()
                    self.log_message(f"Removed stray config written earlier to: {candidate}")
            except Exception:
                pass

    def _config_worker(self, mcp_exe: str):
        try:
            config_path = Path(self.get_claude_config_path())
            config_path.parent.mkdir(parents=True, exist_ok=True)
            self._cleanup_stray_config(config_path)

            config: Dict[str, Any] = {}
            if config_path.exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as fh:
                        content = fh.read().strip()
                        config = json.loads(content) if content else {}
                except (json.JSONDecodeError, OSError) as exc:
                    raise RuntimeError(f"Existing config is not valid JSON: {exc}")

            if "mcpServers" not in config or not isinstance(config.get("mcpServers"), dict):
                config["mcpServers"] = {}

            config["mcpServers"]["notebooklm"] = {
                "command": mcp_exe,
                "args": [],
            }

            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)

            self.log_message(f"Claude config updated: {config_path}")
            self.after(0, lambda: self.on_config_complete(True))
        except Exception as exc:
            message = str(exc)
            self.log_message(f"Configuration error: {message}", level="ERROR")
            self.after(0, lambda: self.on_config_complete(False, message))

    def on_config_complete(self, success: bool, error: str = ""):
        self.config_in_progress = False
        self.btn_configure.configure(state="normal", text="3. Configure Claude Desktop")
        if success:
            self.log_message("Step 3 complete: Claude Desktop configured.")
            self.update_status_indicators()
            messagebox.showinfo(
                "Success",
                "Claude Desktop has been configured with the NotebookLM MCP server.\n\n"
                "Please fully restart Claude Desktop for the change to take effect.",
            )
        else:
            messagebox.showerror("Configuration failed", error or "Could not update the Claude configuration.")

    # ---------------------------------------------------------------- shutdown ---
    def on_closing(self):
        if self.auth_process and self.auth_process.poll() is None:
            try:
                self.auth_process.kill()
            except Exception:
                pass
        self.destroy()

    # ------------------------------------------------------------ auto-updater ---
    def _update_settings_path(self) -> Path:
        """Per-user settings file used to persist intentionally skipped updates."""
        home = Path.home()
        if platform.system() == "Windows":
            base = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "ClaudeMCPAssistant"
        elif platform.system() == "Darwin":
            base = home / "Library" / "Application Support" / "ClaudeMCPAssistant"
        else:
            base = home / ".config" / "ClaudeMCPAssistant"
        return base / "settings.json"

    def _load_skip_set(self) -> set:
        try:
            with open(self._update_settings_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return set(data.get("skipped_versions", []))
        except (OSError, json.JSONDecodeError, ValueError):
            return set()

    def _remember_skipped_version(self, version: str):
        try:
            path = self._update_settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            skipped = sorted(self._load_skip_set() | {version})
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"skipped_versions": skipped}, fh, indent=2)
        except OSError as exc:
            self.log_message(f"Could not persist skipped update: {exc}", level="WARNING")

    @staticmethod
    def _version_tuple(value: str) -> Optional[Tuple[int, ...]]:
        value = value.strip().lstrip("vV")
        if not value:
            return None
        try:
            return tuple(int(part) for part in value.split("."))
        except ValueError:
            return None

    @classmethod
    def _is_newer(cls, candidate: str, current: str) -> bool:
        c = cls._version_tuple(candidate)
        cur = cls._version_tuple(current)
        if not c or not cur:
            return False  # never prompt against unparsable versions
        return c > cur

    def _fetch_latest_release(self) -> Dict[str, Any]:
        """Query the repo's latest (stable, non-prerelease) release."""
        url = f"https://api.github.com/repos/{UPDATER_REPO}/releases/latest"
        request = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json", "User-Agent": "claude-mcp-assistant"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        asset_want = _update_asset_name(str(data.get("tag_name", "")).lstrip("vV"))
        checksum_want = asset_want + ".sha256"
        asset_url = checksum_url = None
        for asset in data.get("assets", []):
            if asset.get("name") == asset_want:
                asset_url = asset.get("browser_download_url")
            elif asset.get("name") == checksum_want:
                checksum_url = asset.get("browser_download_url")
        if not asset_url:
            raise RuntimeError(f"No release asset for this platform ({asset_want})")
        return {
            "version": str(data.get("tag_name", "")).lstrip("vV"),
            "notes": data.get("body", "") or "",
            "asset_url": asset_url,
            "checksum_url": checksum_url,
        }

    def _check_for_updates(self, manual: bool = False):
        if APP_VERSION == "dev" and not manual:
            return
        if getattr(self, "_update_state", None) == "running":
            self.log_message("An update check is already running.")
            return
        self._update_state = "running"
        if manual:
            self.btn_updates.configure(state="disabled")
            self.log_message("Checking for updates...")
        threading.Thread(target=self._update_check_worker, args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual: bool):
        def done(status_text: Optional[str], info: Optional[Dict[str, Any]] = None):
            self._update_state = "idle"

            def apply():
                self.btn_updates.configure(state="normal")
                if status_text:
                    self.log_message(status_text)
                if info:
                    self._prompt_update(info)
            self.after(0, apply)

        try:
            release = self._fetch_latest_release()
        except Exception as exc:
            text = f"Update check failed: {exc}" if manual else f"Update check failed (offline or repo unavailable): {exc}"
            done(text, None)
            return

        version = release["version"]
        if self._is_newer(version, APP_VERSION):
            if not manual and version in self._load_skip_set():
                done(f"Update {version} available (previously skipped by you).", None)
                return
            done(f"Update available: v{version} (you are running v{APP_VERSION}).", release)
            return
        if version == APP_VERSION:
            done("You are running the latest version.", None)
        else:  # running a newer build than the latest release (pre-release/source)
            done(f"No update needed (latest released: v{version}; you run v{APP_VERSION}).", None)

    def _prompt_update(self, info: Dict[str, Any]):
        version = info["version"]
        notes = (info["notes"] or "").strip()
        if len(notes) > 600:
            notes = notes[:600].rstrip() + "\n..."
        shown = notes or "This release contains improvements to the assistant."
        proceed = messagebox.askyesno(
            f"Update available: v{version}",
            f"Version v{version} is available (you are running v{APP_VERSION}).\n\n"
            f"{shown}\n\nUpdate now? The assistant will restart by itself.",
        )
        if not proceed:
            remember = messagebox.askyesno(
                "Skip this version",
                f"Remind you about v{version} again next time?",
            )
            if not remember:
                self._remember_skipped_version(version)
                self.log_message(f"Update v{version} skipped (won't be suggested again).")
            else:
                self.log_message(f"Update v{version} skipped.")
            return
        self.log_message(f"Downloading update v{version}...")
        self.btn_updates.configure(state="disabled")
        threading.Thread(target=self._update_download_worker, args=(info,), daemon=True).start()

    def _update_download_worker(self, info: Dict[str, Any]):
        version = info["version"]
        try:
            temp_dir = Path(tempfile.gettempdir()) / "ClaudeMCPAssistantUpdate"
            temp_dir.mkdir(parents=True, exist_ok=True)
            asset_name = _update_asset_name(version)
            target = temp_dir / asset_name
            urllib.request.urlretrieve(info["asset_url"], target)

            if info.get("checksum_url"):
                with urllib.request.urlopen(urllib.request.Request(
                        info["checksum_url"], headers={"User-Agent": "claude-mcp-assistant"}), timeout=30) as resp:
                    expected = resp.read().decode("utf-8").split()[0].strip().lower()
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if digest != expected:
                    raise RuntimeError(f"checksum mismatch (expected {expected[:12]}..., got {digest[:12]}...)")
            else:
                self.log_message("Release has no checksum file; skipping verification (not recommended).", level="WARNING")

            self.log_message(f"Update v{version} downloaded and verified.")
            if platform.system() == "Windows":
                self.after(0, lambda: self._apply_update(target, asset_name))
            else:
                # The mac asset is a zipped .app bundle: extract it now and
                # hand the resolved bundle to the apply step.
                staging = temp_dir / f"{version}-staged"
                extract = subprocess.run(
                    ["ditto", "-x", "-k", str(target), str(staging)],
                    capture_output=True, text=True, timeout=120,
                )
                if extract.returncode != 0:
                    raise RuntimeError(f"unzip failed: {(extract.stderr or '').strip()[:200]}")
                staged_app = next((p for p in staging.rglob("*") if p.name.endswith(".app") and p.is_dir()), None)
                if not staged_app:
                    raise RuntimeError("No .app bundle found inside the downloaded zip")
                self.after(0, lambda: self._apply_update(staged_app, asset_name))
        except Exception as exc:
            message = str(exc)
            self.log_message(f"Update download failed: {message}", level="ERROR")
            self.after(0, lambda: (self.btn_updates.configure(state="normal"),
                                   messagebox.showerror("Update failed", f"Could not download or verify the update.\n\n{message[:400]}")))

    def _apply_update(self, new_binary: Path, asset_name: str):
        """Swap the running executable for the freshly downloaded one."""
        try:
            current = Path(sys.executable)
            if not current.is_file():
                raise RuntimeError("Could not resolve the location of this app.")

            if platform.system() == "Windows":
                self._apply_update_windows(new_binary, current)
            else:
                self._apply_update_posix(new_binary, current)
        except Exception as exc:
            self.log_message(f"Applying update failed: {exc}", level="ERROR")
            messagebox.showerror("Update failed", str(exc))
            self.btn_updates.configure(state="normal")

    def _apply_update_windows(self, new_binary: Path, current: Path):
        """Windows can't overwrite a running .exe, so a detached PowerShell
        helper waits for this process to exit, swaps the files, restarts us."""
        backup = current.with_name(current.name + ".old.bak")
        helper_script = Path(tempfile.gettempdir()) / "claude_mcp_update.ps1"
        helper_script.write_text(
            f"$current = '{current}'\n"
            f"$new = '{new_binary}'\n"
            f"$backup = '{backup}'\n"
            f"$targetPid = {os.getpid()}\n"
            "while (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 300 }\n"
            "if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }\n"
            "Move-Item -LiteralPath $current -Destination $backup -Force\n"
            "Move-Item -LiteralPath $new -Destination $current -Force\n"
            "Start-Sleep -Seconds 1\n"
            "Start-Process -FilePath $current\n",
            encoding="utf-8",
        )
        env_copy = os.environ.copy()
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(helper_script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            env=env_copy,
        )
        self.log_message("Update staged; closing app so the new version can take its place...")
        self.after(600, self.on_closing)

    def _apply_update_posix(self, staged: Path, current: Path):
        """Swap the installed .app bundle for the freshly downloaded one.

        POSIX allows replacing files that are currently running. If this app
        somehow runs outside a bundle (bare binary), fall back to replacing
        just the executable.
        """
        bundle = next((p for p in current.parents if p.name.endswith(".app")), None)
        if bundle:
            parent = bundle.parent
            installed = parent / bundle.name
            shutil.rmtree(bundle)
            shutil.move(str(staged), str(installed))
            self.log_message("Update installed; restarting...")
            subprocess.Popen(["open", "-a", str(installed)], start_new_session=False)
            self.after(600, self.on_closing)
        else:
            staged_binary = current.with_name(current.name + ".new")
            shutil.copyfile(staged, staged_binary)
            os.chmod(staged_binary, 0o755)
            os.replace(staged_binary, current)
            self.log_message("Update installed; restarting...")
            subprocess.Popen([str(current)], start_new_session=True)
            self.after(600, self.on_closing)


def main():
    app = MCPIntegrationApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
