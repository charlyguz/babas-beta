from pathlib import Path

import pytest

from babas.bridge.protocol import PINNED_EXTENSION_ID
from babas.config import Settings
from babas.safety import DEFAULT_ALLOWED_DOMAINS

ORIGIN = f"chrome-extension://{PINNED_EXTENSION_ID}"


def settings(**env: str) -> Settings:
    return Settings.from_env(env)


def test_defaults():
    s = settings()
    assert s.llm_provider == "openai"
    assert s.llm_base_url is None
    assert s.llm_api_key is None
    assert s.llm_model == "gpt-5.2"
    assert s.llm_json_mode == "auto"
    assert s.llm_timeout == 60.0
    assert s.allowed_domains == tuple(DEFAULT_ALLOWED_DOMAINS)
    assert (s.max_steps, s.max_retries) == (30, 2)
    assert s.language == "es"
    assert (s.bridge_host, s.bridge_port) == ("127.0.0.1", 8765)
    assert s.extension_origins == (ORIGIN,)
    assert s.home == Path.home() / ".babas"
    assert s.pairing_file == Path.home() / ".babas" / "pairing.json"
    assert s.mcp_http_port == 8766
    assert s.log_level == "INFO"
    assert s.needs_api_key


def test_openai_api_key_fallback_and_precedence():
    assert settings(OPENAI_API_KEY="sk-fallback").llm_api_key == "sk-fallback"
    assert settings(OPENAI_API_KEY="sk-fallback", BABAS_LLM_API_KEY="sk-babas").llm_api_key == "sk-babas"
    assert settings(OPENAI_API_KEY="   ").llm_api_key is None


def test_anthropic_provider_uses_its_own_key_and_requires_a_model():
    s = settings(BABAS_LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant", OPENAI_API_KEY="sk-openai")
    assert s.llm_api_key == "sk-ant"
    assert s.llm_model is None
    assert s.needs_api_key
    assert settings(BABAS_LLM_PROVIDER="Anthropic", BABAS_LLM_MODEL="some-model").llm_model == "some-model"


def test_local_endpoints_do_not_need_a_key():
    s = settings(BABAS_LLM_BASE_URL="http://localhost:11434/v1", BABAS_LLM_MODEL="qwen2.5:7b")
    assert not s.needs_api_key
    assert s.llm_base_url == "http://localhost:11434/v1"
    assert s.llm_model == "qwen2.5:7b"
    assert settings(BABAS_LLM_PROVIDER="anthropic", BABAS_LLM_BASE_URL="http://localhost:1/").needs_api_key


def test_allowed_domains_extend_the_defaults_and_star_allows_everything():
    s = settings(BABAS_ALLOWED_DOMAINS=" Colegio.EDU.mx , sat.gob.mx,,")
    assert s.allowed_domains == (*DEFAULT_ALLOWED_DOMAINS, "colegio.edu.mx", "sat.gob.mx")
    assert settings(BABAS_ALLOWED_DOMAINS="gob.mx,gob.mx").allowed_domains == tuple(DEFAULT_ALLOWED_DOMAINS)
    assert settings(BABAS_ALLOWED_DOMAINS="example.com, *").allowed_domains == ("*",)


@pytest.mark.parametrize(
    "name, value",
    [
        ("BABAS_LLM_PROVIDER", "gemini"),
        ("BABAS_LLM_JSON_MODE", "yaml"),
        ("BABAS_LLM_TIMEOUT", "soon"),
        ("BABAS_LLM_TIMEOUT", "0"),
        ("BABAS_LLM_BASE_URL", "localhost:11434"),
        ("BABAS_MAX_STEPS", "ten"),
        ("BABAS_MAX_STEPS", "0"),
        ("BABAS_MAX_STEPS", "3.5"),
        ("BABAS_MAX_RETRIES", "-1"),
        ("BABAS_LANG", "fr"),
        ("BABAS_BRIDGE_PORT", "70000"),
        ("BABAS_BRIDGE_PORT", "abc"),
        ("BABAS_MCP_HTTP_PORT", "0"),
        ("BABAS_LOG_LEVEL", "loud"),
        ("BABAS_EXTENSION_ORIGINS", "https://evil.example"),
        ("BABAS_EXTENSION_ORIGINS", "*"),
    ],
)
def test_invalid_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        settings(**{name: value})


def test_numbers_and_choices_are_parsed():
    s = settings(
        BABAS_LLM_TIMEOUT="12.5",
        BABAS_MAX_STEPS="50",
        BABAS_MAX_RETRIES="0",
        BABAS_LANG="EN",
        BABAS_BRIDGE_HOST="localhost",
        BABAS_BRIDGE_PORT="9000",
        BABAS_MCP_HTTP_PORT="9001",
        BABAS_LOG_LEVEL="debug",
        BABAS_LLM_JSON_MODE="JSON_Schema",
    )
    assert (s.llm_timeout, s.max_steps, s.max_retries) == (12.5, 50, 0)
    assert (s.language, s.bridge_host, s.bridge_port, s.mcp_http_port) == ("en", "localhost", 9000, 9001)
    assert (s.log_level, s.llm_json_mode) == ("DEBUG", "json_schema")


def test_extension_origins_replace_the_default_and_home_is_expanded(tmp_path):
    other = "chrome-extension://" + "a" * 32
    s = settings(BABAS_EXTENSION_ORIGINS=f"{other}, {other.upper()}", BABAS_HOME=str(tmp_path / "x"))
    assert s.extension_origins == (other,)
    assert s.home == tmp_path / "x"
    assert s.pairing_file == tmp_path / "x" / "pairing.json"
    assert settings(BABAS_HOME="~/somewhere").home == Path("~/somewhere").expanduser()


def test_repr_never_contains_the_key():
    s = settings(BABAS_LLM_API_KEY="sk-super-secret-value")
    assert "sk-super-secret-value" not in repr(s)
    assert "sk-super-secret-value" not in str(s)


def test_from_env_without_a_mapping_reads_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BABAS_MAX_STEPS", "7")
    monkeypatch.setenv("BABAS_LLM_API_KEY", "sk-from-process")
    s = Settings.from_env()
    assert s.max_steps == 7
    assert s.llm_api_key == "sk-from-process"


def test_speech_defaults():
    s = settings()
    assert s.stt_provider == "auto"
    assert s.stt_model is None
    assert s.stt_base_url is None
    assert s.stt_api_key is None
    assert s.stt_language is None


def test_speech_variables_are_parsed():
    s = settings(
        BABAS_STT_PROVIDER="Whisper",
        BABAS_STT_MODEL=" whisper-large-v3 ",
        BABAS_STT_BASE_URL="https://api.groq.com/openai/v1",
        BABAS_STT_API_KEY=" gsk-key ",
        BABAS_STT_LANGUAGE="en",
    )
    assert s.stt_provider == "whisper"
    assert s.stt_model == "whisper-large-v3"
    assert s.stt_base_url == "https://api.groq.com/openai/v1"
    assert s.stt_api_key == "gsk-key"
    assert s.stt_language == "en"


@pytest.mark.parametrize("provider", ["auto", "chat", "whisper", "local", "off", "OFF", " Local "])
def test_every_speech_provider_is_accepted(provider):
    assert settings(BABAS_STT_PROVIDER=provider).stt_provider == provider.strip().lower()


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, None),
        ({"BABAS_LANG": "en"}, None),
        ({"BABAS_LANG": "en", "BABAS_STT_LANGUAGE": "es"}, "es"),
        ({"BABAS_STT_LANGUAGE": "ES-mx"}, "es-mx"),
        ({"BABAS_STT_LANGUAGE": "es-MX"}, "es-mx"),
        ({"BABAS_STT_LANGUAGE": "YUE"}, "yue"),
        ({"BABAS_STT_LANGUAGE": "zh-Hans"}, "zh-hans"),
        ({"BABAS_STT_LANGUAGE": "   "}, None),
    ],
)
def test_speech_language_is_only_set_when_configured_and_is_normalized(env, expected):
    assert settings(**env).stt_language == expected


def test_an_unset_speech_language_leaves_the_choice_to_the_caller():
    assert Settings().stt_language is None
    assert Settings(language="en").stt_language is None


def test_speech_endpoint_and_key_are_kept_apart_from_the_llm_ones():
    s = settings(BABAS_LLM_BASE_URL="http://localhost:11434/v1", OPENAI_API_KEY="sk-llm")
    assert s.llm_base_url == "http://localhost:11434/v1" and s.llm_api_key == "sk-llm"
    assert s.stt_base_url is None and s.stt_api_key is None


def test_blank_speech_values_count_as_unset():
    s = settings(BABAS_STT_MODEL="  ", BABAS_STT_BASE_URL="", BABAS_STT_API_KEY=" ", BABAS_STT_PROVIDER=" ")
    assert (s.stt_model, s.stt_base_url, s.stt_api_key, s.stt_provider) == (None, None, None, "auto")


@pytest.mark.parametrize(
    "name, value",
    [
        ("BABAS_STT_PROVIDER", "gemini"),
        ("BABAS_STT_PROVIDER", "on"),
        ("BABAS_STT_BASE_URL", "localhost:8000"),
        ("BABAS_STT_BASE_URL", "ftp://example.com/v1"),
        ("BABAS_STT_BASE_URL", "http://exa mple.com"),
        ("BABAS_STT_BASE_URL", "http://"),
        ("BABAS_STT_LANGUAGE", "e"),
        ("BABAS_STT_LANGUAGE", "spanish"),
        ("BABAS_STT_LANGUAGE", "es_MX"),
        ("BABAS_STT_LANGUAGE", "es-"),
        ("BABAS_STT_LANGUAGE", "es-M"),
        ("BABAS_STT_LANGUAGE", "es-MXXXX"),
        ("BABAS_STT_LANGUAGE", "12"),
        ("BABAS_STT_LANGUAGE", "es mx"),
    ],
)
def test_invalid_speech_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        settings(**{name: value})


def test_the_llm_url_error_message_is_unchanged():
    with pytest.raises(ValueError, match=r"BABAS_LLM_BASE_URL must be an http\(s\) URL, got 'localhost:11434'"):
        settings(BABAS_LLM_BASE_URL="localhost:11434")


def test_repr_never_contains_the_speech_key():
    s = settings(BABAS_STT_API_KEY="sk-speech-secret-value", BABAS_STT_MODEL="whisper-1")
    assert "sk-speech-secret-value" not in repr(s)
    assert "sk-speech-secret-value" not in str(s)
    assert "whisper-1" in repr(s)


def test_speech_variables_come_from_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BABAS_STT_PROVIDER", "local")
    monkeypatch.setenv("BABAS_STT_MODEL", "base")
    monkeypatch.setenv("BABAS_STT_LANGUAGE", "es-MX")
    s = Settings.from_env()
    assert (s.stt_provider, s.stt_model, s.stt_language) == ("local", "base", "es-mx")
