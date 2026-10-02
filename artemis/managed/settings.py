"""Environment settings for managed deployments."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

__all__ = [
    "ENV_MANAGED",
    "MANAGED_PROFILE",
    "ManagedConfigError",
    "ManagedSettings",
    "is_managed",
    "parse_guarded_apps",
]

ENV_MANAGED = "ARTEMIS_MANAGED"
#: The only profile a managed server runs: Pro adds raw adb shell tools.
MANAGED_PROFILE = "flash"
#: Shortest API token accepted, so a typo cannot leave the API guessable.
MIN_TOKEN_LENGTH = 32
_TRUE = {"1", "true", "yes", "on"}


class ManagedConfigError(RuntimeError):
    """A managed server is missing a setting it cannot run safely without."""


def is_managed() -> bool:
    return os.environ.get(ENV_MANAGED, "").strip().lower() in _TRUE


def parse_guarded_apps(raw: str | None) -> tuple[tuple[str, ...], dict[str, str]]:
    """Comma-separated ``package`` or ``package=Display name`` entries.

    Returns the packages and the display names that were given.
    """
    packages: list[str] = []
    names: dict[str, str] = {}
    for entry in (raw or "").split(","):
        package, _, name = entry.partition("=")
        package, name = package.strip(), name.strip()
        if package:
            packages.append(package)
            if name:
                names[package] = name
    return tuple(packages), names


@dataclass(frozen=True)
class ManagedSettings:
    api_token: str
    guarded_apps: tuple[str, ...]
    app_names: dict[str, str]
    control_plane_url: str
    runtime_token: str
    #: Gateway model for helpers hard-wired to Gemini elsewhere (summaries,
    #: memory capsules).
    utility_model: str
    config_path: str
    #: Vision model that checks each committing input (see action_guard.py),
    #: reached through the same gateway URL and key as every other model.
    action_guard_model: str = ""
    model_base_url: str = ""
    model_api_key: str = ""

    @classmethod
    def from_env(cls) -> ManagedSettings:
        env = os.environ
        guarded_apps, app_names = parse_guarded_apps(env.get("ARTEMIS_GUARDED_APPS"))
        return cls(
            api_token=env.get("ARTEMIS_API_TOKEN", ""),
            guarded_apps=guarded_apps,
            app_names=app_names,
            control_plane_url=env.get("ARTEMIS_CONTROL_PLANE_URL", "").rstrip("/"),
            runtime_token=env.get("ARTEMIS_RUNTIME_TOKEN", ""),
            utility_model=env.get("ARTEMIS_MANAGED_UTILITY_MODEL", "artemis-flash-lite"),
            config_path=env.get("ARTEMIS_ARTEMIS_JSONC", ""),
            action_guard_model=env.get("ARTEMIS_ACTION_GUARD_MODEL", "").strip(),
            model_base_url=env.get("OPENAI_BASE_URL", "").strip(),
            model_api_key=env.get("OPENAI_API_KEY", "").strip(),
        )

    def check(self) -> None:
        """Refuse to start without the settings that keep the server safe."""
        if len(self.api_token) < MIN_TOKEN_LENGTH:
            raise ManagedConfigError(
                f"ARTEMIS_API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters."
            )
        if not self.config_path or not Path(self.config_path).is_file():
            # Without it the bundled config would call Gemini directly.
            raise ManagedConfigError(
                "ARTEMIS_ARTEMIS_JSONC must point to the managed model config."
            )
        if self.guarded_apps and not (self.control_plane_url and self.runtime_token):
            raise ManagedConfigError(
                "ARTEMIS_GUARDED_APPS needs ARTEMIS_CONTROL_PLANE_URL and "
                "ARTEMIS_RUNTIME_TOKEN to ask the user."
            )
        if self.action_guard_model:
            if not (self.model_base_url and self.model_api_key):
                raise ManagedConfigError(
                    "ARTEMIS_ACTION_GUARD_MODEL needs OPENAI_BASE_URL and OPENAI_API_KEY."
                )
            if not (self.control_plane_url and self.runtime_token):
                raise ManagedConfigError(
                    "ARTEMIS_ACTION_GUARD_MODEL needs ARTEMIS_CONTROL_PLANE_URL and "
                    "ARTEMIS_RUNTIME_TOKEN to ask the user."
                )
