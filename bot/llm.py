"""The single OpenAI call that does the edit."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import openai
from openai import AsyncOpenAI

from bot import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8")

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```\s*$", re.DOTALL)

_client: AsyncOpenAI | None = None


class HumanizeError(Exception):
    """A failure worth showing to the Telegram user as-is."""


@dataclass
class EditResult:
    text: str
    truncated: bool


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.require("OPENAI_API_KEY"), timeout=config.OPENAI_TIMEOUT_SECONDS)
    return _client


async def edit_script(script: str, system_prompt: str = SYSTEM_PROMPT) -> EditResult:
    kwargs = {}
    if config.OPENAI_TEMPERATURE is not None:
        kwargs["temperature"] = config.OPENAI_TEMPERATURE
    try:
        response = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": script},
            ],
            **kwargs,
        )
    except openai.AuthenticationError as exc:
        raise HumanizeError("OpenAI rejected the API key. Check OPENAI_API_KEY.") from exc
    except openai.RateLimitError as exc:
        raise HumanizeError("OpenAI rate limit hit or the account is out of credit. Try again in a minute, "
                            "or check billing at platform.openai.com.") from exc
    except openai.BadRequestError as exc:
        hint = " (If it mentions temperature, set OPENAI_TEMPERATURE to empty.)" if "temperature" in str(exc) else ""
        raise HumanizeError(f"OpenAI rejected the request: {exc.message}{hint}") from exc
    except openai.APITimeoutError as exc:
        raise HumanizeError("OpenAI took too long to answer. Try again.") from exc
    except openai.APIConnectionError as exc:
        raise HumanizeError("Couldn't reach OpenAI. Try again in a moment.") from exc
    except openai.APIStatusError as exc:
        raise HumanizeError(f"OpenAI returned an error ({exc.status_code}). Try again.") from exc

    choice = response.choices[0]
    if choice.message.refusal:
        raise HumanizeError(f"The model refused to edit this script: {choice.message.refusal}")
    if choice.finish_reason == "content_filter":
        raise HumanizeError("OpenAI's content filter blocked the output.")

    text = (choice.message.content or "").strip()
    if fenced := _FENCE_RE.match(text):
        text = fenced.group(1).strip()
    if not text:
        raise HumanizeError("The model returned an empty script. Try again.")

    usage = response.usage
    if usage:
        logger.info("OpenAI %s: %s in / %s out tokens", config.OPENAI_MODEL, usage.prompt_tokens, usage.completion_tokens)
    return EditResult(text=text, truncated=choice.finish_reason == "length")
