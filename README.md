# Claude MCP Integration Assistant

A clean, cross-platform Desktop GUI Installer Application that automates the installation, token authentication, and configuration of the Google NotebookLM MCP Server for Anthropic's Claude Desktop.

## Features

- **Modern UI**: Built with CustomTkinter for a clean, professional interface
- **3-Step Automated Workflow**: Install dependencies, authenticate Google account, and connect to Claude Desktop
- **Cross-Platform**: Supports Windows and macOS
- **Defensive Error Handling**: Handles corrupted JSON, missing files, path discovery, and more
- **Non-Blocking Operations**: All long-running operations run in background threads to keep UI responsive
- **Status Monitoring**: Real-time validation of Node/Python, packages, auth tokens, and Claude configuration
- **Automatic Updates**: Detects newer releases on GitHub and offers a one-click self-update (Windows + macOS)

## Requirements

- Python 3.8+ (to run from source; the bundled executable needs nothing)
- Internet connection
- Google account with access to NotebookLM
- Claude Desktop installed

## Installation

### Downloads (recommended for end users)

Grab the latest package for your platform from the
[Releases page](https://github.com/DeikuModder/claude-mcp-integration-assistant/releases/latest):

- `ClaudeMCPIntegrationAssistant-<version>-windows.exe`
- `ClaudeMCPIntegrationAssistant-<version>-macos` (Apple Silicon)

On macOS, if Gatekeeper blocks the app because it is unsigned, right-click the
file and choose **Open** once; after that it launches normally.

### Development

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```
3. Run the application:
```bash
python main.py
```

### Build Executable

To compile into a single executable using PyInstaller:

```bash
pyinstaller --name "ClaudeMCPIntegrationAssistant" --windowed --onefile main.py
```

## Usage

1. **Step 1 - Install Server Dependencies**: Click "1. Install Server Dependencies" to install `notebooklm-mcp-cli`. The app tries `uv tool install`, then `uv pip install --system`, then `pip`, then `python -m pip`. It detects an existing installation and disables the button instead of reinstalling.
2. **Step 2 - Link Google Account**: Click "2. Link Google Account". The app runs `nlm login`, which opens a browser window for Google authentication. Log in, authorize, and return to the app. If valid credentials already exist, login is skipped.
3. **Step 3 - Connect to Claude Desktop**: Click "3. Connect to Claude Desktop" to automatically detect and inject the configuration into `claude_desktop_config.json`. Existing configurations are preserved.

## Requirements notes

- The app itself is a self-contained executable, so the machine does **not** need Python to *run* the assistant.
- The MCP server needs either `uv` or Python + `pip`. If neither is found, the assistant offers to install `uv` automatically (uv can provision its own Python), or shows manual instructions.
- `nlm login` (check with `nlm login --check`); the MCP server binary is `notebooklm-mcp`.

## Releases and automatic updates

The release pipeline (`.github/workflows/release.yml`) runs **automatically**:

1. **Bump**: change whatever you need; there is no separate version constant to
   edit — the version comes from the git tag.
2. **Tag and push**:
   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. CI builds the Windows `.exe` and the macOS (Apple Silicon) binary on
   GitHub-hosted runners, writes the tag into the bundled `version.txt`, and
   publishes a GitHub Release with both binaries plus `.sha256` checksum files.
4. Every installed app checks `releases/latest` **once per launch** (and on
   demand through the "Check for updates" button). When a newer version is
   found, the app shows a dialog with the release notes and — after agreeing —
   verifies the SHA-256 checksum, replaces its own executable, and restarts.
5. Heal path: Windows swaps the running `.exe` via a detached helper script
   (previous file kept as `.old.bak`); macOS uses an atomic rename.
   If you skip a version, it is remembered in
   `%APPDATA%\ClaudeMCPAssistant\settings.json` (Windows) or
   `~/Library/Application Support/ClaudeMCPAssistant/settings.json` (macOS)
   and that version is never suggested again.

Manual non-publishing test builds: run the **Release** workflow from the
Actions tab (workflow_dispatch) with any test tag; artifacts upload but no
release is created.
