import asyncio
import httpx
from blackbull.logger import get_logger_set

logger, log = get_logger_set()


async def main():
    async with httpx.AsyncClient(http2=True, verify=False) as c:
        res = await c.get('https://localhost:8000/json', headers={'key': 'value'})

        assert res.status_code == 200
        assert res.content == b'{"a": "b"}'


if __name__ == '__main__':
    asyncio.run(
        asyncio.wait_for(
            main(), timeout=0.5
        )
    )
