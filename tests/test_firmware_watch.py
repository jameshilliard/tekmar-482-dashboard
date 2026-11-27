import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

from tekmar_482 import (
    FirmwareImageUnavailableError,
    GatewayEndpoint,
    StockFirmwareError,
)

from tekmar_482_dashboard.firmware_watch import managed_gateway

MODULE = "tekmar_482_dashboard.firmware_watch."
ENDPOINT = GatewayEndpoint.tcp("127.0.0.1", 3001)


@asynccontextmanager
async def running(*args, **kwargs):
    yield "application"


class FirmwareWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_every_connection_including_when_stock_monitoring_is_allowed(
        self,
    ):
        for result in (False, True, FirmwareImageUnavailableError()):
            notify = Mock()
            with (
                patch(
                    MODULE + "install_firmware", new=AsyncMock(side_effect=[result])
                ) as install,
                patch(MODULE + "identified", running),
            ):
                async with managed_gateway(ENDPOINT, True, notify) as app:
                    self.assertEqual(app, "application")
            install.assert_awaited_once_with(ENDPOINT)
            self.assertEqual(
                notify.call_args.args[0],
                "missing_image" if isinstance(result, Exception) else "current",
            )

    async def test_stock_is_actionable_but_timeout_does_not_mean_stock(self):
        for failure in (StockFirmwareError(), TimeoutError()):
            notify = Mock()
            with patch(MODULE + "install_firmware", new=AsyncMock(side_effect=failure)):
                with self.assertRaises(type(failure)):
                    async with managed_gateway(ENDPOINT, False, notify):
                        self.fail("unidentified or stock gateway was admitted")
                self.assertEqual(
                    notify.call_args.args[0],
                    "manual" if isinstance(failure, StockFirmwareError) else "checking",
                )
        notify = Mock()
        with (
            patch(
                MODULE + "install_firmware",
                new=AsyncMock(side_effect=StockFirmwareError()),
            ),
            patch(MODULE + "open_gateway", running),
        ):
            async with managed_gateway(ENDPOINT, True, notify) as app:
                self.assertEqual(app, "application")
            self.assertEqual(notify.call_args.args[0], "manual")

    async def test_closing_connection_waits_for_in_progress_programming(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def program(endpoint):
            started.set()
            await finish.wait()
            return True

        async def connect():
            async with managed_gateway(ENDPOINT, False, Mock()):
                self.fail("cancelled connection must not take the serial port")

        with patch(MODULE + "install_firmware", program):
            task = asyncio.create_task(connect())
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
