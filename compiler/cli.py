from __future__ import annotations

import argparse
import json
from pathlib import Path

from builder.ingest import PROTECTED_DIRECTORIES, read_source
from builder.models import FoundationError, strict_json

from .core import compile_sources
from .providers import CannedProvider, JsonEndpointProvider, ProviderConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description="Source-checked decision IR; no platform/bank calls")
    parser.add_argument("--brief", action="append", type=Path, required=True)
    parser.add_argument("--tool-api", type=Path, required=True)
    parser.add_argument("--evaluation-only", action="append", type=Path, default=[])
    providers = parser.add_mutually_exclusive_group(required=True)
    providers.add_argument("--canned-response", type=Path)
    providers.add_argument("--provider-config", type=Path)
    parser.add_argument("--allow-model-call", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = args.out.resolve()
        if not output.is_relative_to(Path.cwd().resolve()) or set(p.lower() for p in output.parts) & PROTECTED_DIRECTORIES:
            raise FoundationError("Output must remain within a nonprotected workspace path")
        if output.exists():
            raise FoundationError("Existing compilation evidence must not be overwritten")
        if args.canned_response:
            if args.allow_model_call:
                raise FoundationError("Canned provider does not accept model-call permission")
            provider = CannedProvider(read_source(args.canned_response).text)
        else:
            config = strict_json(read_source(args.provider_config).text)
            provider = JsonEndpointProvider(ProviderConfig(**config), allow_model_call=args.allow_model_call)
        result = compile_sources(policy_paths=args.brief, tool_api_path=args.tool_api, provider=provider,
                                 evaluation_paths=args.evaluation_only)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps(result["validation"]))
        return 0
    except (FoundationError, OSError, TypeError, ValueError):
        print("Compilation blocked: input, evidence, unresolved decision or provider error")
        return 2
