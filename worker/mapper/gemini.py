"""Stage 2: ask Gemini to pick the candidate and write the patch.

Design points that matter more than the prompt wording:

  * The model returns a candidate_index, never a file path. It cannot invent
    a location outside what Stage 1 found.
  * Two confidences. "I know where this is" and "I know what the alt text
    should say" are different questions with different failure costs.
  * Nothing the model says is trusted. verify.py re-finds `original` in the
    real file and decide.py gates on our own checks, so a malformed or
    hallucinated response degrades to an annotation or a drop -- never to a
    wrong one-click suggestion.

Why we do NOT use server-side response schemas
----------------------------------------------
Measured 2026-09-29 against the Gemini API: any request carrying a JSON schema
(`generationConfig.responseSchema` on generateContent, or
`response_format.schema` on interactions) never completes -- the connection
stalls until the client gives up, for schemas as small as one boolean field.
Schema-free JSON mode on the same model and key answers in under a second.

So the schema is injected into the prompt (generated from the pydantic model, so
prompt and parser cannot drift) and enforced by validating locally. Set
GEMINI_USE_RESPONSE_SCHEMA=1 to go back to server-side enforcement if the API
starts honouring it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.logging_config import get_logger
from worker.mapper.candidates import Candidate
from worker.schema import Finding

log = get_logger(__name__)

SYSTEM_PROMPT = """You map accessibility violations found in a rendered web page back to the React/JSX source that produced them, and propose a minimal fix.

You receive:
  1. The failing element's outer HTML as rendered in the browser, the axe-core rule id, and axe's explanation of the failure.
  2. Up to three candidate source excerpts from files changed in the pull request, each with its file path and 1-based line numbers.

Rules:
- Choose the ONE candidate that most plausibly rendered this element, or report matched=false. Never reference a file or line outside the candidates.
- Rendered HTML differs from JSX. Class names may be hashed, text may come from props or translations, attributes may be added at runtime. Match on structure and on stable literals (src, href, data-testid, ids), not exact equality.
- If the element is rendered by a child component (e.g. <Avatar/>) and the fix is to pass a prop at the call site, the call site is the location.
- `original` must be a verbatim, contiguous copy of the lines you change, exactly as in the excerpt, including indentation. Do not include the line-number gutter. `replacement` replaces exactly those lines. Fix the violation and change nothing else. Match the file's formatting conventions.
- Set requires_human_content=true when a correct fix needs information you cannot know: what an image depicts, where a link leads, what a button does. Still write the best patch you can, with a clearly marked placeholder such as alt="TODO: describe the image".
- Be calibrated. location_confidence is your probability the location is right. patch_confidence is your probability that committing `replacement` as-is fixes the violation without breaking the code. A wrong one-click fix is worse than no fix; prefer low confidence."""

# Transient upstream conditions seen in practice: "high demand" 503s and
# occasional 429s. Worth a couple of quick retries rather than dropping a
# finding the developer would have wanted.
_RETRY_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL")
_MAX_ATTEMPTS = 3
_REQUEST_TIMEOUT_S = 90.0
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class MapperResponse(BaseModel):
    matched: bool
    candidate_index: int | None = Field(
        default=None, description="0-based index into the candidates, or null"
    )
    line_start: int | None = Field(default=None, description="1-based, from the excerpt's numbering")
    line_end: int | None = None
    location_confidence: float = Field(ge=0.0, le=1.0)
    original: str | None = Field(default=None, description="verbatim lines being replaced")
    replacement: str | None = None
    patch_confidence: float = Field(ge=0.0, le=1.0)
    requires_human_content: bool = False
    rationale: str = Field(description="one sentence")


class MapperModel(Protocol):
    """Anything that can answer a mapping prompt. The Gemini client below is
    the real one; tests and the eval harness supply fakes/recorders."""

    model_id: str

    async def map(self, prompt: str) -> MapperResponse | None: ...


def schema_instructions() -> str:
    """The response contract, generated from the model so it cannot drift."""
    schema = MapperResponse.model_json_schema()
    return (
        "Respond with a single JSON object and nothing else -- no prose, no "
        "markdown fence. It must validate against this JSON Schema:\n"
        f"{json.dumps(schema, indent=2)}"
    )


def build_prompt(finding: Finding, candidates: list[Candidate]) -> str:
    wcag = ", ".join(f"{c.id} {c.name} ({c.level})" for c in finding.wcag) or "n/a"
    parts = [
        "## Violation",
        f"axe rule: {finding.rule_id}",
        f"WCAG: {wcag}",
        f"impact: {finding.impact.value}",
        f"help: {finding.help}",
        "",
        "axe failure summary:",
        finding.failure_summary,
        "",
        "## Rendered element (outer HTML, as seen in the browser)",
        "```html",
        finding.html,
        "```",
        f"rendered CSS selector: {finding.selector}",
        "",
        "## Candidates (files changed in this pull request)",
    ]
    for i, c in enumerate(candidates):
        parts.append(f"\n### Candidate {i}: {c.file} lines {c.line_start}-{c.line_end}")
        if c.matched:
            parts.append(f"matched literals: {', '.join(c.matched[:6])}")
        parts.append("```tsx")
        for n, line in enumerate(c.excerpt.splitlines(), start=c.line_start):
            parts.append(f"{n:5d} | {line}")
        parts.append("```")
    return "\n".join(parts)


_LITERAL_CONTROL = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def repair_unescaped_quotes(raw: str) -> str:
    """Escape stray double quotes inside JSON string values.

    Without server-side schema enforcement the model regularly emits JSX
    verbatim into a string field:

        "original": "<a href={href} target="_blank">"

    which is invalid JSON. Measured on the eval set, this was the single cause
    of every unparseable response. A quote genuinely ends a string only when the
    next non-space character is one of , } ] : -- anything else means the model
    forgot to escape it.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(raw):
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            nxt = next((c for c in raw[i + 1 :] if not c.isspace()), "")
            if nxt in (",", "}", "]", ":"):
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')  # interior quote the model failed to escape
            continue
        # Raw newlines and tabs inside a JSON string are also illegal; the
        # model emits them when it pastes multi-line JSX.
        out.append(_LITERAL_CONTROL.get(ch, ch))
    return "".join(out)


def parse_response(text: str | None) -> MapperResponse | None:
    """Tolerate a markdown fence, trailing prose, and unescaped JSX quotes."""
    if not text:
        return None
    cleaned = _FENCE.sub("", text.strip())

    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    candidates += [repair_unescaped_quotes(c) for c in list(candidates)]

    last: str | None = None
    for candidate in candidates:
        try:
            return MapperResponse.model_validate_json(candidate)
        except ValueError as exc:
            last = str(exc)
    log.info("mapper_response_invalid", error=(last or "")[:200], head=cleaned[:200])
    return None


class GeminiMapper:
    def __init__(self, api_key: str, model_id: str) -> None:
        from google import genai  # imported lazily so tests never need the SDK

        self._client = genai.Client(api_key=api_key)
        self.model_id = model_id
        self._use_schema = os.environ.get("GEMINI_USE_RESPONSE_SCHEMA") == "1"

    async def map(self, prompt: str) -> MapperResponse | None:
        full = f"{SYSTEM_PROMPT}\n\n{schema_instructions()}\n\n{prompt}"
        last_error: str | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                text = await asyncio.wait_for(self._call(full), timeout=_REQUEST_TIMEOUT_S)
                return parse_response(text)
            except TimeoutError:
                last_error = f"timeout after {_REQUEST_TIMEOUT_S:.0f}s"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if not any(m in str(exc) for m in _RETRY_MARKERS):
                    log.warning("mapper_call_failed", error=last_error[:300])
                    raise
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(2**attempt)
            log.info("mapper_retry", attempt=attempt, error=(last_error or "")[:200])

        raise RuntimeError(f"Gemini unavailable after {_MAX_ATTEMPTS} attempts: {last_error}")

    async def _call(self, prompt: str) -> str | None:
        response_format: dict[str, Any] = {"type": "text", "mime_type": "application/json"}
        if self._use_schema:
            response_format["schema"] = MapperResponse.model_json_schema()

        interaction = await self._client.aio.interactions.create(
            model=self.model_id,
            input=prompt,
            response_format=response_format,
        )
        return _output_text(interaction)


def _output_text(interaction: Any) -> str | None:
    """`output_text` is the documented accessor; walk `steps` if it is absent so
    an SDK shape change degrades to a drop rather than an exception."""
    text = getattr(interaction, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    chunks: list[str] = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) == "thought":
            continue
        for item in getattr(step, "content", None) or []:
            piece = getattr(item, "text", None)
            if isinstance(piece, str):
                chunks.append(piece)
    return "".join(chunks) or None
