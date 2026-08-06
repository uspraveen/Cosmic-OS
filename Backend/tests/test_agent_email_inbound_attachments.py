"""An inbound email's attachments must survive all the way to the desktop.

The transcript now renders inbound emails inside a thread card, with the same
attachment chips a typed message gets. That only works if the attachment
metadata the webhook normalizes actually reaches the client payload in the
shape the renderer reads (filename / mime_type / size_bytes).
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.channels.agent_email import AgentEmailAdapter


def _adapter() -> AgentEmailAdapter:
    return AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
    )


def _webhook(attachments: list[dict] | None) -> dict:
    return {
        "mailbox": {"id": "mbx_1", "address": "iamcosmic001@mail.thelearnchain.com"},
        "thread": {"id": "thr_1", "subject": "Re: SPC referral"},
        "message": {
            "id": "msg_1",
            "thread_id": "thr_1",
            "subject": "Re: SPC referral",
            "text_body": "Here's the signed form.",
            "from_recipients": [{"email": "uspraveenraj@gmail.com", "name": "Praveen Raj U S"}],
            "attachments": attachments,
        },
    }


def test_inbound_attachments_reach_metadata_in_the_shape_the_ui_reads() -> None:
    normalized = _adapter().normalize_message(
        _webhook(
            [
                {
                    "id": "att_1",
                    "filename": "I-765-RFE-response.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 248311,
                },
                {"id": "att_2", "name": "signature.png", "mime_type": "image/png", "size": 5120},
            ]
        )
    )

    attachments = normalized["metadata"]["attachments"]
    assert [a["filename"] for a in attachments] == ["I-765-RFE-response.pdf", "signature.png"]
    # The desktop renderer reads these exact keys (it accepts snake_case).
    assert attachments[0]["mime_type"] == "application/pdf"
    assert attachments[0]["size_bytes"] == 248311
    assert attachments[1]["mime_type"] == "image/png"
    assert attachments[1]["size_bytes"] == 5120
    assert normalized["metadata"]["has_attachments"] is True
    assert normalized["metadata"]["attachment_count"] == 2


def test_an_attachment_without_a_filename_still_shows_as_an_attachment() -> None:
    """A chip with no name would render blank and be dropped by the client
    normalizer, silently hiding the fact that a file was attached at all."""
    normalized = _adapter().normalize_message(
        _webhook([{"id": "att_nameless", "content_type": "application/pdf"}])
    )
    attachment = normalized["metadata"]["attachments"][0]
    assert attachment["filename"], "must always carry a displayable name"
    assert attachment["mime_type"] == "application/pdf"


def test_an_email_with_no_attachments_reports_none() -> None:
    normalized = _adapter().normalize_message(_webhook(None))
    assert normalized["metadata"]["attachments"] == []
    assert normalized["metadata"]["has_attachments"] is False
    assert normalized["metadata"]["attachment_count"] == 0
