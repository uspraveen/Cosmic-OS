import asyncio
import json

import httpx

from gateway.message_reactions import (
    EMOJI_DESCRIPTIONS,
    REACTION_EMOJI,
    build_questions,
    cap_message_text,
    parse_reaction_response,
    pick_reaction,
    summarize_attachments,
)


def _jev_response(react_score, choice, confidence):
    answers = {}
    if react_score is not None:
        answers["react"] = {"noul": react_score}
    if choice is not None:
        answers["emoji"] = {
            "choice": choice,
            "confidence": confidence,
            "probabilities": {choice: confidence},
        }
    return {"model": "jev-latest", "answers": answers}


def _transport(payload, status_code=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=payload if status_code == 200 else {"error": "boom"},
        )

    return httpx.MockTransport(handler)


def test_emoji_whitelist_matches_confirmed_set():
    assert REACTION_EMOJI == ("👍", "🚀", "👀", "🔥", "🤔", "❤️", "⚡", "🫡", "🎉")
    assert set(EMOJI_DESCRIPTIONS) == set(REACTION_EMOJI)


def test_cap_message_text_short_passes_through():
    assert cap_message_text("How about we post this") == "How about we post this"


def test_cap_message_text_long_projects_start_middle_end():
    text = ("a" * 1200) + ("b" * 1200) + ("c" * 1200)
    capped = cap_message_text(text)
    assert len(capped) < 1700
    assert capped.startswith("a")
    assert capped.endswith("c")
    assert "…\n…\n" in capped


def test_summarize_attachments_describes_media():
    attachments = [
        {"mime": "video/mp4", "filename": "clip.mp4"},
        {"mime_type": "image/png"},
        {"filename": "notes.txt"},
        "junk",
    ]
    assert summarize_attachments(attachments) == [
        "video: clip.mp4",
        "image",
        "document: notes.txt",
    ]


def test_build_questions_has_absolute_and_relative_heads():
    questions = build_questions(["video: clip.mp4"])
    assert questions["react"]["type"] == "noul"
    assert questions["emoji"]["type"] == "choice"
    assert set(questions["emoji"]["criteria"]) == set(REACTION_EMOJI)
    assert "clip.mp4" in questions["react"]["instructions"]


def test_parse_reaction_accepts_clear_signal():
    outcome = parse_reaction_response(_jev_response(0.85, "👀", 0.62))
    assert outcome == {"emoji": "👀", "confidence": 0.62, "react_score": 0.85}


def test_parse_reaction_abstains_on_low_react_score():
    assert parse_reaction_response(_jev_response(0.23, "👍", 0.9)) is None


def test_parse_reaction_accepts_calibrated_video_case():
    # "How about we post this" + video scored noul 0.35 live.
    outcome = parse_reaction_response(_jev_response(0.35, "👀", 0.51))
    assert outcome is not None
    assert outcome["emoji"] == "👀"


def test_parse_reaction_abstains_on_low_confidence():
    assert parse_reaction_response(_jev_response(0.8, "🚀", 0.2)) is None


def test_parse_reaction_abstains_on_unknown_emoji():
    assert parse_reaction_response(_jev_response(0.8, "💀", 0.9)) is None


def test_parse_reaction_abstains_on_malformed_payload():
    assert parse_reaction_response(None) is None
    assert parse_reaction_response({}) is None
    assert parse_reaction_response({"answers": {"emoji": {"choice": "👍"}}}) is None


def test_pick_reaction_end_to_end_with_mock_transport():
    async def scenario():
        return await pick_reaction(
            "apikey_test",
            "Deploy the new build to staging and tell me when it is live",
            timeout_sec=2.0,
            transport=_transport(_jev_response(0.9, "🫡", 0.55)),
        )

    outcome = asyncio.run(scenario())
    assert outcome is not None
    assert outcome["emoji"] == "🫡"
    assert outcome["latency_ms"] >= 0


def test_pick_reaction_sends_two_question_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_jev_response(0.9, "👍", 0.6))

    async def scenario():
        return await pick_reaction(
            "apikey_test",
            "x" * 5000,
            attachments=[{"mime": "video/mp4", "filename": "clip.mp4"}],
            timeout_sec=2.0,
            transport=httpx.MockTransport(handler),
        )

    outcome = asyncio.run(scenario())
    assert outcome is not None
    assert seen["auth"] == "Bearer apikey_test"
    assert seen["body"]["model"] == "jev-latest"
    assert set(seen["body"]["questions"]) == {"react", "emoji"}
    # The long message must arrive capped, with the attachment noted.
    assert len(seen["body"]["state"]["message"]) < 1700
    assert seen["body"]["state"]["attachments"] == ["video: clip.mp4"]


def test_pick_reaction_returns_none_without_key():
    outcome = asyncio.run(
        pick_reaction("", "anything", transport=_transport(_jev_response(0.9, "👍", 0.9)))
    )
    assert outcome is None


def test_pick_reaction_returns_none_on_http_error():
    outcome = asyncio.run(
        pick_reaction(
            "apikey_test",
            "anything",
            timeout_sec=2.0,
            transport=_transport(_jev_response(0.9, "👍", 0.9), status_code=500),
        )
    )
    assert outcome is None
