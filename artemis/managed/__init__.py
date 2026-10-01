"""Managed deployments: Artemis as one user's executor on a hosted cloud phone.

Enabled with ``ARTEMIS_MANAGED=1``. A managed server runs next to the user's
runtime (sharing its network namespace and adb server) and differs from a
local install in four ways:

- every ``/api`` request needs ``Authorization: Bearer $ARTEMIS_API_TOKEN``
  and only the routes the runtime uses are served (:mod:`.server`);
- only the Flash profile runs, so no tool can reach a raw adb shell;
- input actions in guarded apps wait for the user's approval through the
  control plane (:mod:`.guard`);
- models come only from the OpenAI-compatible gateway in the managed config
  (``ARTEMIS_ARTEMIS_JSONC``), never from a provider key.
"""

from artemis.managed.settings import ManagedSettings, is_managed

__all__ = ["ManagedSettings", "is_managed"]
