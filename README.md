# Claude MCP Integration Assistant

A clean, cross-platform Desktop GUI Installer Application that automates the installation, token authentication, and configuration of the Google NotebookLM MCP Server for Anthropic's Claude Desktop.

## Features

- **Modern UI**: Built with CustomTkinter for a clean, professional interface
- **3-Step Automated Workflow**: Install dependencies, authenticate Google account, and connect to Claude Desktop
- **Cross-Platform**: Supports Windows and macOS
- **Defensive Error Handling**: Handles corrupted JSON, missing files, path discovery, and more
- **Non-Blocking Operations**: All long-running operations run in background threads to keep UI responsive
- **Status Monitoring**: Real-time validation of Node/Python, packages, auth tokens, and Claude configuration

## Requirements

- Python 3.8+
- Internet connection
- Google account with access to NotebookLM
- Claude Desktop installed

## Installation

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
- Auth command is `nlm login` (check with `nlm login --check`); the MCP server binary is `notebooklm-mcp`.
