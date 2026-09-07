"""Tests for the Feishu model-menu interactive cards.

The model menu upgrades the main menu's "切换模型" button into a three-tier
card flow: provider select card -> model list card (in-place update) ->
switch_model() result card. These tests cover the card helpers and the
dispatch path through ``_handle_card_action_event`` / ``_on_card_action_trigger``.
"""

import json
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import PlatformConfig
import plugins.platforms.feishu.adapter as feishu_module
from plugins.platforms.feishu.adapter import FeishuAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter() -> FeishuAdapter:
    """Create a FeishuAdapter with mocked internals."""
    config = PlatformConfig(enabled=True)
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _make_card_action_data(
    action_value: dict,
    chat_id: str = "oc_12345",
    open_id: str = "ou_user1",
    token: str = "tok_abc",
) -> SimpleNamespace:
    """Create a mock Feishu card action callback data object."""
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(
                tag="button",
                value=action_value,
            ),
        ),
    )


class _FakeCallBackCard:
    def __init__(self):
        self.type = None
        self.data = None


class _FakeP2Response:
    def __init__(self):
        self.card = None


@pytest.fixture(autouse=False)
def _patch_callback_card_types(monkeypatch):
    """Provide real-ish P2CardActionTriggerResponse / CallBackCard for tests."""
    monkeypatch.setattr(feishu_module, "P2CardActionTriggerResponse", _FakeP2Response)
    monkeypatch.setattr(feishu_module, "CallBackCard", _FakeCallBackCard)


# ===========================================================================
# Card helpers
# ===========================================================================

class TestModelMenuCards:
    """Card-builder helpers produce the correct Feishu interactive JSON."""

    def test_card_response_uses_raw_json_string(self, _patch_callback_card_types):
        adapter = _make_adapter()
        response = adapter._card_response({"config": {"update_multi": True}})
        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        # card.data MUST be a JSON string, not a dict (#42465 lesson).
        assert isinstance(response.card.data, str)
        assert json.loads(response.card.data)["config"]["update_multi"] is True

    def test_card_response_noop_when_sdk_absent(self, monkeypatch):
        monkeypatch.setattr(feishu_module, "P2CardActionTriggerResponse", None)
        adapter = _make_adapter()
        assert adapter._card_response({"config": {}}) is None

    def test_build_provider_card_groups_buttons_bisect(self):
        card = FeishuAdapter._build_provider_card([
            {"slug": "openrouter", "name": "OpenRouter"},
            {"slug": "anthropic", "name": "Anthropic"},
        ])
        assert card["config"]["update_multi"] is True
        assert card["header"]["title"]["content"] == "🔧 选择 Provider"
        actions = [e for e in card["elements"] if e["tag"] == "action"]
        assert all(a["layout"] == "bisect" for a in actions)
        values = [b["value"] for a in actions for b in a["actions"]]
        assert {"action": "model_provider", "provider": "openrouter"} in values
        assert {"action": "model_provider", "provider": "anthropic"} in values

    def test_build_model_list_card_has_back_button(self):
        card = FeishuAdapter._build_model_list_card("openrouter", ["a", "b", "c"])
        assert card["config"]["update_multi"] is True
        assert card["header"]["title"]["content"] == "🔧 选择模型"
        actions = [e for e in card["elements"] if e["tag"] == "action"]
        # Model buttons are bisect rows; the trailing row is the back button.
        back = actions[-1]["actions"][0]["value"]
        assert back == {"action": "model_provider_back"}
        model_values = [b["value"] for a in actions[:-1] for b in a["actions"]]
        assert {"action": "model_select", "provider": "openrouter", "model": "a"} in model_values

    def test_build_model_card_error_offers_back(self):
        card = FeishuAdapter._build_model_card_error("timeout")
        assert card["header"]["template"] == "red"
        actions = [e for e in card["elements"] if e["tag"] == "action"]
        assert actions[0]["actions"][0]["value"] == {"action": "model_provider_back"}


# ===========================================================================
# Dispatch through _handle_card_action_event
# ===========================================================================

class TestModelMenuDispatch:
    """Model-menu actions return a card response and never synthesize a command."""

    @pytest.mark.asyncio
    async def test_model_provider_returns_model_list_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        data = _make_card_action_data(
            {"action": "model_provider", "provider": "openrouter"},
            token="tok_model_provider",
        )
        async def _fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(adapter, "_list_models_for_provider", return_value=["a", "b"]) as mock_models,
            patch.object(
                feishu_module.asyncio, "to_thread",
                new=AsyncMock(side_effect=_fake_to_thread),
            ),
            patch.object(adapter, "_send_card_payload", new_callable=AsyncMock) as mock_send_card,
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            response = await adapter._handle_card_action_event(data)

        # Plan B (WebSocket): the hop posts a NEW card, so the callback response
        # carries no replacement card.
        assert response is not None
        assert response.card is None
        mock_models.assert_called_once_with("openrouter")
        mock_send_card.assert_awaited_once()
        sent_card = mock_send_card.await_args.args[1]
        assert sent_card["header"]["title"]["content"] == "🔧 选择模型"
        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_select_synthesizes_model_command(self, _patch_callback_card_types):
        """model_select goes through the agent pipeline: /model --provider <slug> <model>."""
        adapter = _make_adapter()
        data = _make_card_action_data(
            {"action": "model_select", "provider": "openrouter", "model": "gpt-5"},
            token="tok_model_select",
        )
        with (
            patch.object(
                adapter, "_resolve_sender_profile",
                return_value={"user_id": "u", "user_name": "User", "user_id_alt": None},
            ),
            patch.object(adapter, "get_chat_info", return_value={"name": "Test Chat"}),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
            patch.object(adapter, "_send_card_payload", new_callable=AsyncMock) as mock_send_card,
        ):
            response = await adapter._handle_card_action_event(data)

        # The switch runs through /model so the live agent and persistence are
        # used; no card-menu response is returned (the caller sends an empty ack).
        assert response is None
        mock_handle.assert_awaited_once()
        synthetic_event = mock_handle.await_args.args[0]
        assert synthetic_event.text == "/model --provider openrouter gpt-5"
        assert synthetic_event.message_type == feishu_module.MessageType.COMMAND
        assert synthetic_event.source.thread_id is None
        mock_send_card.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_choose_sends_new_provider_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        data = _make_card_action_data({"action": "model_choose"}, token="tok_model_choose")
        with (
            patch.object(adapter, "_send_provider_card", new_callable=AsyncMock) as mock_send,
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            response = await adapter._handle_card_action_event(data)

        assert response is not None and response.card is None
        mock_send.assert_awaited_once_with("oc_12345", thread_id=None)
        mock_handle.assert_not_called()

    def test_on_card_action_trigger_returns_model_menu_response(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data(
            {"action": "model_provider", "provider": "openrouter"},
            token="tok_sync",
        )
        sentinel = _FakeP2Response()
        mock_run = MagicMock(return_value=sentinel)
        with patch.object(adapter, "_run_model_card_action_sync", mock_run), \
                patch.object(adapter, "_submit_on_loop") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is sentinel
        mock_run.assert_called_once_with(data, adapter._loop)
        mock_submit.assert_not_called()

    def test_on_card_action_trigger_still_routes_regular_buttons(self, _patch_callback_card_types):
        """Non-model buttons keep the existing synthetic-command path."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"action": "status"}, token="tok_status")
        with patch.object(adapter, "_run_model_card_action_sync") as mock_run, \
                patch.object(adapter, "_submit_on_loop") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        mock_run.assert_not_called()
        mock_submit.assert_called_once()
        assert response is not None
