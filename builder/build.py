"""Contest entry: python -B builder/build.py brief.md tool_api.json [options]."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    # Avoid shadowing stdlib calendar with adjacent builder/calendar.py.
    sys.path = [entry for entry in sys.path if Path(entry).resolve() != Path(__file__).resolve().parent]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    case_mode=bool(args and Path(args[0]).is_dir()) or (len(args)>1 and args[1].startswith(('https://','http://')))
    if case_mode:
        from orchestration.case_build import main as entry
    else:
        from orchestration.contest_build import main as entry
    return entry(args)

if __name__ == '__main__':
    raise SystemExit(main())
