import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tekmar_482 import NativePath

from tekmar_482_dashboard.feed import Feed
from tekmar_482_dashboard.identity import identify_sources


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_observed_addressable_unknown_sources_on_selected_ports(
        self,
    ) -> None:
        feed = Feed()
        for key, port, node, model in [
            ("master", 1, 31, None),
            ("another port", 2, 13, None),
            ("known", 1, 6, "example"),
            ("unaddressable", 1, 0, None),
        ]:
            feed.peers[key] = {
                "port": port,
                "subnet": 0x42,
                "node": node,
                "model": model,
            }
        manager = AsyncMock()
        with patch(
            "tekmar_482_dashboard.identity.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await identify_sources(feed, manager, (1,))
        manager.request_identity.assert_awaited_once_with(NativePath(1, 0x42, 31))
        self.assertIsNone(feed.peers["master"]["model"])
