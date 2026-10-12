import logging
import sys
from typing import Union

from babas.safety import redact

_HANDLER_MARK = "_babas_handler"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _level_number(level: Union[str, int]) -> int:
    if isinstance(level, int):
        return level
    number = logging.getLevelName(level.strip().upper())
    return number if isinstance(number, int) else logging.INFO


def setup_logging(level: Union[str, int] = "INFO") -> logging.Logger:
    logger = logging.getLogger("babas")
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
    # sys.stderr is read now, not at import: stdout is the protocol channel in MCP stdio mode.
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(_level_number(level))
    return logger
