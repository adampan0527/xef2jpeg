# XEF2JPEG Converter

A Windows desktop application for converting Kinect V2 .XEF files to JPEG format.

## Project Overview

**XEF2JPEG** is a GUI application that converts .XEF files captured by Kinect V2 sensors to JPEG image format. The application runs on Windows 10 and Windows 11.

### Features

- Select .XEF input files via file picker dialog
- Configure output directory for converted JPEG files
- Convert XEF files to JPEG format
- Progress indication during conversion
- Default file picker opens to current working directory

### Directories

- `XEF2JPEG_Input/` - Input .XEF files directory
- `XEF2JPEG_Output/` - Converted JPEG output directory

## Development with Agent Harness

This project uses the long-running agent harness architecture for development.

### First Session (Initializer Agent)

Provide `INITIALIZER_PROMPT.md` with project requirements. The agent will:
- Set up project structure
- Create `feature_list.json` with comprehensive features
- Create `init.sh` for development and testing
- Initialize git repository
- Create `claude-progress.txt` for progress tracking

### Subsequent Sessions (Coding Agent)

Provide `CODING_AGENT_PROMPT.md`. The agent will:
- Read progress files and git history
- Start development server
- Verify basic functionality
- Implement ONE feature from `feature_list.json`
- Test thoroughly end-to-end
- Commit changes and update progress

## Requirements

- Windows 10 or Windows 11
- Python 3.8+
- Kinect for Windows SDK 2.0 (for XEF file handling)
- Pillow library (for image processing)

## Installation

```bash
# Create virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Running the Application

```bash
python xef2jpeg.py
```

## Key Files

| File | Purpose |
|------|---------|
| `xef2jpeg.py` | Main application entry point |
| `requirements.txt` | Python dependencies |
| `INITIALIZER_PROMPT.md` | Instructions for first agent session |
| `CODING_AGENT_PROMPT.md` | Instructions for coding sessions |
| `feature_list.json` | Feature requirements (created by initializer) |
| `init.sh` | Development script (created by initializer) |
| `claude-progress.txt` | Progress log |
| `CLAUDE.md` | Claude Code guidance documentation |

## Architecture

This is a tkinter-based desktop application with:
- File selection dialogs using native Windows dialogs
- Progress tracking during conversion
- Input validation and error handling
- Configurable input/output paths

## License

MIT
