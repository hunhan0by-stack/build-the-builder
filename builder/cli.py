from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import FoundationError
from .pipeline import build, verify_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the Builder: offline foundation only; no API-key option or network transport")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_command = commands.add_parser("build", help="Extract explicit local sources and produce reviewable artifacts")
    compile_command.add_argument("--brief", action="append", required=True)
    compile_command.add_argument("--tool-api", required=True)
    compile_command.add_argument("--context", action="append", default=[])
    compile_command.add_argument("--cases")
    compile_command.add_argument("--out", required=True)
    compile_command.add_argument("--deadline-seconds", type=int, default=120)
    check_command = commands.add_parser("verify", help="Verify hashes of existing local artifacts")
    check_command.add_argument("directory")
    practice_command = commands.add_parser("practice", help="Use the existing downloaded practice kit and local research notes")
    practice_command.add_argument("--profile", choices=("A", "B"), required=True)
    practice_command.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "practice":
            kit = Path("data/freya-hackathon/kit-v3/build-the-builder")
            briefs = [kit / "briefs/brief-a.md"] + ([kit / "briefs/brief-b.md"] if args.profile == "B" else [])
            contexts = [kit / "docs/workflow-format.md", kit / "docs/tools-and-code.md", kit / "docs/api.md",
                        Path("data/freya-hackathon/kickoff-notlar.txt"),
                        Path("data/research/2609.04611v1/FREYA-YAPI-TASARIMI.txt"),
                        Path("data/research/2609.04611v1/MAKALE-DETAYLI-ANALIZ.txt")]
            result = build(brief_paths=briefs, tool_api_path=kit / "tools/openapi.json", output_path=args.out,
                           context_paths=contexts, cases_path=kit / "public-test-set.json")
        elif args.command == "build":
            result = build(brief_paths=args.brief, tool_api_path=args.tool_api, output_path=args.out,
                           context_paths=args.context, cases_path=args.cases, deadline_seconds=args.deadline_seconds)
        else:
            result = verify_run(args.directory)
        if args.command in {"build", "practice"}:
            summary = {key: result[key] for key in ("mode", "requirement_blocks", "parameter_facts",
                       "local_structure_valid", "semantic_completion", "publish_allowed", "network_calls", "api_key_read")}
        else:
            summary = result
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (FoundationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Offline build stopped: {exc}", file=sys.stderr)
        return 2
