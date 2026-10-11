"""Contest entry: python -B builder/build.py brief.md tool_api.json [options]."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    # Avoid shadowing stdlib calendar with adjacent builder/calendar.py.
    sys.path = [entry for entry in sys.path if Path(entry).resolve() != Path(__file__).resolve().parent]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestration.contest_build import main

if __name__ == '__main__':
    raise SystemExit(main())
