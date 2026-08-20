"""Tests for the in-Sprite active-turn lease."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from omnigent.runner._entry import (
    _run_sprite_activity_lease,
    _sprite_activity_task_name,
)

pytestmark = pytest.mark.asyncio


async def test_sprite_activity_task_name_sanitizes_runner_token() -> None:
    """Delegated runner IDs use underscores, which Sprite task names reject."""
    assert _sprite_activity_task_name("runner_token_34D104") == "omnigent-runner-token-34d104"


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """Wait briefly for a background lease request to arrive."""
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for Sprite Tasks API request")


async def test_active_work_upserts_then_releases_task() -> None:
    """A turn holds the Sprite, and draining all work releases it promptly."""
    requests: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        return httpx.Response(204 if request.method == "DELETE" else 200)

    active = True
    async with httpx.AsyncClient(
        base_url="http://sprite",
        transport=httpx.MockTransport(handler),
    ) as client:
        task = asyncio.create_task(
            _run_sprite_activity_lease(
                has_active_work=lambda: active,
                task_name="omnigent-runner-123",
                client=client,
                poll_interval_s=0.005,
                refresh_interval_s=60,
            )
        )
        await _wait_until(lambda: any(method == "PUT" for method, _, _ in requests))
        active = False
        await _wait_until(lambda: any(method == "DELETE" for method, _, _ in requests))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert requests[:2] == [
        (
            "PUT",
            "/v1/tasks/omnigent-runner-123",
            {"expire": "5m"},
        ),
        ("DELETE", "/v1/tasks/omnigent-runner-123", None),
    ]


async def test_cancellation_releases_live_task() -> None:
    """Orderly runner shutdown removes an outstanding hold immediately."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(204 if request.method == "DELETE" else 200)

    async with httpx.AsyncClient(
        base_url="http://sprite",
        transport=httpx.MockTransport(handler),
    ) as client:
        task = asyncio.create_task(
            _run_sprite_activity_lease(
                has_active_work=lambda: True,
                task_name="omnigent-runner-123",
                client=client,
                poll_interval_s=0.005,
            )
        )
        await _wait_until(lambda: "PUT" in methods)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert methods == ["PUT", "DELETE"]


async def test_missing_task_on_release_is_success() -> None:
    """A task that expired independently is already safely released."""
    methods: list[str] = []
    active = True

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(404 if request.method == "DELETE" else 200)

    async with httpx.AsyncClient(
        base_url="http://sprite",
        transport=httpx.MockTransport(handler),
    ) as client:
        task = asyncio.create_task(
            _run_sprite_activity_lease(
                has_active_work=lambda: active,
                task_name="omnigent-runner-123",
                client=client,
                poll_interval_s=0.005,
            )
        )
        await _wait_until(lambda: "PUT" in methods)
        active = False
        await _wait_until(lambda: "DELETE" in methods)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert methods == ["PUT", "DELETE"]
