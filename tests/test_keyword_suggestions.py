"""The CV -> keyword suggestions module, exercised without any API call.

    python3 tests/test_keyword_suggestions.py

Costs nothing. `suggest_keywords` is the only function that talks to the
Anthropic API, and every call it makes here goes to a fake client that
returns canned text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.keyword_suggestions import (  # noqa: E402
    MAX_SUGGESTIONS,
    SUGGESTION_MODEL,
    ResponseError,
    build_keyword_suggestion_prompt,
    build_keyword_suggestion_system,
    parse_suggestions,
    suggest_keywords,
)

CHECKS = 0
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    global CHECKS
    CHECKS += 1
    if actual == expected:
        print(f"  OK    {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}")
    print(f"          expected: {expected!r}")
    print(f"          actual  : {actual!r}")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ===========================================================================
section("PROMPTS — language handling and shape")

check("no language -> infer from the CV itself",
      "same language as the CV" in build_keyword_suggestion_system(None), True)
check("an explicit language code is named in full",
      "Spanish" in build_keyword_suggestion_system("es"), True)
check("an unrecognised code is passed through as-is rather than dropped",
      "xx" in build_keyword_suggestion_system("xx"), True)
check("the system prompt demands bare JSON",
      "no code fence" in build_keyword_suggestion_system(None), True)
check("the casing rule capitalizes the meaningful words",
      "meaningful words" in build_keyword_suggestion_system(None), True)
check("and explicitly keeps connecting words lowercase",
      "lowercase" in build_keyword_suggestion_system(None), True)
check("with a non-English example, not just an English-only rule",
      "Automatización de Procesos" in build_keyword_suggestion_system(None),
      True)
check("warns against blindly applying English capitalization elsewhere",
      "do not apply English capitalization rules" in
      build_keyword_suggestion_system(None), True)
check("the casing rule holds regardless of which language is requested",
      "meaningful words" in build_keyword_suggestion_system("es"), True)

prompt = build_keyword_suggestion_prompt("Some CV body, with real content.")
check("the CV text is embedded verbatim",
      "Some CV body, with real content." in prompt, True)
check("asks for the exact output shape", '"suggestions"' in prompt, True)


# ===========================================================================
section("PARSE_SUGGESTIONS — strict shape, no silent empty list")

good_json = '{"suggestions": ["AI Engineer", "Data Scientist"]}'
check("parses a clean reply",
      parse_suggestions(good_json), ["AI Engineer", "Data Scientist"])
check("strips a ```json fence",
      parse_suggestions("```json\n" + good_json + "\n```"),
      ["AI Engineer", "Data Scientist"])
check("strips a bare ``` fence",
      parse_suggestions("```\n" + good_json + "\n```"),
      ["AI Engineer", "Data Scientist"])
check("trims to MAX_SUGGESTIONS even if the model over-answers",
      len(parse_suggestions(json.dumps(
          {"suggestions": [f"kw{i}" for i in range(10)]}))),
      MAX_SUGGESTIONS)
check("drops blank entries rather than keeping them as empty suggestions",
      parse_suggestions(json.dumps({"suggestions": ["  ", "AI Engineer", ""]})),
      ["AI Engineer"])

for label, bad in (
    ("truncated JSON", good_json[:10]),
    ("a JSON array instead of an object", "[1, 2, 3]"),
    ("a missing suggestions key", "{}"),
    ("suggestions is a string, not a list", '{"suggestions": "AI Engineer"}'),
    ("an empty list", '{"suggestions": []}'),
    ("a list of only blanks", '{"suggestions": ["", "   "]}'),
):
    raised = False
    try:
        parse_suggestions(bad)
    except ResponseError:
        raised = True
    check(f"rejects {label} rather than returning [] silently", raised, True)


# ===========================================================================
section("SUGGEST_KEYWORDS — the retry path, end to end against a fake client")


class FakeMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class FakeSuggestClient:
    """Returns each reply in order (or raises it, if it's an exception)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, **kwargs):
        self.calls.append({"model": model, "system": system,
                           "prompt": messages[0]["content"]})
        reply = self.replies[len(self.calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return FakeMessage(reply)


client = FakeSuggestClient([good_json])
result = suggest_keywords(client, "some cv text", language="en")
check("returns the parsed suggestions",
      result, ["AI Engineer", "Data Scientist"])
check("exactly one call when the first reply is usable", len(client.calls), 1)
check("uses the Sonnet model, matching the writing config's default",
      client.calls[0]["model"], SUGGESTION_MODEL)
check("the CV text reaches the prompt",
      "some cv text" in client.calls[0]["prompt"], True)
check("the language hint reaches the system prompt",
      "English" in client.calls[0]["system"], True)

client2 = FakeSuggestClient(["not json at all", good_json])
result2 = suggest_keywords(client2, "cv", max_attempts=3)
check("retries once on an unusable reply, then succeeds",
      result2, ["AI Engineer", "Data Scientist"])
check("exactly two calls were made", len(client2.calls), 2)

client3 = FakeSuggestClient(["bad 1", "bad 2", "bad 3"])
raised = False
try:
    suggest_keywords(client3, "cv", max_attempts=3)
except ResponseError:
    raised = True
check("exhausting every retry raises ResponseError, never an empty list",
      raised, True)
check("exactly max_attempts calls were made, no more", len(client3.calls), 3)

client4 = FakeSuggestClient([RuntimeError("network down")])
raised = False
try:
    suggest_keywords(client4, "cv", max_attempts=3)
except ResponseError:
    raised = True
check("a non-JSON failure (network, rate limit) raises too",
      raised, True)
check("but does NOT retry a failure that was never about bad JSON",
      len(client4.calls), 1)


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks. No Anthropic call was made.")
print("=" * 78)
