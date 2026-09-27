"""
CLI: python -m sep_engine [--input request.json] [--completed "CS 1" "ENGL 1A"] [--summer] [--out plan.json]

Without --input it runs on the built-in mock data (Chabot -> UC Berkeley -> CS).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import mock_data
from .errors import SEPError
from .models import SEPRequest
from .pipeline import generate_sep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sep_engine", description="Generate SEP plans as JSON.")
    ap.add_argument("--input", help="JSON file shaped like SEPRequest {catalog, agreement, profile, plans}.")
    ap.add_argument("--completed", nargs="*", help="Override the student's completed courses.")
    ap.add_argument("--summer", action="store_true", help="Allow summer terms.")
    ap.add_argument("--out", help="Write JSON here instead of stdout.")
    args = ap.parse_args(argv)

    request = (SEPRequest.model_validate_json(Path(args.input).read_text(encoding="utf-8"))
               if args.input else mock_data.mock_request())
    updates = {}
    if args.completed is not None:
        updates["completed_courses"] = args.completed
    if args.summer:
        updates["include_summer"] = True
    profile = request.profile.model_copy(update=updates)

    try:
        result = generate_sep(request.catalog, request.agreement, profile, request.plans)
    except SEPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
