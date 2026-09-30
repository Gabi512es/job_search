"""The CV -> scoring criteria (dimensions + flags) module, no API call.

    python3 tests/test_scoring_criteria_suggestions.py

Costs nothing. `suggest_scoring_criteria` is the only function that talks to
the Anthropic API, and every call it makes here goes to a fake client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.profile import LlmFlag, ScoringDimension  # noqa: E402
from jobscout.scoring_criteria_suggestions import (  # noqa: E402
    MAX_DIMENSIONS,
    MAX_FLAGS,
    MIN_DIMENSIONS,
    SEVERITY_ADJUSTMENTS,
    SUGGESTION_MODEL,
    ResponseError,
    _normalize_importances,
    build_scoring_criteria_prompt,
    build_scoring_criteria_system,
    parse_scoring_criteria,
    suggest_scoring_criteria,
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


def good_payload(**overrides) -> str:
    payload = {
        "dimensions": [
            {"key": "field_experience", "label": "Field experience",
             "importance": 5, "rubric": "- 9-10: a\n- 7-8: b\n- 5-6: c\n- 3-4: d\n- 0-2: e"},
            {"key": "language_fit", "label": "Language fit",
             "importance": 3, "rubric": "- 9-10: a\n- 7-8: b\n- 5-6: c\n- 3-4: d\n- 0-2: e"},
            {"key": "org_type_fit", "label": "Org type fit",
             "importance": 2, "rubric": "- 9-10: a\n- 7-8: b\n- 5-6: c\n- 3-4: d\n- 0-2: e"},
        ],
        "flags": [
            {"key": "requires_catalan", "definition": "Set true if Catalan is required",
             "severity": "strong_penalty", "note": "Requires Catalan"},
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


# ===========================================================================
section("NORMALIZE_IMPORTANCES — the arithmetic the model never does itself")

check("weights always sum to exactly 1.0 (5/5/5/5/1)",
      round(sum(_normalize_importances([5, 5, 5, 5, 1])), 10), 1.0)
check("weights always sum to exactly 1.0 (3/3/3, an awkward third)",
      round(sum(_normalize_importances([3, 3, 3])), 10), 1.0)
check("a single dimension gets the full weight",
      _normalize_importances([5]), [1.0])
check("higher importance yields a higher weight",
      _normalize_importances([5, 1])[0] > _normalize_importances([5, 1])[1], True)


# ===========================================================================
section("PROMPTS — the two-kinds-of-criteria instruction, never arithmetic")

sys_prompt = build_scoring_criteria_system(None)
check("asks for dimensions as a matter of degree",
      "DEGREE" in sys_prompt, True)
check("asks for flags as binary dealbreakers/bonuses",
      "BINARY" in sys_prompt, True)
check("asks for importance, never a weight, from the model",
      "never a weight" in sys_prompt, True)
check("asks for severity, never a numeric adjustment, from the model",
      "never a numeric adjustment" in sys_prompt, True)
check("an explicit language hint is named in full",
      "Spanish" in build_scoring_criteria_system("es"), True)
check("no language -> infer from the CV itself",
      "same language as the CV" in build_scoring_criteria_system(None), True)

prompt = build_scoring_criteria_prompt("Some CV body, with real content.")
check("the CV text is embedded verbatim",
      "Some CV body, with real content." in prompt, True)
check("no search_keywords -> no keywords section",
      "Search keywords" in prompt, False)

prompt_with_kw = build_scoring_criteria_prompt(
    "Some CV body.", ["Mediación intercultural", "Educación social"])
check("given search_keywords, they anchor the prompt",
      "Mediación intercultural" in prompt_with_kw, True)


# ===========================================================================
section("PARSE_SCORING_CRITERIA — strict shape, exact schema conformance")

result = parse_scoring_criteria(good_payload())
check("three dimensions parsed", len(result["dimensions"]), 3)
check("one flag parsed", len(result["llm_flags"]), 1)
check("weights sum to exactly 1.0",
      round(sum(d["weight"] for d in result["dimensions"]), 10), 1.0)
check("higher importance -> higher weight",
      result["dimensions"][0]["weight"] > result["dimensions"][1]["weight"], True)
check("severity maps to the fixed adjustment value",
      result["llm_flags"][0]["adjustment"], SEVERITY_ADJUSTMENTS["strong_penalty"])
check("a negative adjustment targets red_flags",
      result["llm_flags"][0]["target"], "red_flags")

# Every dimension/flag must round-trip through the real schema classes.
for d in result["dimensions"]:
    ScoringDimension(**d)
for f in result["llm_flags"]:
    LlmFlag(**f)
check("every dimension validates as a real ScoringDimension", True, True)
check("every flag validates as a real LlmFlag", True, True)

check("a bonus flag targets match_signals, not red_flags",
      parse_scoring_criteria(good_payload(flags=[
          {"key": "has_certification", "definition": "Set true if certified",
           "severity": "bonus", "note": "Has certification"},
      ]))["llm_flags"][0]["target"], "match_signals")

check("flags are optional - an empty/missing list is fine",
      parse_scoring_criteria(good_payload(flags=[]))["llm_flags"], [])

check("strips a ```json fence",
      len(parse_scoring_criteria("```json\n" + good_payload() + "\n```")["dimensions"]), 3)

for label, bad in (
    ("truncated JSON", good_payload()[:20]),
    ("a JSON array instead of an object", "[1, 2, 3]"),
    ("too few dimensions", good_payload(dimensions=[
        {"key": "only_one", "label": "Only one", "importance": 3,
         "rubric": "- 9-10: a\n- 7-8: b\n- 5-6: c\n- 3-4: d\n- 0-2: e"},
    ])),
    ("a non-snake_case dimension key", good_payload(dimensions=[
        {"key": "Not Snake Case", "label": "x", "importance": 3, "rubric": "x" * 20},
    ])),
    ("importance out of range", good_payload(dimensions=[
        {"key": "a", "label": "x", "importance": 9, "rubric": "x" * 20},
        {"key": "b", "label": "y", "importance": 3, "rubric": "x" * 20},
    ])),
    ("duplicate dimension keys", good_payload(dimensions=[
        {"key": "dup", "label": "x", "importance": 3, "rubric": "x" * 20},
        {"key": "dup", "label": "y", "importance": 2, "rubric": "x" * 20},
    ])),
    ("an unknown severity label", good_payload(flags=[
        {"key": "x", "definition": "x" * 20, "severity": "very_bad", "note": "x"},
    ])),
    ("duplicate flag keys", good_payload(flags=[
        {"key": "dup", "definition": "x" * 20, "severity": "bonus", "note": "x"},
        {"key": "dup", "definition": "y" * 20, "severity": "bonus", "note": "y"},
    ])),
    ("too many flags", good_payload(flags=[
        {"key": f"f{i}", "definition": "x" * 20, "severity": "bonus", "note": "x"}
        for i in range(MAX_FLAGS + 1)
    ])),
):
    raised = False
    try:
        parse_scoring_criteria(bad)
    except ResponseError:
        raised = True
    check(f"rejects {label}", raised, True)

check(f"MIN_DIMENSIONS is enforced ({MIN_DIMENSIONS})", MIN_DIMENSIONS >= 2, True)
check(f"MAX_DIMENSIONS is enforced ({MAX_DIMENSIONS})", MAX_DIMENSIONS <= 6, True)


# ===========================================================================
section("SUGGEST_SCORING_CRITERIA — the retry path, against a fake client")


class FakeMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class FakeClient:
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


client = FakeClient([good_payload()])
out = suggest_scoring_criteria(client, "some cv text", language="es")
check("returns dimensions and llm_flags", set(out.keys()), {"dimensions", "llm_flags"})
check("exactly one call when the first reply is usable", len(client.calls), 1)
check("uses the Sonnet model", client.calls[0]["model"], SUGGESTION_MODEL)
check("the CV text reaches the prompt",
      "some cv text" in client.calls[0]["prompt"], True)
check("the language hint reaches the system prompt",
      "Spanish" in client.calls[0]["system"], True)

client_kw = FakeClient([good_payload()])
suggest_scoring_criteria(client_kw, "cv", search_keywords=["Educación social"])
check("search_keywords reach the prompt",
      "Educación social" in client_kw.calls[0]["prompt"], True)

client2 = FakeClient(["not json at all", good_payload()])
out2 = suggest_scoring_criteria(client2, "cv", max_attempts=3)
check("retries once on an unusable reply, then succeeds",
      len(out2["dimensions"]), 3)
check("exactly two calls were made", len(client2.calls), 2)

client3 = FakeClient(["bad 1", "bad 2", "bad 3"])
raised = False
try:
    suggest_scoring_criteria(client3, "cv", max_attempts=3)
except ResponseError:
    raised = True
check("exhausting every retry raises ResponseError", raised, True)
check("exactly max_attempts calls were made", len(client3.calls), 3)

client4 = FakeClient([RuntimeError("network down")])
raised = False
try:
    suggest_scoring_criteria(client4, "cv", max_attempts=3)
except ResponseError:
    raised = True
check("a non-JSON failure (network, rate limit) raises too", raised, True)
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
