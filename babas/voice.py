from typing import Any, Callable, Dict, Optional, Tuple

_loaded: Dict[str, Optional[Tuple[Any, ...]]] = {}


def _output() -> Optional[Tuple[Callable[[], Any], Callable[[], Any], Callable[[], Any]]]:
    if "output" not in _loaded:
        try:
            from babas_voice import init_butler, shutdown_butler, get_butler
            _loaded["output"] = (init_butler, shutdown_butler, get_butler)
        except ImportError:
            _loaded["output"] = None
    return _loaded["output"]


def _input() -> Optional[Tuple[Callable[[], Any], Any]]:
    if "input" not in _loaded:
        try:
            from babas_listen import get_voice_input, VoiceInputMode
            _loaded["input"] = (get_voice_input, VoiceInputMode)
        except ImportError:
            _loaded["input"] = None
    return _loaded["input"]


def _gui() -> Optional[Tuple[Callable[[], Any], Any]]:
    if "gui" not in _loaded:
        try:
            from babas_gui import get_voice_gui, VoiceInputGUI
            _loaded["gui"] = (get_voice_gui, VoiceInputGUI)
        except ImportError:
            _loaded["gui"] = None
    return _loaded["gui"]


def voice_output_available() -> bool:
    return _output() is not None


def voice_input_available() -> bool:
    return _input() is not None


def voice_gui_available() -> bool:
    return _gui() is not None


def init_butler() -> Any:
    loaded = _output()
    return loaded[0]() if loaded else None


def shutdown_butler() -> None:
    loaded = _output()
    if loaded:
        loaded[1]()


def get_butler() -> Any:
    loaded = _output()
    return loaded[2]() if loaded else None


def get_voice_input() -> Any:
    loaded = _input()
    return loaded[0]() if loaded else None


def get_voice_gui() -> Any:
    loaded = _gui()
    return loaded[0]() if loaded else None
