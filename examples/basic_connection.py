"""Standalone ResolumeAgentClient usage — no MCP required."""
import asyncio
from resolume_mcp.client import ResolumeAgentClient


async def main():
    client = ResolumeAgentClient()
    if await client.connect():
        print("BPM:", client.get_bpm())
        await client.disconnect()


asyncio.run(main())
