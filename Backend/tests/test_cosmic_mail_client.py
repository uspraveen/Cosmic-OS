import httpx
import pytest

from shared.cosmic_mail_client import CosmicMailClient


@pytest.mark.asyncio
async def test_cosmic_mail_client_list_mailboxes_accepts_top_level_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/mailboxes"
        return httpx.Response(
            200,
            json=[
                {"id": "mbx_1", "address": "agent@example.com"},
                {"id": "mbx_2", "address": "ops@example.com"},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cosmic = CosmicMailClient(
            base_url="https://console.thelearnchain.com",
            api_token="token",
            client=client,
        )
        mailboxes = await cosmic.list_mailboxes()

    assert mailboxes == [
        {"id": "mbx_1", "address": "agent@example.com"},
        {"id": "mbx_2", "address": "ops@example.com"},
    ]


@pytest.mark.asyncio
async def test_cosmic_mail_client_get_auth_context_still_requires_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/system/auth-context"
        return httpx.Response(200, json={"is_admin": True, "organization_id": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cosmic = CosmicMailClient(
            base_url="https://console.thelearnchain.com",
            api_token="token",
            client=client,
        )
        payload = await cosmic.get_auth_context()

    assert payload["is_admin"] is True


@pytest.mark.asyncio
async def test_cosmic_mail_client_updates_and_deletes_webhooks() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8") if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            return httpx.Response(
                200,
                json={
                    "id": "wh_123",
                    "url": "https://gateway.example.com/internal/channels/agent-email/incoming",
                    "event_type": "message.received",
                    "is_active": True,
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cosmic = CosmicMailClient(
            base_url="https://console.thelearnchain.com",
            api_token="token",
            client=client,
        )
        payload = await cosmic.update_webhook(
            "wh_123",
            {
                "url": "https://gateway.example.com/internal/channels/agent-email/incoming",
                "event_type": "message.received",
                "is_active": True,
            },
        )
        await cosmic.delete_webhook("wh_123")

    assert payload["id"] == "wh_123"
    assert seen == [
        (
            "PATCH",
            "/v1/webhooks/wh_123",
            '{"url":"https://gateway.example.com/internal/channels/agent-email/incoming","event_type":"message.received","is_active":true}',
        ),
        ("DELETE", "/v1/webhooks/wh_123", None),
    ]


@pytest.mark.asyncio
async def test_cosmic_mail_client_creates_organization_api_key() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8") if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(
            201,
            json={
                "api_key": {
                    "id": "key_123",
                    "organization_id": "org_123",
                    "name": "COSMIC Gateway Agent Email",
                    "key_prefix": "cm_org_123",
                    "last_used_at": None,
                    "created_at": "2026-03-27T00:00:00Z",
                    "revoked_at": None,
                },
                "plaintext_key": "cm_org_secret",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cosmic = CosmicMailClient(
            base_url="https://console.thelearnchain.com",
            api_token="token",
            client=client,
        )
        payload = await cosmic.create_organization_api_key(
            "org_123",
            name="COSMIC Gateway Agent Email",
        )

    assert payload["plaintext_key"] == "cm_org_secret"
    assert seen == [
        (
            "POST",
            "/v1/organizations/org_123/api-keys",
            '{"name":"COSMIC Gateway Agent Email"}',
        )
    ]
