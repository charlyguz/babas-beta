import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple, TypeVar

from babas.bridge.protocol import DEFAULT_PORT, PINNED_EXTENSION_ID
from babas.safety import ALLOW_ALL, DEFAULT_ALLOWED_DOMAINS

PROVIDERS = ("openai", "anthropic")
JSON_MODES = ("auto", "json_schema", "json_object", "none")
LANGUAGES = ("es", "en")
DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_MCP_HTTP_PORT = 8766
_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")

T = TypeVar("T")


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "openai"
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = field(default=None, repr=False)
    llm_model: Optional[str] = DEFAULT_OPENAI_MODEL
    llm_json_mode: str = "auto"
    llm_timeout: float = 60.0
    allowed_domains: Tuple[str, ...] = tuple(DEFAULT_ALLOWED_DOMAINS)
    max_steps: int = 30
    max_retries: int = 2
    language: str = "es"
    bridge_host: str = "127.0.0.1"
    bridge_port: int = DEFAULT_PORT
    extension_origins: Tuple[str, ...] = (f"chrome-extension://{PINNED_EXTENSION_ID}",)
    home: Path = field(default_factory=lambda: Path.home() / ".babas")
    mcp_http_port: int = DEFAULT_MCP_HTTP_PORT
    log_level: str = "INFO"

    @property
    def pairing_file(self) -> Path:
        return self.home / "pairing.json"

    @property
    def needs_api_key(self) -> bool:
        return self.llm_provider == "anthropic" or not self.llm_base_url

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        if env is None:
            from dotenv import load_dotenv

            load_dotenv()
            env = os.environ

        def text(name: str) -> Optional[str]:
            value = env.get(name)
            if value is None:
                return None
            value = value.strip()
            return value or None

        def number(name: str, default: T, convert: Callable[[str], T], low: float, high: float, kind: str) -> T:
            raw = text(name)
            if raw is None:
                return default
            try:
                value = convert(raw)
            except ValueError:
                raise ValueError(f"{name} must be {kind}, got {raw!r}") from None
            if not low <= value <= high:  # type: ignore[operator]
                raise ValueError(f"{name} must be between {low:g} and {high:g}, got {raw!r}")
            return value

        def choice(name: str, default: str, allowed: Tuple[str, ...]) -> str:
            raw = text(name)
            if raw is None:
                return default
            value = raw.lower()
            if value not in allowed:
                raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {raw!r}")
            return value

        provider = choice("BABAS_LLM_PROVIDER", "openai", PROVIDERS)
        key_fallback = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        base_url = text("BABAS_LLM_BASE_URL")
        if base_url is not None and not re.match(r"^https?://[^\s]+$", base_url):
            raise ValueError(f"BABAS_LLM_BASE_URL must be an http(s) URL, got {base_url!r}")
        model = text("BABAS_LLM_MODEL") or (DEFAULT_OPENAI_MODEL if provider == "openai" else None)

        domains = [part.strip().lower() for part in (env.get("BABAS_ALLOWED_DOMAINS") or "").split(",") if part.strip()]
        allowed = (ALLOW_ALL,) if ALLOW_ALL in domains else tuple(dict.fromkeys([*DEFAULT_ALLOWED_DOMAINS, *domains]))

        origins = tuple(dict.fromkeys(part.strip().lower() for part in (env.get("BABAS_EXTENSION_ORIGINS") or "").split(",") if part.strip()))
        for origin in origins:
            if not _ORIGIN.match(origin):
                raise ValueError(f"BABAS_EXTENSION_ORIGINS entries must look like chrome-extension://<32 letters>, got {origin!r}")
        home = text("BABAS_HOME")
        host = text("BABAS_BRIDGE_HOST")

        return cls(
            llm_provider=provider,
            llm_base_url=base_url,
            llm_api_key=text("BABAS_LLM_API_KEY") or text(key_fallback),
            llm_model=model,
            llm_json_mode=choice("BABAS_LLM_JSON_MODE", "auto", JSON_MODES),
            llm_timeout=number("BABAS_LLM_TIMEOUT", 60.0, float, 1, 3600, "a number of seconds"),
            allowed_domains=allowed,
            max_steps=number("BABAS_MAX_STEPS", 30, int, 1, 200, "a whole number"),
            max_retries=number("BABAS_MAX_RETRIES", 2, int, 0, 10, "a whole number"),
            language=choice("BABAS_LANG", "es", LANGUAGES),
            bridge_host=host or "127.0.0.1",
            bridge_port=number("BABAS_BRIDGE_PORT", DEFAULT_PORT, int, 1, 65535, "a port number"),
            extension_origins=origins or (f"chrome-extension://{PINNED_EXTENSION_ID}",),
            home=Path(home).expanduser() if home else Path.home() / ".babas",
            mcp_http_port=number("BABAS_MCP_HTTP_PORT", DEFAULT_MCP_HTTP_PORT, int, 1, 65535, "a port number"),
            log_level=choice("BABAS_LOG_LEVEL", "INFO", ("debug", "info", "warning", "error", "critical")).upper(),
        )
