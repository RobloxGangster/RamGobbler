# Ram Gobbler (Batch 2)

Windows memory trimmer with a clean, friendly UI.

## Features
- Light/Dark theme, system tray mode
- Start with Windows (HKCU Run)
- Add targets from running processes, per-target Gobble/Stop
- Global: Trim Visible Targets Now (toolbar + tray + optional global hotkey)
- Per-target reclaimed MB counters + history, session/day totals + global session total
- Safe process guard (hide/protect system processes)
- Profiles: Balanced (15s) / Aggressive (8s) / Gentle (30s)
- Import/Export settings + auto-backups
- **Smarter trims**: adaptive intervals, pressure-aware, per-target rules (CPU/background/fullscreen/strategy), Gaming Mode, Battery Saver
- Optional Trim on wake from sleep

## Requirements
```
pip install -r requirements.txt
```

## Run
```
python ram_gobbler.py
```

## Build (optional)
```
pip install pyinstaller
pyinstaller -F -w -n "RamGobbler" ram_gobbler.py
```

## Notes
- Trimming may require Administrator privileges for some processes.
- The Reset strategy (advanced) is more forceful; we fall back to standard if it fails.
