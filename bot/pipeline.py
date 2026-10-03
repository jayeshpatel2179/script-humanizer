"""protect → edit → restore → scan. Shared by the Telegram bot and the CLI."""

from dataclasses import dataclass

from bot import config
from bot.llm import edit_script
from bot.protect import protect, restore, strip_placeholders
from bot.scan import build_report, compare


@dataclass
class HumanizeResult:
    text: str
    report: str
    truncated: bool = False


async def humanize(script: str) -> HumanizeResult:
    script = script.replace("\r\n", "\n").strip()
    protected = protect(script)
    edited = await edit_script(protected.text)
    restored = restore(edited.text, protected)

    result = compare(
        strip_placeholders(protected.text),
        strip_placeholders(edited.text),
        length_tolerance=config.LENGTH_TOLERANCE,
    )
    report = build_report(
        result,
        protected_total=len(protected.blocks),
        reinserted=restored.reinserted,
        duplicated=restored.duplicated,
        truncated=edited.truncated,
    )
    return HumanizeResult(text=restored.text.strip() + "\n", report=report, truncated=edited.truncated)
