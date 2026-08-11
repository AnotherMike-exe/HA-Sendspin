"""Shared data structures for the Sendspin integration.

Exists to break the import cycle that otherwise forms between the config entry
setup, the server host, the coordinator and the entity platforms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server_host import ServerHost


@dataclass(slots=True)
class SendspinRuntimeData:
    """Everything a Sendspin config entry owns while it is loaded.

    Stored on `entry.runtime_data`. Grows in M2 with the coordinator and the
    adopted-player registry.
    """

    host: ServerHost
