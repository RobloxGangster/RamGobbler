# Ram Gobbler (Batch 2)

Windows memory gobbler with a clean, friendly UI.

## Features
- Light/Dark theme, system tray mode
- Start with Windows (HKCU Run)
- Add targets from running processes, per-target Gobble/Stop
- Global: Gobble Visible Targets Now (toolbar + tray + optional global hotkey)
- Per-target reclaimed MB counters + history, session/day totals + global session total
- Safe process guard (hide/protect system processes)
- Profiles: Balanced (15s) / Aggressive (8s) / Gentle (30s)
- Import/Export settings + auto-backups
- **Smarter gobbles**: adaptive intervals, pressure-aware, per-target rules (CPU/background/fullscreen/strategy), Gaming Mode, Battery Saver
- Optional Gobble on wake from sleep
- Richer matching: match by name, path, command line substring, or parent process name with live preview
- Optional elevated helper service for gobbling processes that require Administrator rights
- Notifications with optional thresholds and daily recap
- Anti-thrash controls (cooldown timers and reset caps)
- Headless CLI mode with gobble and metrics export options

## Architecture

- `ram_gobbler_app/core/` – immutable domain models, configuration
  serialization, scheduling helpers and shared palette maths.
- `ram_gobbler_app/services/` – process discovery, gobble execution,
  scheduler orchestration and metrics persistence with debounced disk writes.
- `ram_gobbler_app/ui/` – Qt presentation layer that listens to scheduler
  events via thread-safe hooks and triggers manual actions.
- `ram_gobbler_app/cli/` – command-line bootstrap shared by the GUI launcher
  and the `--headless` mode.

Process snapshots are cached once per tick to avoid repeated psutil scans and
configuration mutations are validated through the new `ConfigRepository`.

## Requirements
```
pip install -r requirements.txt
```

## Run
```
python ram_gobbler.py
```

### CLI

Ram Gobbler can run without the GUI using `--headless` and one-shot flags:

```
python ram_gobbler.py --headless --gobble-visible --export-metrics metrics.json
```

Available flags:

- `--gobble-visible` – gobble all visible targets once
- `--gobble-group <name>` – gobble a specific group once
- `--export-metrics <path>` – write aggregated metrics JSON to the given path

### Optional elevated helper
Some processes can only be gobbled with Administrator rights. An auxiliary helper service can perform these gobbles:

```
python gobble_helper.py  # run as Administrator
```

The GUI will connect via a named pipe and fall back to standard gobbling with a non-blocking hint if the helper isn't running.

## Build (optional)
```
pip install pyinstaller
pyinstaller -F -w -n "RamGobbler" ram_gobbler.py
```

## Notes
- Gobbling may require Administrator privileges for some processes.
- The Reset strategy (advanced) is more forceful; we fall back to standard if it fails.


### Crash log
If the app crashes on startup, a traceback is written to `%APPDATA%/RamGobbler/error.log`.

## Development

Run the automated tests with `pytest`. The Qt layer falls back to Python stubs
so the suite runs in headless environments. The CLI and GUI share the same
bootstrap sequence, ensuring both code paths are exercised in the tests.
