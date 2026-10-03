"""Google Docs access for Doc mode: read the Doc as plain text, replace its body.

Uses a service account (the Doc must be shared with its email as Editor).
The Google client is synchronous, so calls run in a worker thread.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from bot import config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/documents"]

# Characters the Docs API won't accept in inserted text (everything below 0x20 except \n and \t).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_TEXT_STYLE_FIELDS = ",".join([
    "bold", "italic", "underline", "strikethrough", "smallCaps", "backgroundColor",
    "foregroundColor", "fontSize", "weightedFontFamily", "baselineOffset", "link",
])

_service = None


class DocError(Exception):
    """A Doc read/write failure worth showing to the user as-is."""


@dataclass
class DocSnapshot:
    text: str  # plain text, normalised
    revision_id: str
    end_index: int  # body end index (includes the final newline Docs always keeps)


def doc_url(doc_id: str | None = None, tab_id: str | None = None) -> str:
    return f"https://docs.google.com/document/d/{doc_id or config.GOOGLE_DOC_ID}/edit?tab={tab_id or config.GOOGLE_DOC_TAB_ID}"


def _get_service():
    global _service
    if _service is None:
        if config.GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = service_account.Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _service = build("docs", "v1", credentials=creds, cache_discovery=False)
    return _service


def normalise(text: str) -> str:
    """Plain-text form used for word counts, hashing and the post-write comparison."""
    text = text.replace("\r\n", "\n").replace("\u000b", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _extract(elements: list[dict]) -> str:
    parts: list[str] = []
    for element in elements:
        if "paragraph" in element:
            for run in element["paragraph"].get("elements", []):
                parts.append(run.get("textRun", {}).get("content", ""))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    parts.append(_extract(cell.get("content", [])))
        elif "tableOfContents" in element:
            parts.append(_extract(element["tableOfContents"].get("content", [])))
    return "".join(parts)


def _utf16_len(text: str) -> int:
    # Docs indexes count UTF-16 code units, so emoji count as 2.
    return len(text.encode("utf-16-le")) // 2


def _explain(exc: HttpError) -> str:
    status = exc.resp.status if exc.resp is not None else None
    detail = getattr(exc, "reason", "") or str(exc)
    if status == 403:
        return ("Google refused access. Check the Doc is shared with the service account email as Editor "
                "and the Google Docs API is enabled.")
    if status == 404:
        return "Doc not found. Check GOOGLE_DOC_ID."
    if status == 400 and "revision" in detail.lower():
        return "The Doc was edited while I was working, so I didn't overwrite it. Run go humanize again."
    return f"Google Docs error ({status}): {detail}"


def _find_tab(tabs: list[dict], tab_id: str) -> dict | None:
    for tab in tabs:
        if tab.get("tabProperties", {}).get("tabId") == tab_id:
            return tab
        if found := _find_tab(tab.get("childTabs", []), tab_id):
            return found
    return None


def _read_sync(doc_id: str, tab_id: str) -> DocSnapshot:
    # includeTabsContent=True returns every tab under "tabs" (and no top-level "body"),
    # so the configured tab is picked explicitly instead of relying on "first tab".
    try:
        doc = _get_service().documents().get(documentId=doc_id, includeTabsContent=True).execute(num_retries=2)
    except HttpError as exc:
        raise DocError(_explain(exc)) from exc
    tab = _find_tab(doc.get("tabs", []), tab_id)
    if tab is None:
        raise DocError(f"Tab {tab_id} not found in the Doc. Check GOOGLE_DOC_TAB_ID.")
    content = tab["documentTab"]["body"]["content"]
    return DocSnapshot(text=normalise(_extract(content)), revision_id=doc["revisionId"], end_index=content[-1]["endIndex"])


def _replace_sync(doc_id: str, tab_id: str, new_text: str, snapshot: DocSnapshot) -> None:
    new_text = _CONTROL_RE.sub("", new_text).rstrip("\n")
    length = _utf16_len(new_text)
    requests: list[dict] = []
    # The body always ends with one newline that can't be deleted, so the range stops before it.
    if snapshot.end_index - 1 > 1:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": snapshot.end_index - 1,
                                                           "tabId": tab_id}}})
    if length:
        text_range = {"startIndex": 1, "endIndex": 1 + length, "tabId": tab_id}
        requests += [
            {"insertText": {"location": {"index": 1, "tabId": tab_id}, "text": new_text}},
            # Inserted paragraphs inherit the surviving paragraph's style - reset to plain text.
            {"updateParagraphStyle": {"range": text_range, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                      "fields": "namedStyleType"}},
            {"deleteParagraphBullets": {"range": text_range}},
            {"updateTextStyle": {"range": text_range, "textStyle": {}, "fields": _TEXT_STYLE_FIELDS}},
        ]
    if not requests:
        return  # empty text into an already-empty tab: nothing to do (Google rejects an empty batch)
    body = {"requests": requests, "writeControl": {"requiredRevisionId": snapshot.revision_id}}
    try:
        # One batchUpdate: Google applies all of it or none of it. No retries - a retried
        # write after an unclear failure would hit a changed revision anyway.
        _get_service().documents().batchUpdate(documentId=doc_id, body=body).execute()
    except HttpError as exc:
        raise DocError(_explain(exc)) from exc


async def read_doc() -> DocSnapshot:
    return await asyncio.to_thread(_read_sync, config.GOOGLE_DOC_ID, config.GOOGLE_DOC_TAB_ID)


async def replace_doc(new_text: str, snapshot: DocSnapshot) -> None:
    await asyncio.to_thread(_replace_sync, config.GOOGLE_DOC_ID, config.GOOGLE_DOC_TAB_ID, new_text, snapshot)
