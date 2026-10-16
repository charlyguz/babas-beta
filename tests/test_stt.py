import asyncio
import base64
import gc
import io
import logging
import socket
import subprocess
import sys
import threading
import time
import types
import unicodedata
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Type

import openai
import pytest
from openai import AsyncOpenAI

from babas import stt
from babas.config import Settings
from babas.session import TRANSCRIPT_REPLY_TIMEOUT_S
from babas.stt import (
    AUTH_TEXT,
    BAD_AUDIO_TEXT,
    CANCEL_GRACE_S,
    CLIENT_TEXT,
    CONNECTION_TEXT,
    EMPTY_TEXT,
    ERROR_CODES,
    KEY_TEXT,
    LOCAL_MISSING_TEXT,
    MAX_TRANSCRIPT_CHARS,
    MODEL_TEXT,
    NO_BACKEND_TEXT,
    OFF_TEXT,
    PROMPT_EN,
    PROMPT_ES,
    PROVIDER_TEXT,
    RATE_TEXT,
    STT_DEADLINE_S,
    STT_HTTP_DEADLINE_S,
    STT_MAX_RETRIES,
    STT_TIMEOUT_S,
    TIMEOUT_TEXT,
    ChatAudioSTT,
    LocalWhisperSTT,
    SpeechToText,
    STTConfigError,
    STTError,
    WhisperApiSTT,
    clean_transcript,
    make_stt,
    resolve_stt,
    stt_status,
    with_deadline,
)
from fake_openai_server import CHAT_PATH, TRANSCRIPTIONS_PATH, FakeOpenAIServer

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEY = "sk-test-SECRET-0123456789"
SECRET_BODY = "my CURP is GOMC800101HDFRRL09"
SPOKEN = "frase privada del dictado"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/"
WAV = "audio/wav"


def make_wav(frames: int = 1600) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(bytes(index % 251 for index in range(frames * 2)) + b"\r\n\r\n--\r\n")
    return buffer.getvalue()


AUDIO = make_wav()


def settings(**env: str) -> Settings:
    return Settings.from_env(env)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "BABAS_LLM_MODEL", "BABAS_LLM_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def server():
    with FakeOpenAIServer() as running:
        yield running


@pytest.fixture
async def made():
    backends: List[Any] = []
    yield backends
    for backend in backends:
        await backend.close()


@pytest.fixture
def chat(server: FakeOpenAIServer, made: List[Any]):
    def build(**overrides: Any) -> ChatAudioSTT:
        options: Dict[str, Any] = {"base_url": server.base_url, "api_key": SECRET_KEY, "model": "chat-model", "language": "es"}
        options.update(overrides)
        made.append(ChatAudioSTT(**options))
        return made[-1]

    return build


@pytest.fixture
def whisper(server: FakeOpenAIServer, made: List[Any]):
    def build(**overrides: Any) -> WhisperApiSTT:
        options: Dict[str, Any] = {"base_url": server.base_url, "api_key": SECRET_KEY, "model": "whisper-1", "language": "es"}
        options.update(overrides)
        made.append(WhisperApiSTT(**options))
        return made[-1]

    return build


async def failure_of(backend: Any, audio: bytes = AUDIO, mime: str = WAV, language: str = "es") -> STTError:
    with pytest.raises(STTError) as info:
        await backend.transcribe(audio, mime, language)
    return info.value


def no_leak(*texts: str) -> None:
    for text in texts:
        for secret in (SECRET_KEY, SECRET_BODY, "Incorrect", SPOKEN):
            assert secret not in text


def test_error_classes_carry_a_code_and_a_message() -> None:
    error = STTError("provider", "No pude.")
    assert (error.code, error.message, str(error)) == ("provider", "No pude.", "No pude.")
    config = STTConfigError("Falta algo.")
    assert isinstance(config, STTError) and (config.code, config.message) == ("not_configured", "Falta algo.")
    assert ERROR_CODES == ("busy", "not_configured", "provider", "bad_audio", "empty", "timeout")


def test_every_user_text_is_plain_spanish_without_internals() -> None:
    texts = [BAD_AUDIO_TEXT, EMPTY_TEXT, PROVIDER_TEXT, AUTH_TEXT, RATE_TEXT, CONNECTION_TEXT, OFF_TEXT, NO_BACKEND_TEXT]
    texts += [KEY_TEXT, MODEL_TEXT, CLIENT_TEXT, LOCAL_MISSING_TEXT, stt.STT_BUSY_TEXT, stt.NOT_CONFIGURED_TEXT, TIMEOUT_TEXT]
    for text in texts:
        assert text.endswith((".", '"')) and "http" not in text.lower() and "sk-" not in text
    assert LOCAL_MISSING_TEXT.endswith('pip install -e ".[stt]"')


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  hola  ", "hola"),
        ('"hola"', "hola"),
        ("“hola”", "hola"),
        ("«hola»", "hola"),
        ("'hola'", "hola"),
        ("`hola`", "hola"),
        ('"“hola”"', "hola"),
        ('  " hola mundo "  ', "hola mundo"),
        ('dijo "hola" ayer', 'dijo "hola" ayer'),
        ('"Abre YouTube" y "busca recetas"', '"Abre YouTube" y "busca recetas"'),
        ("“Abre YouTube” y “busca recetas”", "“Abre YouTube” y “busca recetas”"),
        ("«Abre YouTube» y «busca recetas»", "«Abre YouTube» y «busca recetas»"),
        ("'abre' y 'busca'", "'abre' y 'busca'"),
        ('"hola" y "', '"hola" y "'),
        ('"dijo “hola” ayer"', "dijo “hola” ayer"),
        ('“Abre "YouTube" ahora”', 'Abre "YouTube" ahora'),
        ('"hola', '"hola'),
        ('hola"', 'hola"'),
        ("uno\ndos\r\ntres", "uno dos tres"),
        ("a  b\t c\n\n d", "a b c d"),
        ('""', ""),
        ("", ""),
        ("   \n ", ""),
        ("\x1b]0;PWNED\x07\x1b[2Jhola\rborrado\x08\x08", "hola borrado"),
        ("\x1b[31mrojo\x1b[0m y \x1b[1;4mazul\x1b[m", "rojo y azul"),
        ("\x1b]8;;http://x\x1b\\texto\x1b]8;;\x1b\\", "texto"),
        ("\x1bPdatos\x1b\\listo", "listo"),
        ("\x1bc\x1b7\x1b(Bhola", "hola"),
        ("\x9b2Jhola\x85mundo", "hola mundo"),
        ("ho\x07la\x00 mundo\x7f", "ho la mundo"),
        ("a\u202eb\u200bc\ufeffd\u2066e", "a b c d e"),
        ("hola\x1b", "hola"),
        ("\x1b[2J", ""),
        ("\x07\x08", ""),
        ("x" * (MAX_TRANSCRIPT_CHARS + 50), "x" * MAX_TRANSCRIPT_CHARS),
        ("a" * (MAX_TRANSCRIPT_CHARS - 1) + " b", "a" * (MAX_TRANSCRIPT_CHARS - 1)),
    ],
)
def test_clean_transcript(raw: str, expected: str) -> None:
    assert clean_transcript(raw) == expected


def test_clean_transcript_leaves_no_control_characters_in_hostile_text() -> None:
    fragments = ["\x1b", "\x1b[", "\x1b]", "\x1bP", "\x1b\\", "\x07", "\x08", "\x9b", "\x9d", "\x00", "\x7f", "\u202e", "\n", "\r", " ", "hola", "[0;31m", "0;x", '"', "«"]
    state = 12345
    for _ in range(300):
        pieces = []
        for _ in range(40):
            state = (state * 1103515245 + 12345) % (2**31)
            pieces.append(fragments[state % len(fragments)])
        cleaned = clean_transcript("".join(pieces))
        assert not any(unicodedata.category(char) in ("Cc", "Cf") for char in cleaned), repr(cleaned)
        assert cleaned == cleaned.strip() and "  " not in cleaned


@pytest.mark.parametrize("text", ["\x1b]" * 50000, "\x1bP" * 50000, "\x1b[" * 50000, "\x9b" * 200000, "\x1b]0;" * 50000, "\x9d" * 200000])
def test_clean_transcript_stays_fast_on_pathological_input(text: str) -> None:
    started = time.monotonic()
    clean_transcript(text)
    assert time.monotonic() - started < 3


def plan_of(**env: str) -> Any:
    return resolve_stt(settings(**env))


def shape(plan: Any) -> tuple:
    return plan.backend, plan.model, plan.base_url, plan.api_key


LOCAL_URL = "http://localhost:11434/v1"


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"OPENAI_API_KEY": "k"}, ("whisper", "whisper-1", None, "k")),
        ({"BABAS_LLM_API_KEY": "k", "BABAS_STT_API_KEY": "s"}, ("whisper", "whisper-1", None, "s")),
        ({"OPENAI_API_KEY": "k", "BABAS_STT_MODEL": "whisper-large"}, ("whisper", "whisper-large", None, "k")),
        (
            {"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_MODEL": "gemini-2.5-flash", "BABAS_LLM_API_KEY": "g"},
            ("chat", "gemini-2.5-flash", GEMINI, "g"),
        ),
        (
            {"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_MODEL": "gemini-2.5-flash", "BABAS_LLM_API_KEY": "g", "BABAS_STT_MODEL": "gemini-lite"},
            ("chat", "gemini-lite", GEMINI, "g"),
        ),
        (
            {"BABAS_STT_BASE_URL": GEMINI, "BABAS_STT_MODEL": "gemini-2.5-flash", "BABAS_STT_API_KEY": "g", "OPENAI_API_KEY": "o"},
            ("chat", "gemini-2.5-flash", GEMINI, "g"),
        ),
        (
            {"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "claude-x", "BABAS_STT_BASE_URL": GEMINI, "BABAS_STT_MODEL": "m", "BABAS_STT_API_KEY": "g"},
            ("chat", "m", GEMINI, "g"),
        ),
        ({"BABAS_LLM_BASE_URL": LOCAL_URL, "BABAS_LLM_MODEL": "qwen-omni"}, ("chat", "qwen-omni", LOCAL_URL, None)),
        ({"BABAS_LLM_BASE_URL": LOCAL_URL, "BABAS_STT_BASE_URL": "http://localhost:8000/v1", "BABAS_STT_MODEL": "m"}, ("chat", "m", "http://localhost:8000/v1", None)),
        ({"BABAS_LLM_BASE_URL": "https://api.openai.com/v1", "OPENAI_API_KEY": "k"}, ("whisper", "whisper-1", "https://api.openai.com/v1", "k")),
        ({"BABAS_STT_BASE_URL": "https://API.OpenAI.com/v1", "OPENAI_API_KEY": "k"}, ("whisper", "whisper-1", "https://API.OpenAI.com/v1", "k")),
        ({"BABAS_STT_PROVIDER": "chat", "BABAS_LLM_BASE_URL": LOCAL_URL}, ("chat", "gpt-5.2", LOCAL_URL, None)),
        ({"BABAS_STT_PROVIDER": "chat", "BABAS_STT_BASE_URL": LOCAL_URL, "BABAS_STT_MODEL": "m"}, ("chat", "m", LOCAL_URL, None)),
        (
            {"BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": "https://api.groq.com/openai/v1", "BABAS_STT_API_KEY": "gk", "BABAS_STT_MODEL": "whisper-large-v3"},
            ("whisper", "whisper-large-v3", "https://api.groq.com/openai/v1", "gk"),
        ),
        ({"BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": LOCAL_URL}, ("whisper", "whisper-1", LOCAL_URL, None)),
        (
            {"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "claude-x", "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_API_KEY": "s"},
            ("whisper", "whisper-1", None, "s"),
        ),
        ({"BABAS_STT_PROVIDER": "local"}, ("local", "small", None, None)),
        ({"BABAS_STT_PROVIDER": "local", "BABAS_STT_MODEL": "base", "OPENAI_API_KEY": "k", "BABAS_LLM_BASE_URL": GEMINI}, ("local", "base", None, None)),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m", "BABAS_STT_PROVIDER": "local"}, ("local", "small", None, None)),
    ],
)
def test_resolution_picks_the_backend_model_endpoint_and_key(env: Dict[str, str], expected: tuple) -> None:
    plan = plan_of(**env)
    assert plan.problem is None
    assert shape(plan) == expected


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, KEY_TEXT),
        ({"BABAS_STT_PROVIDER": "off", "OPENAI_API_KEY": "k"}, OFF_TEXT),
        ({"BABAS_STT_PROVIDER": "OFF"}, OFF_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m"}, NO_BACKEND_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m", "BABAS_LLM_BASE_URL": LOCAL_URL}, NO_BACKEND_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m", "BABAS_STT_API_KEY": "s", "BABAS_STT_MODEL": "x"}, NO_BACKEND_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "claude-x", "BABAS_STT_BASE_URL": LOCAL_URL}, MODEL_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "claude-x", "BABAS_STT_PROVIDER": "chat"}, MODEL_TEXT),
        ({"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_MODEL": "gemini"}, KEY_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m", "BABAS_STT_BASE_URL": GEMINI, "BABAS_STT_MODEL": "m"}, KEY_TEXT),
        ({"BABAS_STT_PROVIDER": "whisper"}, KEY_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m", "BABAS_STT_PROVIDER": "whisper"}, KEY_TEXT),
    ],
)
def test_resolution_explains_what_is_missing(env: Dict[str, str], expected: str) -> None:
    plan = plan_of(**env)
    assert plan.backend is None
    assert plan.problem == expected


OPENAI_V1 = "https://api.openai.com/v1"
GEMINI_NO_SLASH = "https://generativelanguage.googleapis.com/v1beta/openai"
LLM_KEY = "sk-REAL-LLM-KEY"


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": OPENAI_V1}, ("whisper", "whisper-1", OPENAI_V1, LLM_KEY)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": OPENAI_V1 + "/"}, ("whisper", "whisper-1", OPENAI_V1 + "/", LLM_KEY)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://api.openai.com:443/v1"}, ("whisper", "whisper-1", "https://api.openai.com:443/v1", LLM_KEY)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://API.OPENAI.COM./v1"}, ("whisper", "whisper-1", "https://API.OPENAI.COM./v1", LLM_KEY)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_LLM_BASE_URL": "https://API.OpenAI.com/v1/"}, ("whisper", "whisper-1", "https://API.OpenAI.com/v1/", LLM_KEY)),
        (
            {"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_MODEL": "gemini-2.5-flash", "BABAS_LLM_API_KEY": "g", "BABAS_STT_BASE_URL": GEMINI_NO_SLASH},
            ("chat", "gemini-2.5-flash", GEMINI_NO_SLASH, "g"),
        ),
        (
            {"BABAS_LLM_BASE_URL": "http://localhost/v1", "BABAS_LLM_MODEL": "omni", "BABAS_LLM_API_KEY": "l", "BABAS_STT_BASE_URL": "http://LOCALHOST:80/other/"},
            ("chat", "omni", "http://LOCALHOST:80/other/", "l"),
        ),
        (
            {"BABAS_LLM_BASE_URL": "https://llm.example/v1", "BABAS_LLM_MODEL": "omni", "BABAS_LLM_API_KEY": "l", "BABAS_STT_BASE_URL": "https://llm.example:443/v1"},
            ("chat", "omni", "https://llm.example:443/v1", "l"),
        ),
        (
            {"BABAS_LLM_BASE_URL": "https://llm.example/v1", "BABAS_LLM_MODEL": "omni", "BABAS_LLM_API_KEY": "l", "BABAS_STT_PROVIDER": "chat", "BABAS_STT_BASE_URL": "https://llm.example/v1"},
            ("chat", "omni", "https://llm.example/v1", "l"),
        ),
        (
            {"BABAS_LLM_BASE_URL": "https://llm.example/v1", "BABAS_LLM_API_KEY": "l", "BABAS_STT_BASE_URL": "https://llm.example/v1", "BABAS_STT_API_KEY": "s"},
            ("chat", "gpt-5.2", "https://llm.example/v1", "s"),
        ),
    ],
)
def test_the_llm_key_and_model_are_inherited_when_the_speech_endpoint_is_the_llm_endpoint(env: Dict[str, str], expected: tuple) -> None:
    plan = plan_of(**env)
    assert plan.problem is None
    assert shape(plan) == expected


OTHER = "https://stt.example/v1"


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": OTHER}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "chat", "BABAS_STT_BASE_URL": OTHER, "BABAS_STT_MODEL": "m"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": OTHER, "BABAS_STT_MODEL": "m"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://api.groq.com/openai/v1", "BABAS_STT_MODEL": "whisper-large-v3"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": GEMINI, "BABAS_STT_MODEL": "gemini-2.5-flash"}, KEY_TEXT),
        ({"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_MODEL": "gemini-2.5-flash", "BABAS_LLM_API_KEY": "g", "BABAS_STT_BASE_URL": OPENAI_V1}, KEY_TEXT),
        ({"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_API_KEY": "g", "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": OPENAI_V1}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "http://api.openai.com/v1"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://api.openai.com:8443/v1"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://eu.api.openai.com/v1", "BABAS_STT_MODEL": "m"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://api.openai.com.evil.example/v1", "BABAS_STT_PROVIDER": "whisper"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": "https://api.openai.com@evil.example/v1", "BABAS_STT_PROVIDER": "whisper"}, KEY_TEXT),
        ({"BABAS_LLM_BASE_URL": "https://llm.example/v1", "BABAS_LLM_API_KEY": "l", "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": "https://llm.example.net/v1"}, KEY_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": OTHER}, MODEL_TEXT),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "chat", "BABAS_STT_BASE_URL": LOCAL_URL}, MODEL_TEXT),
        ({"BABAS_LLM_BASE_URL": GEMINI, "BABAS_LLM_MODEL": "gemini-2.5-flash", "BABAS_LLM_API_KEY": "g", "BABAS_STT_BASE_URL": OTHER}, MODEL_TEXT),
        ({"BABAS_LLM_BASE_URL": LOCAL_URL, "BABAS_LLM_MODEL": "qwen-omni", "BABAS_STT_BASE_URL": "http://localhost:8000/v1"}, MODEL_TEXT),
    ],
)
def test_the_llm_key_and_model_are_never_sent_to_a_different_endpoint(env: Dict[str, str], expected: str) -> None:
    plan = plan_of(**env)
    assert plan.backend is None and plan.api_key is None
    assert plan.problem == expected
    assert LLM_KEY not in repr(plan)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": OTHER, "BABAS_STT_MODEL": "m", "BABAS_STT_API_KEY": "s"}, ("chat", "m", OTHER, "s")),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": OTHER, "BABAS_STT_API_KEY": "s"}, ("whisper", "whisper-1", OTHER, "s")),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_BASE_URL": OPENAI_V1, "BABAS_STT_API_KEY": "s"}, ("whisper", "whisper-1", OPENAI_V1, "s")),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "chat", "BABAS_STT_BASE_URL": LOCAL_URL, "BABAS_STT_MODEL": "m"}, ("chat", "m", LOCAL_URL, None)),
        ({"BABAS_LLM_BASE_URL": LOCAL_URL, "BABAS_LLM_API_KEY": "l", "BABAS_STT_BASE_URL": "http://localhost:8000/v1", "BABAS_STT_MODEL": "m"}, ("chat", "m", "http://localhost:8000/v1", None)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": "http://192.168.1.20:9000/v1"}, ("whisper", "whisper-1", "http://192.168.1.20:9000/v1", None)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": "http://whisper:9000/v1"}, ("whisper", "whisper-1", "http://whisper:9000/v1", None)),
        ({"OPENAI_API_KEY": LLM_KEY, "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": "http://[::1]:9000/v1"}, ("whisper", "whisper-1", "http://[::1]:9000/v1", None)),
    ],
)
def test_a_different_endpoint_gets_its_own_key_or_none_when_it_is_a_local_server(env: Dict[str, str], expected: tuple) -> None:
    plan = plan_of(**env)
    assert plan.problem is None
    assert shape(plan) == expected
    assert LLM_KEY not in repr(plan)


def test_the_reason_for_a_blocked_plan_names_the_cause_without_secrets() -> None:
    key = plan_of(OPENAI_API_KEY=LLM_KEY, BABAS_STT_PROVIDER="whisper", BABAS_STT_BASE_URL=OTHER)
    assert key.problem == KEY_TEXT and "BABAS_STT_API_KEY" in key.reason and "never sent to a different endpoint" in key.reason
    model = plan_of(OPENAI_API_KEY=LLM_KEY, BABAS_STT_BASE_URL=OTHER)
    assert model.problem == MODEL_TEXT and "BABAS_STT_MODEL" in model.reason and "LLM model" in model.reason
    for plan in (key, model):
        assert LLM_KEY not in plan.reason and OTHER not in plan.reason


def test_the_anthropic_key_is_never_a_fallback_for_the_speech_service() -> None:
    plan = plan_of(
        BABAS_LLM_PROVIDER="anthropic",
        BABAS_LLM_API_KEY="anthropic-secret",
        BABAS_LLM_MODEL="claude-x",
        BABAS_LLM_BASE_URL="https://proxy.example/anthropic",
        BABAS_STT_BASE_URL=LOCAL_URL,
        BABAS_STT_MODEL="m",
    )
    assert shape(plan) == ("chat", "m", LOCAL_URL, None)
    assert "anthropic-secret" not in repr(plan)


def test_the_plan_repr_hides_the_key() -> None:
    plan = plan_of(OPENAI_API_KEY=SECRET_KEY)
    assert plan.api_key == SECRET_KEY and SECRET_KEY not in repr(plan)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"OPENAI_API_KEY": "k"}, None),
        ({"OPENAI_API_KEY": "k", "BABAS_LANG": "en"}, None),
        ({"OPENAI_API_KEY": "k", "BABAS_STT_LANGUAGE": "en"}, "en"),
        ({"OPENAI_API_KEY": "k", "BABAS_LANG": "en", "BABAS_STT_LANGUAGE": "ES-mx"}, "es-mx"),
        ({"BABAS_STT_PROVIDER": "local", "BABAS_STT_LANGUAGE": "es"}, "es"),
        ({"BABAS_STT_PROVIDER": "local"}, None),
    ],
)
def test_resolution_carries_only_the_configured_language(env: Dict[str, str], expected: Optional[str]) -> None:
    assert plan_of(**env).language == expected


def test_a_blank_language_is_left_to_the_caller() -> None:
    bare = SimpleNamespace(**{**vars(replace(settings(OPENAI_API_KEY="k"))), "stt_language": ""})
    assert resolve_stt(bare).language is None  # type: ignore[arg-type]


def test_make_stt_builds_the_backend_that_was_resolved() -> None:
    chat_backend = make_stt(settings(BABAS_LLM_BASE_URL=GEMINI, BABAS_LLM_MODEL="gemini-x", BABAS_LLM_API_KEY="g", BABAS_STT_LANGUAGE="es-MX"))
    assert isinstance(chat_backend, ChatAudioSTT)
    assert (chat_backend.base_url, chat_backend.model, chat_backend.language) == (GEMINI, "gemini-x", "es-mx")
    whisper_backend = make_stt(settings(OPENAI_API_KEY="k"))
    assert isinstance(whisper_backend, WhisperApiSTT)
    assert (whisper_backend.base_url, whisper_backend.model, whisper_backend.language) == (None, "whisper-1", None)
    local_backend = make_stt(settings(BABAS_STT_PROVIDER="local", BABAS_STT_MODEL="base", BABAS_STT_LANGUAGE="en"))
    assert isinstance(local_backend, LocalWhisperSTT)
    assert (local_backend.model, local_backend.language) == ("base", "en")
    for backend in (chat_backend, whisper_backend, local_backend):
        assert isinstance(backend, SpeechToText)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, KEY_TEXT),
        ({"BABAS_STT_PROVIDER": "off"}, OFF_TEXT),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m"}, NO_BACKEND_TEXT),
    ],
)
async def test_make_stt_never_raises_for_configuration_problems(env: Dict[str, str], expected: str) -> None:
    backend = make_stt(settings(**env))
    assert isinstance(backend, SpeechToText)
    for _ in range(2):
        error = await failure_of(backend)
        assert isinstance(error, STTConfigError) and error.code == "not_configured" and error.message == expected
    await backend.close()
    await backend.close()


async def test_make_stt_survives_settings_it_cannot_understand(caplog: pytest.LogCaptureFixture) -> None:
    broken = SimpleNamespace()
    with caplog.at_level(logging.DEBUG, logger="babas"):
        backend = make_stt(broken)  # type: ignore[arg-type]
    error = await failure_of(backend)
    assert isinstance(error, STTConfigError) and error.message == stt.NOT_CONFIGURED_TEXT
    assert "AttributeError" in caplog.text
    unknown = make_stt(replace(settings(OPENAI_API_KEY="k"), stt_provider="bogus"))
    assert (await failure_of(unknown)).code == "not_configured"


@pytest.mark.parametrize(
    "env, fragment, level",
    [
        ({}, "BABAS_STT_API_KEY", logging.WARNING),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m"}, "BABAS_STT_PROVIDER", logging.WARNING),
        ({"OPENAI_API_KEY": SECRET_KEY, "BABAS_STT_BASE_URL": OTHER}, "BABAS_STT_MODEL", logging.WARNING),
        ({"OPENAI_API_KEY": SECRET_KEY, "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": OTHER}, "never sent to a different endpoint", logging.WARNING),
        ({"BABAS_STT_PROVIDER": "off"}, "BABAS_STT_PROVIDER=off", logging.INFO),
    ],
)
def test_make_stt_logs_why_it_is_not_usable_at_info_or_warning_without_secrets(
    caplog: pytest.LogCaptureFixture, env: Dict[str, str], fragment: str, level: int
) -> None:
    with caplog.at_level(logging.INFO, logger="babas.stt"):
        make_stt(settings(**env))
    (record,) = [record for record in caplog.records if "not usable" in record.getMessage()]
    assert record.levelno == level and fragment in record.getMessage()
    assert SECRET_KEY not in caplog.text and OTHER not in caplog.text


def test_an_unknown_provider_is_logged_by_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="babas.stt"):
        make_stt(replace(settings(OPENAI_API_KEY=SECRET_KEY), stt_provider="bogus"))
    assert "unknown BABAS_STT_PROVIDER 'bogus'" in caplog.text and SECRET_KEY not in caplog.text


async def test_the_not_configured_reason_is_logged_once_per_backend_and_not_per_click(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="babas.stt"):
        backend = make_stt(settings())
        for _ in range(3):
            await failure_of(backend)
    assert len([record for record in caplog.records if "not usable" in record.getMessage()]) == 1


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"OPENAI_API_KEY": "k"}, (True, None)),
        ({"BABAS_LLM_BASE_URL": LOCAL_URL, "BABAS_LLM_MODEL": "qwen-omni"}, (True, None)),
        ({}, (False, KEY_TEXT)),
        ({"BABAS_STT_PROVIDER": "off", "OPENAI_API_KEY": "k"}, (False, OFF_TEXT)),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "a", "BABAS_LLM_MODEL": "m"}, (False, NO_BACKEND_TEXT)),
        ({"OPENAI_API_KEY": "k", "BABAS_STT_PROVIDER": "whisper", "BABAS_STT_BASE_URL": OTHER}, (False, KEY_TEXT)),
        ({"OPENAI_API_KEY": "k", "BABAS_STT_BASE_URL": OTHER}, (False, MODEL_TEXT)),
    ],
)
async def test_stt_status_says_whether_dictation_can_work_and_why_not(env: Dict[str, str], expected: tuple, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="babas.stt"):
        assert stt_status(settings(**env)) == expected
    assert caplog.records == []
    ready, reason = expected
    backend = make_stt(settings(**env))
    if not ready:
        assert (await failure_of(backend)).message == reason
    await backend.close()


def test_stt_status_never_raises() -> None:
    assert stt_status(SimpleNamespace()) == (False, stt.NOT_CONFIGURED_TEXT)  # type: ignore[arg-type]


def test_stt_status_for_the_local_backend_checks_that_the_package_is_installed(
    monkeypatch: pytest.MonkeyPatch, faster_whisper: "FakeWhisperEnv"
) -> None:
    assert stt_status(settings(BABAS_STT_PROVIDER="local")) == (True, None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    assert stt_status(settings(BABAS_STT_PROVIDER="local")) == (False, LOCAL_MISSING_TEXT)
    assert stt_status(settings(OPENAI_API_KEY="k")) == (True, None)


async def test_make_stt_logs_the_backend_and_model_but_not_the_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="babas.stt"):
        make_stt(settings(OPENAI_API_KEY=SECRET_KEY))
    assert "backend=whisper model=whisper-1" in caplog.text
    assert SECRET_KEY not in caplog.text


async def test_chat_backend_sends_the_audio_part_and_returns_the_clean_text(server: FakeOpenAIServer, chat: Any) -> None:
    server.queue_completion('  "Quiero ver videos de cocina"\n')
    backend = chat()
    assert await backend.transcribe(AUDIO, WAV, "en") == "Quiero ver videos de cocina"

    (request,) = server.requests
    assert request.path == CHAT_PATH
    assert request.model == "chat-model"
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    assert request.audio_format == "wav"
    assert request.audio_bytes == len(AUDIO)
    assert base64.b64decode(request.input_audio["data"]) == AUDIO  # type: ignore[index]
    messages = request.body["messages"]
    assert [message["role"] for message in messages] == ["user"]
    assert [part["type"] for part in messages[0]["content"]] == ["text", "input_audio"]
    assert request.text_prompt == PROMPT_ES
    assert "[SIN_VOZ]" in PROMPT_ES and "[SIN_VOZ]" in PROMPT_EN
    assert request.response_format is None


@pytest.mark.parametrize(
    "configured, hint, expected",
    [("es", "en", PROMPT_ES), ("es-mx", "en", PROMPT_ES), (None, "es", PROMPT_ES), (None, "ES-MX", PROMPT_ES), ("en", "es", PROMPT_EN), (None, "en", PROMPT_EN), (None, "", PROMPT_EN), (None, "fr", PROMPT_EN)],
)
async def test_chat_prompt_follows_the_language(
    server: FakeOpenAIServer, chat: Any, configured: Optional[str], hint: str, expected: str
) -> None:
    server.queue_completion("hola")
    await chat(language=configured).transcribe(AUDIO, WAV, hint)
    assert server.requests[0].text_prompt == expected


@pytest.mark.parametrize("content", ["[SIN_VOZ]", " [sin_voz] ", '"[SIN_VOZ]"', "[SIN_VOZ].", "[ SIN VOZ ]", "[SIN_VOZ] No se oye nada.", "", "   \n", None])
async def test_chat_no_speech_and_empty_answers_are_the_empty_code(server: FakeOpenAIServer, chat: Any, content: Optional[str]) -> None:
    server.queue_completion(content)
    error = await failure_of(chat())
    assert (error.code, error.message) == ("empty", EMPTY_TEXT)


async def test_chat_a_person_saying_those_words_is_not_no_speech(server: FakeOpenAIServer, chat: Any) -> None:
    server.queue_completion("sin voz y sin voto")
    assert await chat().transcribe(AUDIO, WAV, "es") == "sin voz y sin voto"


async def test_chat_without_choices_is_a_provider_error(server: FakeOpenAIServer, chat: Any) -> None:
    server.queue_reply(200, {"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": []})
    error = await failure_of(chat())
    assert (error.code, error.message) == ("provider", PROVIDER_TEXT)


async def test_chat_refusal_is_a_provider_error_and_not_silence(server: FakeOpenAIServer, chat: Any, caplog: pytest.LogCaptureFixture) -> None:
    server.queue_completion(None, refusal="No puedo ayudar con eso.")
    with caplog.at_level(logging.WARNING, logger="babas.stt"):
        error = await failure_of(chat())
    assert (error.code, error.message) == ("provider", PROVIDER_TEXT)
    assert "refused" in caplog.text and "No puedo" not in caplog.text


async def test_chat_content_wins_over_a_refusal_field_and_an_empty_answer_stays_silence(server: FakeOpenAIServer, chat: Any) -> None:
    server.queue_completion("hola", refusal="no")
    server.queue_completion("", refusal="no")
    backend = chat()
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    assert (await failure_of(backend)).code == "empty"


async def test_chat_answer_cut_short_is_returned_with_a_warning(server: FakeOpenAIServer, chat: Any, caplog: pytest.LogCaptureFixture) -> None:
    server.queue_completion("quiero ver videos de", finish_reason="length")
    with caplog.at_level(logging.WARNING, logger="babas.stt"):
        assert await chat().transcribe(AUDIO, WAV, "es") == "quiero ver videos de"
    assert "finish_reason=length" in caplog.text and "quiero" not in caplog.text


async def test_chat_cleans_and_clips_long_answers(server: FakeOpenAIServer, chat: Any) -> None:
    server.queue_completion("línea uno\nlínea   dos\r\n")
    server.queue_completion("palabra " * 1000)
    backend = chat()
    assert await backend.transcribe(AUDIO, WAV, "es") == "línea uno línea dos"
    long = await backend.transcribe(AUDIO, WAV, "es")
    assert len(long) <= MAX_TRANSCRIPT_CHARS and long.endswith("palabra") and "\n" not in long


async def test_hostile_characters_in_a_chat_answer_never_reach_the_caller(server: FakeOpenAIServer, chat: Any) -> None:
    server.queue_completion("\x1b]0;PWNED\x07\x1b[2Jabre YouTube\x08\x08")
    assert await chat().transcribe(AUDIO, WAV, "es") == "abre YouTube"


async def test_whisper_backend_uploads_the_wav_and_returns_the_text(server: FakeOpenAIServer, whisper: Any) -> None:
    server.queue_transcription("  hola mundo \n")
    assert await whisper(language="es-MX").transcribe(AUDIO, WAV, "en") == "hola mundo"

    (request,) = server.requests
    assert request.path == TRANSCRIPTIONS_PATH
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    assert request.model == "whisper-1"
    assert request.language == "es"
    assert request.audio_bytes == len(AUDIO)
    assert request.audio_filename == "audio.wav" and request.audio_content_type == WAV
    assert request.body["response_format"] == "json"


@pytest.mark.parametrize(
    "configured, hint, expected",
    [("es", "en", "es"), (None, "en", "en"), (None, "ES-MX", "es"), ("en-US", "es", "en"), ("yue", "es", None), (None, "", None), (None, "x1", None)],
)
async def test_whisper_language_is_a_two_letter_code_or_absent(
    server: FakeOpenAIServer, whisper: Any, configured: Optional[str], hint: str, expected: Optional[str]
) -> None:
    server.queue_transcription("hola")
    await whisper(language=configured).transcribe(AUDIO, WAV, hint)
    assert server.requests[0].language == expected


@pytest.mark.parametrize("text", ["", "   ", "\n"])
async def test_whisper_empty_text_is_the_empty_code(server: FakeOpenAIServer, whisper: Any, text: str) -> None:
    server.queue_transcription(text)
    error = await failure_of(whisper())
    assert (error.code, error.message) == ("empty", EMPTY_TEXT)


async def test_whisper_does_not_treat_the_chat_marker_specially(server: FakeOpenAIServer, whisper: Any) -> None:
    server.queue_transcription("[SIN_VOZ]")
    assert await whisper().transcribe(AUDIO, WAV, "es") == "[SIN_VOZ]"


@pytest.mark.parametrize("response, expected", [("texto plano", "texto plano"), (SimpleNamespace(text=" objeto "), "objeto")])
async def test_whisper_accepts_a_plain_text_or_an_object_answer(whisper: Any, response: Any, expected: str) -> None:
    async def create(**options: Any) -> Any:
        return response

    backend = whisper()
    backend._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)), close=lambda: asyncio.sleep(0))
    assert await backend.transcribe(AUDIO, WAV, "es") == expected


async def test_whisper_answer_without_text_is_a_provider_error_and_not_silence(whisper: Any, caplog: pytest.LogCaptureFixture) -> None:
    async def create(**options: Any) -> Any:
        return SimpleNamespace(language="es")

    backend = whisper()
    backend._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)), close=lambda: asyncio.sleep(0))
    with caplog.at_level(logging.WARNING, logger="babas.stt"):
        error = await failure_of(backend)
    assert (error.code, error.message) == ("provider", PROVIDER_TEXT)
    assert "no text field" in caplog.text


async def test_make_stt_reaches_a_whisper_endpoint_with_the_default_model(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_transcription("hola")
    backend = make_stt(
        settings(BABAS_STT_PROVIDER="whisper", BABAS_STT_BASE_URL=server.base_url, BABAS_STT_API_KEY=SECRET_KEY, BABAS_STT_LANGUAGE="en")
    )
    made.append(backend)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    request = server.requests[0]
    assert (request.path, request.model, request.language) == (TRANSCRIPTIONS_PATH, "whisper-1", "en")
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"


async def test_make_stt_uses_the_llm_endpoint_model_and_key_for_chat_audio(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_completion("hola")
    backend = make_stt(settings(BABAS_LLM_BASE_URL=server.base_url, BABAS_LLM_MODEL="omni-model", BABAS_LLM_API_KEY=SECRET_KEY))
    made.append(backend)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    request = server.requests[0]
    assert (request.path, request.model) == (CHAT_PATH, "omni-model")
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"


async def test_a_local_endpoint_needs_no_key(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_completion("hola")
    backend = make_stt(settings(BABAS_LLM_BASE_URL=server.base_url, BABAS_LLM_MODEL="omni-model"))
    made.append(backend)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    assert server.requests[0].headers["authorization"] == "Bearer not-needed"


async def test_the_anthropic_key_never_reaches_the_speech_endpoint(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_completion("hola")
    backend = make_stt(
        settings(
            BABAS_LLM_PROVIDER="anthropic",
            BABAS_LLM_API_KEY=SECRET_KEY,
            BABAS_LLM_MODEL="claude-x",
            BABAS_STT_BASE_URL=server.base_url,
            BABAS_STT_MODEL="omni-model",
        )
    )
    made.append(backend)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    request = server.requests[0]
    assert request.model == "omni-model"
    assert SECRET_KEY not in str(request.headers) and request.headers["authorization"] == "Bearer not-needed"


async def test_the_llm_key_never_reaches_a_speech_endpoint_on_another_host(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_transcription("hola")
    backend = make_stt(settings(OPENAI_API_KEY=SECRET_KEY, BABAS_STT_PROVIDER="whisper", BABAS_STT_BASE_URL=server.base_url))
    made.append(backend)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    request = server.requests[0]
    assert SECRET_KEY not in str(request.headers) and request.headers["authorization"] == "Bearer not-needed"


async def test_the_llm_model_never_reaches_a_speech_endpoint_on_another_host(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_completion("hola")
    backend = make_stt(settings(OPENAI_API_KEY=SECRET_KEY, BABAS_LLM_MODEL="gpt-private", BABAS_STT_BASE_URL=server.base_url))
    made.append(backend)
    error = await failure_of(backend)
    assert (error.code, error.message) == ("not_configured", MODEL_TEXT)
    assert server.requests == []


async def test_the_llm_key_and_model_follow_to_the_same_endpoint_with_a_trailing_slash(server: FakeOpenAIServer, made: List[Any]) -> None:
    server.queue_completion("hola")
    backend = make_stt(
        settings(
            BABAS_LLM_BASE_URL=server.base_url,
            BABAS_LLM_API_KEY=SECRET_KEY,
            BABAS_LLM_MODEL="omni-model",
            BABAS_STT_BASE_URL=server.base_url + "/",
        )
    )
    made.append(backend)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    request = server.requests[0]
    assert request.model == "omni-model" and request.headers["authorization"] == f"Bearer {SECRET_KEY}"


@pytest.mark.parametrize(
    "env, hint, expected",
    [
        ({}, "en", "en"),
        ({}, "es", "es"),
        ({"BABAS_LANG": "es"}, "en", "en"),
        ({"BABAS_STT_LANGUAGE": "es"}, "en", "es"),
        ({"BABAS_LANG": "en", "BABAS_STT_LANGUAGE": "es-MX"}, "en", "es"),
        ({"BABAS_STT_LANGUAGE": "en-US"}, "es", "en"),
    ],
)
async def test_the_configured_language_wins_and_the_callers_language_is_the_fallback(
    server: FakeOpenAIServer, made: List[Any], env: Dict[str, str], hint: str, expected: str
) -> None:
    server.queue_transcription("hola")
    backend = make_stt(settings(BABAS_STT_PROVIDER="whisper", BABAS_STT_BASE_URL=server.base_url, BABAS_STT_API_KEY=SECRET_KEY, **env))
    made.append(backend)
    await backend.transcribe(AUDIO, WAV, hint)
    assert server.requests[0].language == expected


@pytest.mark.parametrize(
    "env, hint, expected",
    [({}, "en", PROMPT_EN), ({}, "es", PROMPT_ES), ({"BABAS_STT_LANGUAGE": "es"}, "en", PROMPT_ES), ({"BABAS_STT_LANGUAGE": "en"}, "es", PROMPT_EN)],
)
async def test_the_chat_prompt_follows_the_same_precedence_through_make_stt(
    server: FakeOpenAIServer, made: List[Any], env: Dict[str, str], hint: str, expected: str
) -> None:
    server.queue_completion("hola")
    backend = make_stt(settings(BABAS_STT_BASE_URL=server.base_url, BABAS_STT_MODEL="omni-model", **env))
    made.append(backend)
    await backend.transcribe(AUDIO, WAV, hint)
    assert server.requests[0].text_prompt == expected


@pytest.mark.parametrize("kind", ["chat", "whisper"])
@pytest.mark.parametrize(
    "status, expected",
    [(401, AUTH_TEXT), (403, AUTH_TEXT), (429, RATE_TEXT), (500, PROVIDER_TEXT), (503, PROVIDER_TEXT), (400, PROVIDER_TEXT), (404, PROVIDER_TEXT), (422, PROVIDER_TEXT)],
)
async def test_http_failures_become_a_fixed_spanish_provider_error(
    server: FakeOpenAIServer, chat: Any, whisper: Any, caplog: pytest.LogCaptureFixture, kind: str, status: int, expected: str
) -> None:
    server.queue_error(status, f"Incorrect API key provided: {SECRET_KEY}. Input was: {SECRET_BODY}", code="invalid_api_key", times=5)
    backend = chat() if kind == "chat" else whisper()
    with caplog.at_level(logging.DEBUG, logger="babas"):
        error = await failure_of(backend)

    assert (error.code, error.message) == ("provider", expected)
    assert error.__cause__ is None and error.__suppress_context__
    no_leak(error.message, str(error), caplog.text)
    assert f"HTTP {status}" in caplog.text
    assert len(server.requests) <= 1 + STT_MAX_RETRIES


@pytest.mark.parametrize("kind", ["chat", "whisper"])
@pytest.mark.parametrize(
    "status, fragment",
    [
        (401, "BABAS_STT_API_KEY"),
        (403, "BABAS_STT_API_KEY"),
        (404, "BABAS_STT_BASE_URL"),
        (400, "may not accept audio"),
        (415, "may not accept audio"),
        (422, "may not accept audio"),
        (429, "rate limiting"),
        (500, "server error"),
    ],
)
async def test_operator_logs_name_the_speech_service_and_the_setting_to_check(
    server: FakeOpenAIServer, chat: Any, whisper: Any, caplog: pytest.LogCaptureFixture, kind: str, status: int, fragment: str
) -> None:
    server.queue_error(status, "Incorrect API key provided", code="invalid_api_key", times=5)
    backend = chat() if kind == "chat" else whisper()
    with caplog.at_level(logging.INFO, logger="babas.stt"):
        await failure_of(backend)
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and fragment in warnings[0] and f"HTTP {status}" in warnings[0]
    assert f"({kind}, model" in warnings[0] and "Code: invalid_api_key" in warnings[0]
    assert "LLM" not in caplog.text and "Incorrect" not in caplog.text


@pytest.mark.parametrize("kind", ["chat", "whisper"])
async def test_a_transient_failure_is_retried_once(server: FakeOpenAIServer, chat: Any, whisper: Any, kind: str) -> None:
    server.queue_error(500)
    server.queue_completion("hola") if kind == "chat" else server.queue_transcription("hola")
    backend = chat() if kind == "chat" else whisper()
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    assert len(server.requests) == 2


@pytest.mark.parametrize("kind", ["chat", "whisper"])
async def test_an_unreachable_server_is_a_connection_error(chat: Any, whisper: Any, kind: str) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    options = {"base_url": f"http://127.0.0.1:{port}/v1", "max_retries": 0}
    error = await failure_of(chat(**options) if kind == "chat" else whisper(**options))
    assert (error.code, error.message) == ("provider", CONNECTION_TEXT)


async def test_a_slow_server_times_out_as_a_connection_error(made: List[Any]) -> None:
    with FakeOpenAIServer(delay=2.0) as slow:
        slow.queue_completion("tarde")
        backend = ChatAudioSTT(base_url=slow.base_url, api_key="k", model="m", timeout=0.2, max_retries=0)
        made.append(backend)
        error = await failure_of(backend)
    assert (error.code, error.message) == ("provider", CONNECTION_TEXT)


async def test_a_hung_server_is_cut_at_the_deadline_before_the_sdk_timeout(made: List[Any], caplog: pytest.LogCaptureFixture) -> None:
    with FakeOpenAIServer(delay=30.0) as hung:
        hung.queue_completion("tarde", times=2)
        backend = ChatAudioSTT(base_url=hung.base_url, api_key="k", model="m", timeout=5.0, max_retries=0, deadline=0.4)
        made.append(backend)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="babas.stt"):
            error = await failure_of(backend)
        elapsed = time.monotonic() - started
    assert (error.code, error.message) == ("timeout", TIMEOUT_TEXT)
    assert 0.3 < elapsed < 3
    assert "gave up after 0.4 s" in caplog.text and "chat" in caplog.text


@pytest.mark.parametrize("kind", ["chat", "whisper"])
async def test_a_long_retry_after_cannot_outlast_the_deadline(server: FakeOpenAIServer, chat: Any, whisper: Any, kind: str) -> None:
    server.queue_error(429, "slow down", headers={"retry-after-ms": "100000"})
    server.queue_completion("hola") if kind == "chat" else server.queue_transcription("hola")
    backend = (chat if kind == "chat" else whisper)(deadline=0.5)
    started = time.monotonic()
    error = await asyncio.wait_for(failure_of(backend), 10)
    assert time.monotonic() - started < 3
    assert (error.code, error.message) == ("timeout", TIMEOUT_TEXT)
    assert len(server.requests) == 1


@pytest.mark.parametrize("kind", ["chat", "whisper"])
async def test_a_short_retry_after_still_fits_inside_the_deadline(server: FakeOpenAIServer, chat: Any, whisper: Any, kind: str) -> None:
    server.queue_error(429, "slow down", headers={"retry-after-ms": "200"})
    server.queue_completion("hola") if kind == "chat" else server.queue_transcription("hola")
    backend = (chat if kind == "chat" else whisper)(deadline=5.0)
    assert await backend.transcribe(AUDIO, WAV, "es") == "hola"
    assert len(server.requests) == 2


async def test_with_deadline_returns_results_and_raises_errors_untouched() -> None:
    async def fine() -> str:
        await asyncio.sleep(0)
        return "listo"

    async def broken() -> str:
        raise ValueError("mala")

    assert await with_deadline(fine(), 5) == "listo"
    with pytest.raises(ValueError, match="mala"):
        await with_deadline(broken(), 5)


async def test_with_deadline_cancels_the_work_that_runs_out_of_time() -> None:
    cancelled = asyncio.Event()

    async def hangs() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    started = time.monotonic()
    with pytest.raises(STTError) as info:
        await with_deadline(hangs(), 0.2)
    assert (info.value.code, info.value.message) == ("timeout", TIMEOUT_TEXT)
    assert cancelled.is_set() and time.monotonic() - started < 2


async def test_with_deadline_does_not_wait_forever_for_work_that_ignores_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt, "CANCEL_GRACE_S", 0.1)
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue

    started = time.monotonic()
    with pytest.raises(STTError) as info:
        await with_deadline(stubborn(), 0.1)
    assert info.value.code == "timeout" and time.monotonic() - started < 2
    release.set()
    await asyncio.sleep(0.1)


async def test_with_deadline_passes_an_outer_cancellation_to_the_work_and_is_not_swallowed() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hangs() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    outer = asyncio.create_task(with_deadline(hangs(), 30))
    await asyncio.wait_for(started.wait(), 2)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    await asyncio.wait_for(cancelled.wait(), 2)


async def test_with_deadline_does_not_leave_unretrieved_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    async def explode_on_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(STTError):
            await with_deadline(explode_on_cancel(), 0.1)
        await asyncio.sleep(0.05)
        gc.collect()
    assert "never retrieved" not in caplog.text


def test_the_deadlines_fit_inside_the_service_worker_timeout() -> None:
    assert STT_HTTP_DEADLINE_S < STT_DEADLINE_S
    assert STT_DEADLINE_S + CANCEL_GRACE_S + TRANSCRIPT_REPLY_TIMEOUT_S < 90


async def test_unexpected_exceptions_do_not_leak_their_text(chat: Any, caplog: pytest.LogCaptureFixture) -> None:
    async def create(**options: Any) -> Any:
        raise RuntimeError(f"boom {SECRET_KEY} {SECRET_BODY}")

    backend = chat()
    backend._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: asyncio.sleep(0))
    with caplog.at_level(logging.DEBUG, logger="babas"):
        error = await failure_of(backend)
    assert (error.code, error.message) == ("provider", PROVIDER_TEXT)
    no_leak(error.message, str(error), caplog.text)
    assert "RuntimeError" in caplog.text


async def test_an_openai_error_without_status_is_described_safely(chat: Any, caplog: pytest.LogCaptureFixture) -> None:
    async def create(**options: Any) -> Any:
        raise openai.OpenAIError(f"weird {SECRET_KEY}")

    backend = chat()
    backend._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: asyncio.sleep(0))
    with caplog.at_level(logging.DEBUG, logger="babas"):
        error = await failure_of(backend)
    assert error.message == PROVIDER_TEXT
    no_leak(error.message, caplog.text)
    assert "OpenAIError" in caplog.text


async def test_cancellation_is_not_swallowed(chat: Any) -> None:
    started = asyncio.Event()

    async def create(**options: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    backend = chat()
    backend._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: asyncio.sleep(0))
    task = asyncio.create_task(backend.transcribe(AUDIO, WAV, "es"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("kind", ["chat", "whisper"])
async def test_a_missing_key_is_a_configuration_error_before_any_request(server: FakeOpenAIServer, chat: Any, whisper: Any, kind: str) -> None:
    backend = (chat if kind == "chat" else whisper)(api_key=None, require_key=True)
    error = await failure_of(backend)
    assert isinstance(error, STTConfigError) and error.message == KEY_TEXT
    assert server.requests == []


class RecordingOpenAI(AsyncOpenAI):
    built: List[dict] = []
    closed = 0

    def __init__(self, **kwargs: Any) -> None:
        RecordingOpenAI.built.append(kwargs)
        super().__init__(**kwargs)

    async def close(self) -> None:
        RecordingOpenAI.closed += 1
        await super().close()


async def test_the_client_is_built_once_with_the_configured_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch, server: FakeOpenAIServer
) -> None:
    RecordingOpenAI.built, RecordingOpenAI.closed = [], 0
    monkeypatch.setattr(stt, "AsyncOpenAI", RecordingOpenAI)
    server.queue("uno", "dos")
    backend = ChatAudioSTT(base_url=server.base_url, api_key=SECRET_KEY, model="m")
    assert RecordingOpenAI.built == []
    await backend.transcribe(AUDIO, WAV, "es")
    await backend.transcribe(AUDIO, WAV, "es")
    assert RecordingOpenAI.built == [
        {"base_url": server.base_url, "api_key": SECRET_KEY, "timeout": STT_TIMEOUT_S, "max_retries": STT_MAX_RETRIES}
    ]

    await backend.close()
    await backend.close()
    assert RecordingOpenAI.closed == 1
    server.queue_completion("tres")
    assert await backend.transcribe(AUDIO, WAV, "es") == "tres"
    assert len(RecordingOpenAI.built) == 2
    await backend.close()


async def test_a_client_that_cannot_be_built_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def broken(**kwargs: Any) -> Any:
        raise ValueError(f"bad url {SECRET_KEY}")

    monkeypatch.setattr(stt, "AsyncOpenAI", broken)
    backend = WhisperApiSTT(base_url="http://x/v1", api_key=SECRET_KEY, model="m")
    with caplog.at_level(logging.DEBUG, logger="babas"):
        error = await failure_of(backend)
    assert isinstance(error, STTConfigError) and error.message == CLIENT_TEXT
    no_leak(error.message, caplog.text)


async def test_a_failing_close_is_harmless(chat: Any, caplog: pytest.LogCaptureFixture) -> None:
    async def explode() -> None:
        raise RuntimeError(f"cannot close {SECRET_KEY}")

    backend = chat()
    backend._client = SimpleNamespace(close=explode)
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await backend.close()
    assert SECRET_KEY not in caplog.text and "RuntimeError" in caplog.text


@pytest.mark.parametrize("kind", ["chat", "whisper", "local"])
@pytest.mark.parametrize(
    "audio, mime",
    [(b"", WAV), (AUDIO, "audio/webm"), (AUDIO, ""), (AUDIO, "text/plain"), (AUDIO, None)],
)
async def test_bad_audio_is_refused_before_any_work(
    server: FakeOpenAIServer, chat: Any, whisper: Any, kind: str, audio: bytes, mime: Any
) -> None:
    backend = chat() if kind == "chat" else whisper() if kind == "whisper" else LocalWhisperSTT()
    error = await failure_of(backend, audio, mime)
    assert (error.code, error.message) == ("bad_audio", BAD_AUDIO_TEXT)
    assert server.requests == []


@pytest.mark.parametrize("mime", ["audio/wav", "AUDIO/WAV", "audio/x-wav", " audio/wave "])
async def test_wav_mime_types_are_accepted(server: FakeOpenAIServer, whisper: Any, mime: str) -> None:
    server.queue_transcription("hola")
    assert await whisper().transcribe(AUDIO, mime, "es") == "hola"


@pytest.mark.parametrize("kind", ["chat", "whisper"])
async def test_nothing_private_reaches_the_logs_on_success_or_failure(
    server: FakeOpenAIServer, chat: Any, whisper: Any, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    backend = chat() if kind == "chat" else whisper()
    queue = server.queue_completion if kind == "chat" else server.queue_transcription
    queue(SPOKEN)
    server.queue_error(401, f"{SPOKEN} {SECRET_BODY} {SECRET_KEY}")
    with caplog.at_level(logging.DEBUG):
        assert await backend.transcribe(AUDIO, WAV, "es") == SPOKEN
        await failure_of(backend)
    text = caplog.text
    assert "babas.stt" in text and f"{len(AUDIO)} audio bytes" in text
    no_leak(text)
    assert base64.b64encode(AUDIO).decode()[:60] not in text and "RIFF" not in text


class FakeWhisperEnv:
    def __init__(self) -> None:
        self.imports: List[str] = []
        self.models: List[Any] = []
        self.release: Optional[threading.Event] = None
        self.constructor_error: Optional[BaseException] = None
        self.transcribe_error: Optional[BaseException] = None
        self.words: List[str] = [" Hola", " mundo ", "  "]
        self.segment_threads: List[threading.Thread] = []
        env = self

        class Model:
            def __init__(self, size: str, device: str = "auto", compute_type: str = "default") -> None:
                if env.constructor_error is not None:
                    raise env.constructor_error
                self.args = (size, device, compute_type)
                self.calls: List[Dict[str, Any]] = []
                env.models.append(self)

            def transcribe(self, audio: Any, language: Optional[str] = None, vad_filter: bool = False) -> Any:
                try:
                    asyncio.get_running_loop()
                    in_loop = True
                except RuntimeError:
                    in_loop = False
                self.calls.append(
                    {"thread": threading.current_thread(), "in_loop": in_loop, "language": language, "vad_filter": vad_filter, "audio": audio.read()}
                )
                if env.release is not None:
                    env.release.wait(5)
                if env.transcribe_error is not None:
                    raise env.transcribe_error

                def segments() -> Iterator[Any]:
                    for word in env.words:
                        env.segment_threads.append(threading.current_thread())
                        yield SimpleNamespace(text=word)

                return segments(), SimpleNamespace(language=language)

        self.model_class = Model

    def module(self) -> types.ModuleType:
        module = types.ModuleType("faster_whisper")

        def lookup(name: str) -> Any:
            if name == "WhisperModel":
                self.imports.append(name)
                return self.model_class
            raise AttributeError(name)

        module.__getattr__ = lookup  # type: ignore[method-assign]
        return module


@pytest.fixture
def faster_whisper(monkeypatch: pytest.MonkeyPatch) -> FakeWhisperEnv:
    env = FakeWhisperEnv()
    monkeypatch.setitem(sys.modules, "faster_whisper", env.module())
    return env


def test_importing_the_module_never_imports_the_optional_package() -> None:
    code = "import sys, babas.stt, babas.session; assert 'faster_whisper' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


async def test_local_backend_imports_and_loads_the_model_lazily(faster_whisper: FakeWhisperEnv) -> None:
    backend = make_stt(settings(BABAS_STT_PROVIDER="local"))
    assert isinstance(backend, LocalWhisperSTT)
    assert faster_whisper.imports == [] and faster_whisper.models == []
    assert await backend.transcribe(AUDIO, WAV, "es") == "Hola mundo"
    assert faster_whisper.imports == ["WhisperModel"]
    assert [model.args for model in faster_whisper.models] == [("small", "cpu", "int8")]


async def test_local_backend_caches_the_model_until_closed(faster_whisper: FakeWhisperEnv) -> None:
    backend = LocalWhisperSTT(model="base", language="es")
    for _ in range(3):
        await backend.transcribe(AUDIO, WAV, "es")
    assert len(faster_whisper.models) == 1 and len(faster_whisper.models[0].calls) == 3
    assert faster_whisper.models[0].args[0] == "base"

    await backend.close()
    await backend.transcribe(AUDIO, WAV, "es")
    assert len(faster_whisper.models) == 2


async def test_local_backend_passes_the_language_vad_and_audio(faster_whisper: FakeWhisperEnv) -> None:
    configured = LocalWhisperSTT(language="es-MX")
    await configured.transcribe(AUDIO, WAV, "en")
    hinted = LocalWhisperSTT(language=None)
    await hinted.transcribe(AUDIO, WAV, "EN-us")
    unknown = LocalWhisperSTT(language="yue")
    await unknown.transcribe(AUDIO, WAV, "es")
    calls = [model.calls[0] for model in faster_whisper.models]
    assert [call["language"] for call in calls] == ["es", "en", None]
    assert all(call["vad_filter"] is True and call["audio"] == AUDIO for call in calls)


async def test_local_backend_works_off_the_event_loop(faster_whisper: FakeWhisperEnv) -> None:
    faster_whisper.release = threading.Event()
    backend = LocalWhisperSTT()
    task = asyncio.create_task(backend.transcribe(AUDIO, WAV, "es"))
    await asyncio.sleep(0.1)
    assert not task.done()
    faster_whisper.release.set()
    assert await asyncio.wait_for(task, 5) == "Hola mundo"

    (call,) = faster_whisper.models[0].calls
    assert call["thread"] is not threading.current_thread() and call["in_loop"] is False
    assert faster_whisper.segment_threads and all(thread is not threading.current_thread() for thread in faster_whisper.segment_threads)


async def test_local_backend_announces_the_possible_download_once(faster_whisper: FakeWhisperEnv, caplog: pytest.LogCaptureFixture) -> None:
    backend = LocalWhisperSTT(model="small")
    with caplog.at_level(logging.INFO, logger="babas.stt"):
        for _ in range(3):
            await backend.transcribe(AUDIO, WAV, "es")
        await backend.close()
        await backend.transcribe(AUDIO, WAV, "es")
    notes = [record for record in caplog.records if "may download" in record.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.INFO and "small" in notes[0].getMessage()
    assert "Hola" not in caplog.text


async def test_local_backend_without_the_package_explains_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    backend = make_stt(settings(BABAS_STT_PROVIDER="local"))
    error = await failure_of(backend)
    assert isinstance(error, STTConfigError) and error.code == "not_configured"
    assert error.message == LOCAL_MISSING_TEXT
    assert 'pip install -e ".[stt]"' in error.message and error.message.startswith("Falta instalar")


async def test_local_backend_recovers_once_the_package_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    backend = LocalWhisperSTT()
    assert (await failure_of(backend)).code == "not_configured"
    env = FakeWhisperEnv()
    monkeypatch.setitem(sys.modules, "faster_whisper", env.module())
    assert await backend.transcribe(AUDIO, WAV, "es") == "Hola mundo"


async def test_local_model_load_failure_is_a_provider_error_and_is_retried(
    faster_whisper: FakeWhisperEnv, caplog: pytest.LogCaptureFixture
) -> None:
    faster_whisper.constructor_error = OSError(f"cannot download {SECRET_BODY}")
    backend = LocalWhisperSTT()
    with caplog.at_level(logging.DEBUG, logger="babas"):
        error = await failure_of(backend)
    assert (error.code, error.message) == ("provider", PROVIDER_TEXT)
    no_leak(error.message, caplog.text)
    assert "OSError" in caplog.text

    faster_whisper.constructor_error = None
    assert await backend.transcribe(AUDIO, WAV, "es") == "Hola mundo"


async def test_local_decode_failure_is_a_provider_error(faster_whisper: FakeWhisperEnv, caplog: pytest.LogCaptureFixture) -> None:
    faster_whisper.transcribe_error = ValueError(f"cannot decode {SECRET_BODY}")
    backend = LocalWhisperSTT()
    with caplog.at_level(logging.DEBUG, logger="babas"):
        error = await failure_of(backend)
    assert (error.code, error.message) == ("provider", PROVIDER_TEXT)
    no_leak(error.message, caplog.text)


@pytest.mark.parametrize("words", [[], ["  ", ""], [" \n "]])
async def test_local_backend_without_speech_is_the_empty_code(faster_whisper: FakeWhisperEnv, words: List[str]) -> None:
    faster_whisper.words = words
    error = await failure_of(LocalWhisperSTT())
    assert (error.code, error.message) == ("empty", EMPTY_TEXT)


async def test_local_backend_cleans_and_clips_the_text(faster_whisper: FakeWhisperEnv) -> None:
    faster_whisper.words = [" uno\ndos ", "x" * 5000]
    text = await LocalWhisperSTT().transcribe(AUDIO, WAV, "es")
    assert text.startswith("uno dos xxx") and len(text) == MAX_TRANSCRIPT_CHARS


async def test_local_transcripts_are_not_logged(faster_whisper: FakeWhisperEnv, caplog: pytest.LogCaptureFixture) -> None:
    faster_whisper.words = [SPOKEN]
    with caplog.at_level(logging.DEBUG):
        assert await LocalWhisperSTT().transcribe(AUDIO, WAV, "es") == SPOKEN
    assert "babas.stt" in caplog.text and SPOKEN not in caplog.text


def test_the_local_dependency_is_an_optional_extra_and_not_part_of_dev() -> None:
    tomllib = pytest.importorskip("tomllib")
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    assert extras["stt"] == ["faster-whisper>=1.0"]
    assert not any("faster-whisper" in requirement for requirement in extras["dev"])
