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
