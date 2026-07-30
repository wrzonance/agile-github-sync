#!/usr/bin/env python3
"""Entry point for the GitHub -> AgilePlace sync: `python sync.py [--apply]`.

The orchestration itself lives in `agilesync/sync.py`; this stays at the repo root so the documented
command, the Windows scheduled task, and any existing cron line keep working unchanged after the
modules moved into the `agilesync` package.
"""
from datetime import datetime
from agilesync.sync import main

now = datetime.now()

print(now)

if __name__ == "__main__":
    main()
