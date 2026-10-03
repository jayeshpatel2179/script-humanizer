"""Run the humanizer locally without Telegram.

    python -m bot.cli script.txt                 # humanize → script_humanized.txt + report
    python -m bot.cli script.txt --scan-only     # just measure the tics, no API call
"""

import argparse
import asyncio
import sys
from pathlib import Path

from bot.protect import protect, strip_placeholders
from bot.scan import analyze, build_analysis_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Humanize a video script.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="default: <input>_humanized.txt")
    parser.add_argument("--scan-only", action="store_true", help="report tics in the input without calling OpenAI")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    script = args.input.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    if args.scan_only:
        protected = protect(script)
        print(f"Protected lines: {len(protected.blocks)}")
        print(build_analysis_report(analyze(strip_placeholders(protected.text))))
        return

    from bot.llm import HumanizeError
    from bot.pipeline import humanize

    try:
        result = asyncio.run(humanize(script))
    except HumanizeError as exc:
        sys.exit(f"Error: {exc}")
    output = args.output or args.input.with_name(f"{args.input.stem}_humanized.txt")
    output.write_text(result.text, encoding="utf-8")
    print(result.report)
    print(f"\nSaved: {output}")


if __name__ == "__main__":
    main()
