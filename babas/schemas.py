import re
from typing import Annotated, ClassVar, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from babas.drivers.base import Target

WAIT_DEFAULT_MS = 2000
WAIT_MAX_MS = 30000
SCROLL_DEFAULT_PIXELS = 600
SCROLL_MAX_PIXELS = 10000

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")
_KEY_ALIASES = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "pagedown": "PageDown",
    "pageup": "PageUp",
}


class StepBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    EXAMPLE: ClassVar[dict]
    CHANGES_PAGE: ClassVar[bool] = True

    explanation: Optional[str] = Field(
        default=None,
        description="One or two short, friendly sentences for the user about what this step does.",
    )
    consequential: bool = Field(
        default=False,
        description="True when the step sends, submits, pays, buys, deletes, posts, signs or books something.",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data):
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class Targeted(StepBase):
    TARGET_REQUIRED: ClassVar[bool] = True

    index: Optional[int] = Field(default=None, description="Element index from CURRENT PAGE (preferred).")
    selector: Optional[str] = Field(default=None, description="CSS selector, only when no index is available.")
    text: Optional[str] = Field(default=None, description="Visible text of the element, as a last resort.")

    @field_validator("selector", "text", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_target(self):
        if self.TARGET_REQUIRED and self.index is None and not self.selector and not self.text:
            action = getattr(self, "action", "this step")
            raise ValueError(f"{action} needs an index, a selector or a text")
        return self

    def to_target(self, snapshot_id: Optional[str]) -> Optional[Target]:
        if self.index is not None:
            return Target(index=self.index, snapshot_id=snapshot_id)
        if self.selector:
            return Target(selector=self.selector)
        if self.text:
            return Target(text=self.text)
        return None


class GotoStep(StepBase):
    """Open a web address in the current tab (https:// is added when it is missing)."""

    action: Literal["goto"]
    value: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "goto",
        "value": "https://www.gob.mx",
        "explanation": "Voy a abrir la página principal del Gobierno de México.",
    }

    @field_validator("value")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            raise ValueError("goto needs a URL")
        if url.startswith("//"):
            url = "https:" + url
        match = _SCHEME.match(url)
        if match is None:
            return "https://" + url
        if match.group(1).lower() not in ("http", "https"):
            raise ValueError("goto only opens http or https addresses")
        return url


class WaitStep(StepBase):
    """Wait a number of milliseconds (0 to 30000), for example for a page to finish loading."""

    CHANGES_PAGE: ClassVar[bool] = False

    action: Literal["wait"]
    ms: int = WAIT_DEFAULT_MS

    EXAMPLE: ClassVar[dict] = {
        "action": "wait",
        "ms": 2000,
        "explanation": "Espero un momento a que la página termine de cargar.",
    }

    @field_validator("ms")
    @classmethod
    def _clamp_ms(cls, value: int) -> int:
        return max(0, min(value, WAIT_MAX_MS))


class ClickStep(Targeted):
    """Click an element, given by its index from CURRENT PAGE (or a CSS selector, or its visible text)."""

    action: Literal["click"]

    EXAMPLE: ClassVar[dict] = {
        "action": "click",
        "index": 4,
        "explanation": "Voy a presionar el botón Buscar.",
    }


class TypeTextStep(Targeted):
    """Type value into an input field, given by its index (or selector, or text); never for passwords."""

    CHANGES_PAGE: ClassVar[bool] = False

    action: Literal["type_text"]
    value: str

    EXAMPLE: ClassVar[dict] = {
        "action": "type_text",
        "index": 2,
        "value": "recetas de pozole",
        "explanation": "Voy a escribir en el cuadro lo que quiere buscar.",
    }


class PressKeyStep(Targeted):
    """Press a key (Enter, Tab, Escape, Backspace, PageDown, PageUp or one character), optionally on an element."""

    TARGET_REQUIRED: ClassVar[bool] = False

    action: Literal["press_key"]
    value: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "press_key",
        "value": "Enter",
        "index": 2,
        "explanation": "Presiono Enter para iniciar la búsqueda.",
    }

    @field_validator("value")
    @classmethod
    def _canonical_key(cls, value: str) -> str:
        return _KEY_ALIASES.get(value.strip().lower(), value)


class ScreenshotStep(StepBase):
    """Save a picture of the current page to a file."""

    CHANGES_PAGE: ClassVar[bool] = False

    action: Literal["screenshot"]

    EXAMPLE: ClassVar[dict] = {
        "action": "screenshot",
        "explanation": "Voy a guardar una imagen de lo que se ve en la pantalla.",
    }


class ScrollStep(StepBase):
    """Scroll the page up or down; ms is the distance in pixels (default 600)."""

    CHANGES_PAGE: ClassVar[bool] = False

    action: Literal["scroll"]
    value: Literal["up", "down"] = "down"
    ms: Optional[int] = SCROLL_DEFAULT_PIXELS

    EXAMPLE: ClassVar[dict] = {
        "action": "scroll",
        "value": "down",
        "ms": 600,
        "explanation": "Bajo un poco para ver más resultados.",
    }

    @field_validator("value", mode="before")
    @classmethod
    def _lower_direction(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("ms")
    @classmethod
    def _clamp_pixels(cls, value: Optional[int]) -> int:
        if value is None:
            return SCROLL_DEFAULT_PIXELS
        return max(1, min(value, SCROLL_MAX_PIXELS))


class SmartSearchStep(StepBase):
    """Find the search box on the page, type value in it and press Enter."""

    action: Literal["smart_search"]
    value: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "smart_search",
        "value": "clima en Guadalajara",
        "explanation": "Voy a escribir su búsqueda en el cuadro de búsqueda de la página.",
    }


class SmartClickStep(StepBase):
    """Click the element whose visible text is value."""

    action: Literal["smart_click"]
    value: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "smart_click",
        "value": "Consultar CURP",
        "explanation": "Voy a presionar el enlace que dice Consultar CURP.",
    }


class AnalyzePageStep(StepBase):
    """Look at the page again to refresh the list of elements."""

    action: Literal["analyze_page"]

    EXAMPLE: ClassVar[dict] = {
        "action": "analyze_page",
        "explanation": "Reviso de nuevo la página para ver qué puedo usar.",
    }


class NewTabStep(StepBase):
    """Open a new tab, optionally named with tab_name."""

    action: Literal["new_tab"]
    tab_name: Optional[str] = None
    value: Optional[str] = None

    EXAMPLE: ClassVar[dict] = {
        "action": "new_tab",
        "tab_name": "correo",
        "explanation": "Abro una pestaña nueva para no perder la que tiene abierta.",
    }


class SwitchTabStep(StepBase):
    """Switch to the tab called tab_name."""

    action: Literal["switch_tab"]
    tab_name: Optional[str] = None
    value: Optional[str] = None

    EXAMPLE: ClassVar[dict] = {
        "action": "switch_tab",
        "tab_name": "correo",
        "explanation": "Regreso a la pestaña del correo.",
    }


class CloseTabStep(StepBase):
    """Close the current tab, or the tab called tab_name."""

    action: Literal["close_tab"]
    tab_name: Optional[str] = None
    value: Optional[str] = None

    EXAMPLE: ClassVar[dict] = {
        "action": "close_tab",
        "explanation": "Cierro la pestaña que ya no necesitamos.",
    }


class SearchYoutubeStep(StepBase):
    """Open YouTube and search for value."""

    action: Literal["search_youtube"]
    value: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "search_youtube",
        "value": "canciones de José José",
        "explanation": "Voy a buscar esas canciones en YouTube.",
    }


class ClickFirstVideoStep(StepBase):
    """Open the first video in the YouTube search results."""

    action: Literal["click_first_video"]

    EXAMPLE: ClassVar[dict] = {
        "action": "click_first_video",
        "explanation": "Voy a abrir el primer video de la lista.",
    }


class OpenGmailStep(StepBase):
    """Open Gmail."""

    action: Literal["open_gmail"]

    EXAMPLE: ClassVar[dict] = {
        "action": "open_gmail",
        "explanation": "Voy a abrir su correo de Gmail.",
    }


class ComposeEmailStep(StepBase):
    """Start a new Gmail message with the recipient, subject and body filled in (it does not send it)."""

    action: Literal["compose_email"]
    to: str = Field(min_length=1)
    subject: Optional[str] = None
    body: Optional[str] = None

    EXAMPLE: ClassVar[dict] = {
        "action": "compose_email",
        "to": "ana@example.com",
        "subject": "Reunión del sábado",
        "body": "Hola Ana, ¿nos vemos el sábado a las 5?",
        "explanation": "Voy a preparar el correo para Ana. Todavía no lo envío.",
    }


class SendEmailStep(StepBase):
    """Send the Gmail message that is being written (the user is always asked first)."""

    action: Literal["send_email"]

    EXAMPLE: ClassVar[dict] = {
        "action": "send_email",
        "consequential": True,
        "explanation": "Voy a enviar el correo, si usted está de acuerdo.",
    }


class OpenWhatsappStep(StepBase):
    """Open WhatsApp Web."""

    action: Literal["open_whatsapp"]

    EXAMPLE: ClassVar[dict] = {
        "action": "open_whatsapp",
        "explanation": "Voy a abrir WhatsApp en la computadora.",
    }


class SearchContactStep(StepBase):
    """Search WhatsApp for the contact or chat called value and open it."""

    action: Literal["search_contact"]
    value: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "search_contact",
        "value": "Mamá",
        "explanation": "Voy a buscar la conversación con Mamá.",
    }


class SendWhatsappMessageStep(StepBase):
    """Write body in the open WhatsApp chat and send it (the user is always asked first)."""

    action: Literal["send_whatsapp_message"]
    body: str = Field(min_length=1)

    EXAMPLE: ClassVar[dict] = {
        "action": "send_whatsapp_message",
        "body": "Ya llegué, gracias.",
        "consequential": True,
        "explanation": "Voy a enviar su mensaje, si usted está de acuerdo.",
    }


class SelectOptionStep(Targeted):
    """Choose an option (value is its text) in a dropdown list, given by its index (or selector, or text)."""

    CHANGES_PAGE: ClassVar[bool] = False

    action: Literal["select_option"]
    value: str

    EXAMPLE: ClassVar[dict] = {
        "action": "select_option",
        "index": 7,
        "value": "Jalisco",
        "explanation": "Voy a elegir Jalisco en la lista de estados.",
    }


class ReadTextStep(Targeted):
    """Read the text of an element, or of the whole page when no element is given."""

    CHANGES_PAGE: ClassVar[bool] = False
    TARGET_REQUIRED: ClassVar[bool] = False

    action: Literal["read_text"]

    EXAMPLE: ClassVar[dict] = {
        "action": "read_text",
        "index": 12,
        "explanation": "Voy a leer el aviso para decirle qué dice.",
    }


class AskUserStep(StepBase):
    """Ask the user a question (kind text), or hand over something only they can do, like a login (kind handoff)."""

    action: Literal["ask_user"]
    value: str = Field(min_length=1)
    kind: Literal["text", "handoff"] = "text"

    EXAMPLE: ClassVar[dict] = {
        "action": "ask_user",
        "value": "¿Cuál es su CURP? La necesito para continuar.",
        "kind": "text",
        "explanation": "Me falta un dato que solo usted conoce.",
    }


class DoneStep(StepBase):
    """Finish the task; value is the short, plain-language answer for the user, success is false if it failed."""

    CHANGES_PAGE: ClassVar[bool] = False

    action: Literal["done"]
    value: str
    success: bool = True

    EXAMPLE: ClassVar[dict] = {
        "action": "done",
        "value": "Listo. Encontré varias recetas de pozole y abrí la primera.",
        "success": True,
        "explanation": "Terminé lo que me pidió.",
    }


Step = Annotated[
    Union[
        GotoStep, WaitStep, ClickStep, TypeTextStep, PressKeyStep, ScreenshotStep, ScrollStep,
        SmartSearchStep, SmartClickStep, AnalyzePageStep, NewTabStep, SwitchTabStep, CloseTabStep,
        SearchYoutubeStep, ClickFirstVideoStep, OpenGmailStep, ComposeEmailStep, SendEmailStep,
        OpenWhatsappStep, SearchContactStep, SendWhatsappMessageStep, SelectOptionStep, ReadTextStep,
        AskUserStep, DoneStep,
    ],
    Field(discriminator="action"),
]

ActStep = Annotated[
    Union[
        GotoStep, WaitStep, ClickStep, TypeTextStep, PressKeyStep, ScreenshotStep, ScrollStep,
        SmartSearchStep, SmartClickStep, AnalyzePageStep, NewTabStep, SwitchTabStep, CloseTabStep,
        SearchYoutubeStep, ClickFirstVideoStep, OpenGmailStep, ComposeEmailStep, SendEmailStep,
        OpenWhatsappStep, SearchContactStep, SendWhatsappMessageStep, SelectOptionStep, ReadTextStep,
    ],
    Field(discriminator="action"),
]

ACTION_MODELS: Dict[str, Type[StepBase]] = {
    "goto": GotoStep,
    "wait": WaitStep,
    "click": ClickStep,
    "type_text": TypeTextStep,
    "press_key": PressKeyStep,
    "screenshot": ScreenshotStep,
    "scroll": ScrollStep,
    "smart_search": SmartSearchStep,
    "smart_click": SmartClickStep,
    "analyze_page": AnalyzePageStep,
    "new_tab": NewTabStep,
    "switch_tab": SwitchTabStep,
    "close_tab": CloseTabStep,
    "search_youtube": SearchYoutubeStep,
    "click_first_video": ClickFirstVideoStep,
    "open_gmail": OpenGmailStep,
    "compose_email": ComposeEmailStep,
    "send_email": SendEmailStep,
    "open_whatsapp": OpenWhatsappStep,
    "search_contact": SearchContactStep,
    "send_whatsapp_message": SendWhatsappMessageStep,
    "select_option": SelectOptionStep,
    "read_text": ReadTextStep,
    "ask_user": AskUserStep,
    "done": DoneStep,
}

ALWAYS_CONFIRM = frozenset({"send_email", "send_whatsapp_message"})


class Plan(BaseModel):
    steps: List[Step] = Field(min_length=1)
    reasoning: Optional[str] = None


def changes_page(step: StepBase) -> bool:
    if isinstance(step, PressKeyStep):
        return step.value == "Enter"
    return type(step).CHANGES_PAGE


def plan_json_schema() -> dict:
    return Plan.model_json_schema()
