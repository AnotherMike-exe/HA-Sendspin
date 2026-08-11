"""Shared pytest fixtures for the Sendspin integration tests."""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom integrations in every test.

    Without this, Home Assistant refuses to load anything from
    custom_components/ during tests.
    """
    yield
