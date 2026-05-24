# Scripts Directory

This directory contains utility scripts for the Telegram Toolkit.

---

## Available Scripts

### configure_performance.py
**Purpose:** Quick configuration tool for performance settings

**Usage:**
```bash
python scripts/configure_performance.py
```

**Features:**
- Configure profile photo reconciliation mode (off/daily/always)
- Configure reconciliation strategy (quick/deep)
- Interactive menu for easy configuration
- Updates `.env` file automatically

**Modes:**
1. **Maximum Speed** - `reconcile=off` (Recommended for most users)
2. **Balanced** - `reconcile=daily, strategy=quick` (Good balance)
3. **Data Integrity** - `reconcile=always, strategy=deep` (Thorough verification)
4. **Custom** - Set your own values

---

### detect_dead_code.py
**Purpose:** Static analysis tool to detect unused code

**Usage:**
```bash
python scripts/detect_dead_code.py
```

**Features:**
- Detects unused imports
- Detects unused variables
- Detects unused functions
- Helps maintain code quality

---

## Adding New Scripts

When adding new utility scripts:

1. Place them in this `scripts/` directory
2. Add a shebang line: `#!/usr/bin/env python3`
3. Add a docstring explaining the purpose
4. Update this README with usage instructions
5. Make sure the script can be run from the project root

---

## Running Scripts

All scripts should be run from the project root directory:

```bash
# From project root
python scripts/script_name.py
```

Or make them executable (Unix/Linux/Mac):

```bash
chmod +x scripts/script_name.py
./scripts/script_name.py
```

---

**Last Updated:** 2026-04-26
