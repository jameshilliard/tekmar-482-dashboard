"""Provision and verify before the dashboard takes ownership of a gateway."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from tekmar_482 import (
    BridgeClient,
    FirmwareImageUnavailableError,
    GatewayEndpoint,
    StockFirmwareError,
    Tekmar482Client,
    TekmarError,
    install_firmware,
    open_gateway,
)


@asynccontextmanager
async def identified(endpoint: GatewayEndpoint) -> AsyncIterator[BridgeClient]:
    async with endpoint.connect() as wire:
        bridge = BridgeClient(wire)
        await bridge.identify()
        yield bridge


async def stock_at_factory_speed(endpoint: GatewayEndpoint) -> GatewayEndpoint | None:
    if endpoint.port is not None or endpoint.baudrate == 9600:
        return None  # A transparent TCP bridge controls its own physical baud rate.
    factory = GatewayEndpoint.serial(endpoint.address, baudrate=9600)
    try:
        async with identified(factory):
            pass
    except StockFirmwareError:
        return factory
    except (OSError, TimeoutError, TekmarError):
        pass
    return None


@asynccontextmanager
async def managed_gateway(
    endpoint: GatewayEndpoint, allow_stock: bool, notify: Callable[[str, str], None]
) -> AsyncIterator[BridgeClient | Tekmar482Client]:
    notify("checking", "Checking gateway firmware")
    image = True
    stock_endpoint = endpoint
    async with AsyncExitStack() as stack:
        try:
            task = asyncio.create_task(install_firmware(endpoint))
            try:
                updated = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not hand the serial line to another operation halfway through flash.
                await asyncio.gather(task, return_exceptions=True)
                raise
            except FirmwareImageUnavailableError:
                image = False
                updated = False
                notify("missing_image", "No bundled firmware image installed")
            app: BridgeClient | Tekmar482Client = await stack.enter_async_context(
                identified(endpoint)
            )
        except StockFirmwareError:
            stock = True
        except TimeoutError:
            factory = await stock_at_factory_speed(endpoint)
            if factory is None:
                raise
            stock_endpoint, stock = factory, True
        else:
            stock = False
            notify(
                "current" if image else "missing_image",
                "Gateway firmware updated and verified"
                if updated
                else (
                    "Gateway firmware matches the bundled image"
                    if image
                    else "No bundled firmware image installed"
                ),
            )
        if stock:
            notify(
                "manual" if image else "stock_missing_image", "Stock firmware detected"
            )
            if not allow_stock:
                raise StockFirmwareError("Waiting for the 482 to enter update mode")
            app = await stack.enter_async_context(
                open_gateway(stock_endpoint, vendor=True)
            )
        yield app
