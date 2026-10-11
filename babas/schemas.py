from typing import List, Literal, Optional

from pydantic import BaseModel

ActionType = Literal[
    "goto", "wait", "click", "type_text", "press_key", "screenshot", "scroll",
    "smart_search", "smart_click", "new_tab", "switch_tab", "close_tab",
    "search_youtube", "click_first_video", "open_gmail", "compose_email", "send_email",
    "open_whatsapp", "search_contact", "send_whatsapp_message", "analyze_page"
]


class Step(BaseModel):
    action: ActionType
    value: Optional[str] = None
    selector: Optional[str] = None
    ms: Optional[int] = None
    to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    tab_name: Optional[str] = None


class Plan(BaseModel):
    steps: List[Step]
    reasoning: Optional[str] = None
