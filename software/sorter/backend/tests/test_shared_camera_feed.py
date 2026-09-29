"""Everyone watching one camera view streams the same encoded frames."""

from __future__ import annotations

import asyncio

from server.routers import cameras


def test_viewers_of_one_view_share_each_encoded_frame(monkeypatch) -> None:
    monkeypatch.setattr(cameras, "PREVIEW_MAX_FPS", 20.0)
    feed = cameras._SharedFeed("c_channel_2", True, False)
    renders: list[bytes] = []

    def render() -> bytes:
        renders.append(f"frame {len(renders)}".encode())
        return renders[-1]

    feed._render = render

    async def run() -> list[bytes]:
        first, second = feed.stream(), feed.stream()
        frames = [await first.__anext__(), await second.__anext__()]
        frames += [await first.__anext__(), await second.__anext__()]
        await first.aclose()
        await second.aclose()
        return frames

    frames = asyncio.run(run())
    assert frames[0] is frames[1] and frames[2] is frames[3]
    assert frames[0] != frames[2]
    assert len(renders) <= 3  # one encode per frame, not one per viewer
    assert feed.viewers == 0
