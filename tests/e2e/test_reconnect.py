import pytest

from babas.bridge.server import BridgeServer

pytestmark = pytest.mark.e2e


async def test_extension_reconnects_after_bridge_restart(ext_env):
    original = ext_env.bridge
    port = original.port
    assert original.connected

    await original.stop()

    replacement = BridgeServer(port=port)
    await replacement.start()
    try:
        assert replacement.port == port
        assert await replacement.wait_connected(10), "the extension did not reconnect to the restarted bridge within 10 s"
        assert replacement.extension_version
        info = await replacement.request("tab.info", timeout=10)
        assert isinstance(info["tab_id"], int)
    finally:
        await replacement.stop()
