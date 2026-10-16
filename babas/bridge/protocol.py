import uuid
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8765
PINNED_EXTENSION_ID = "ooflejhodolbaphddedikkpleeephhmp"


class ErrorInfo(BaseModel):
    code: str
    message: str
    data: Optional[Dict[str, Any]] = None


class Envelope(BaseModel):
    v: Literal[1] = PROTOCOL_VERSION
    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)


class ResultMessage(BaseModel):
    v: Literal[1] = PROTOCOL_VERSION
    id: str = Field(min_length=1)
    type: Literal["result"] = "result"
    ok: bool
    payload: Optional[Dict[str, Any]] = None
    error: Optional[ErrorInfo] = None


class Hello(BaseModel):
    ext_version: str
    protocol: int
    token: Optional[str] = None


def new_id() -> str:
    return uuid.uuid4().hex


def request(type: str, payload: Optional[Dict[str, Any]] = None) -> Envelope:
    return Envelope(id=new_id(), type=type, payload=payload or {})


def parse_incoming(raw: Dict[str, Any]) -> "Envelope | ResultMessage":
    if raw.get("type") == "result":
        return ResultMessage.model_validate(raw)
    return Envelope.model_validate(raw)
