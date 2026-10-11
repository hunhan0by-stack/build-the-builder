"""Two-file offline judge entry for bounded source-derived native graph builds.

Each invocation gets a fresh output child. This wrapper neither publishes nor
retries platform/bank calls, and local verification grants no provider authority.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import uuid

# A direct script invocation imports the adjacent package, without environment
# path configuration or replacing any production ROOT.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestration.build_the_builder import OUTPUT, main as build_main


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline Build the Builder: one Markdown brief and one tool API.')
    parser.add_argument('brief_path')
    parser.add_argument('tool_api_path')
    args = parser.parse_args(argv)
    output = OUTPUT / ('jury-' + uuid.uuid4().hex)
    return build_main(['--brief', args.brief_path, '--tool-api', args.tool_api_path,
        '--native-mode', 'official', '--native-runtime-mode', 'official_bank_session',
        '--out', str(output), '--deadline-seconds', '1800'])


if __name__ == '__main__':
    raise SystemExit(main())
