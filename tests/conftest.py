"""Shared pytest fixtures for the Sendspin integration tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom integrations in every test.

    Without this, Home Assistant refuses to load anything from
    custom_components/ during tests.
    """
    yield


@pytest.fixture(autouse=True)
def no_controller_links():
    """Never open a real controller websocket during tests.

    Controller links observe live Sendspin servers to read what is playing.
    Left unpatched they would try to dial the addresses in the mesh fixtures.
    The protocol handling itself is covered directly in test_legacy_client.
    """
    with patch("custom_components.sendspin.coordinator.LegacyControllerClient"):
        yield
