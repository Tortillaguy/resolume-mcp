"""Fade out layer 1, switch to clip 3, fade back in — the code-mode advantage."""
import asyncio
from resolume_mcp.client import ResolumeAgentClient


async def main():
    client = ResolumeAgentClient()
    if not await client.connect():
        return
    await client.set_layer_opacity(1, 0.0)
    await client.connect_clip(1, 3)
    await client.set_layer_opacity(1, 1.0)
    print("done")
    await client.disconnect()


asyncio.run(main())
