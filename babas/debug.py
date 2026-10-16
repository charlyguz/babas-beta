import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Optional

TEMP_DEBUG_DIR: Optional[str] = None


def get_temp_debug_dir() -> str:
    global TEMP_DEBUG_DIR
    if TEMP_DEBUG_DIR is None or not os.path.exists(TEMP_DEBUG_DIR):
        TEMP_DEBUG_DIR = tempfile.mkdtemp(prefix="babas_debug_")
    return TEMP_DEBUG_DIR


def cleanup_temp_dir() -> None:
    global TEMP_DEBUG_DIR
    if TEMP_DEBUG_DIR and os.path.exists(TEMP_DEBUG_DIR):
        try:
            shutil.rmtree(TEMP_DEBUG_DIR, ignore_errors=True)
            print(f"   Temp files cleaned")
        except Exception:
            pass
        TEMP_DEBUG_DIR = None


async def save_debug_screenshot(page: Any, name: str = "error") -> str:
    debug_dir = get_temp_debug_dir()
    filename = os.path.join(debug_dir, f"{name}_{datetime.now().strftime('%H%M%S')}.png")
    try:
        await page.screenshot(path=filename)
        return filename
    except Exception:
        return ""
