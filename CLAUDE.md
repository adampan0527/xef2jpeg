# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

XEF2JPEG is a Windows desktop application that converts .XEF files (captured by Kinect V2) to JPEG format. The application is built using the long-running agent harness framework for incremental, context-window-spanning development.

**Target Platform:** Windows 10 & Windows 11
**Python Environment:** uv (fast Python package manager)
**Input Directory:** `XEF2JPEG_Input/`
**Output Directory:** `XEF2JPEG_Output/`

## Architecture

### Harness Framework

This project uses a two-agent architecture for development:

1. **Initializer Agent** - Runs once to set up project structure, feature list, and development environment
2. **Coding Agent** - Runs in subsequent sessions to implement one feature at a time

### Key Files

| File | Purpose |
|------|---------|
| `INITIALIZER_PROMPT.md` | Instructions for the first agent session |
| `CODING_AGENT_PROMPT.md` | Instructions for subsequent coding sessions |
| `feature_list.json` | Comprehensive feature requirements (created by initializer) |
| `init.sh` | Development server and testing script (created by initializer) |
| `claude-progress.txt` | Progress log updated each session |
| `XEF2JPEG_Input/` | Directory for input .XEF files |
| `XEF2JPEG_Output/` | Directory for converted JPEG output |

### Application Features

The GUI application should provide:
- File picker dialog to select .XEF files (default path: current working directory)
- Output path configuration
- Start conversion button
- Progress indication during conversion

## Development Workflow

### Environment Setup

This project uses **uv** for Python package management (faster alternative to pip/venv).

```bash
# Install uv (if not already installed)
# Windows (PowerShell):
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Create virtual environment with uv
uv venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate

# Install dependencies with uv
uv pip install -r requirements.txt

# Run the application
python xef2jpeg.py
```

### Starting a New Project (Initializer Session)

1. Provide `INITIALIZER_PROMPT.md` with project requirements
2. The agent will create:
   - Project structure and dependencies
   - `feature_list.json` with comprehensive testable features
   - `init.sh` script for development and testing
   - Git repository with initial commit
   - `claude-progress.txt` for progress tracking

### Subsequent Development Sessions (Coding Agent)

1. Provide `CODING_AGENT_PROMPT.md`
2. The agent will:
   - Read `claude-progress.txt` and git history
   - Run `init.sh` to start development environment
   - Verify existing functionality
   - Implement ONE feature from `feature_list.json`
   - Test thoroughly end-to-end
   - Commit changes and update progress

### Session Startup Checklist

Every coding session must:
1. Read `claude-progress.txt` for current state
2. Review git history: `git log --oneline -20`
3. Read `feature_list.json` for available features
4. Run `bash init.sh` to start development server
5. Verify basic functionality before implementing new features

## Feature List Management

### Format

```json
{
  "features": [
    {
      "id": "feat-001",
      "category": "functional|ui|error-handling|accessibility|performance",
      "description": "Clear, testable description",
      "steps": [
        "Step 1: Navigate to...",
        "Step 2: Perform action",
        "Step 3: Verify result"
      ],
      "passes": false,
      "priority": "high|medium|low"
    }
  ]
}
```

### Rules

- **Only change `"passes"` from `false` to `true`** after thorough testing
- **Never remove or edit features** - this could lead to missing functionality
- **Work on one feature at a time** - incremental progress
- **Test end-to-end** before marking as passing

## Testing Principles

### For Desktop Applications

- Test as a human user would interact with the GUI
- Verify file selection dialogs work correctly
- Test conversion with sample .XEF files from `XEF2JPEG_Input/`
- Verify output JPEG files are created in `XEF2JPEG_Output/`
- Test error cases (invalid files, missing permissions, etc.)

### Before Marking Features as Passing

1. Read the feature steps from `feature_list.json`
2. Follow each step as a user would
3. Verify the feature works at each step
4. Test edge cases where applicable
5. Only then change `"passes"` to `true`

## Error Recovery

- Use git to revert bad changes: `git revert` or `git reset`
- Always verify basic functionality before implementing new features
- Fix existing bugs before adding new features
- Leave environment in a clean, working state

## Progress Documentation

After each session, update `claude-progress.txt`:

```
SESSION N - Coding Agent
-----------------------
Date: YYYY-MM-DD
Worked on: [Feature description]
Status: Implemented and tested
Features now passing: X / Y

Changes made:
- [Brief description]

Next session should:
- [Suggestion]
```

## Common Failure Modes

| Problem | Solution |
|---------|----------|
| Declares victory too early | Read `feature_list.json`, work on one feature at a time |
| Leaves environment broken | Test basic functionality before new features |
| Marks features done prematurely | Test end-to-end before changing `passes` to `true` |
| Wastes time on setup | `init.sh` contains all startup instructions |
| Removes/edits features | Only change the `passes` field |

## When to Stop a Session

Stop when:
- At least one feature is implemented and tested
- Code is in a clean, working state
- Progress is committed and documented
- Environment is ready for next session

It's better to leave a clean environment than to push too far and break something.

## XEF File Handling

.XEF files are Kinect v2 sensor data files. The conversion process should:
- Read binary .XEF format data
- Extract color/depth frames
- Convert to JPEG format
- Handle multi-frame sequences appropriately
- Preserve metadata where applicable

## References

- [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) - Anthropic Engineering Blog
- [Kinect for Windows SDK 2.0](https://developer.microsoft.com/en-us/windows/kinect/) - Kinect v2 development resources
