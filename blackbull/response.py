import asyncio
from typing import Union
from http import HTTPStatus

from .logger import get_logger_set
logger, log = get_logger_set()


def make_start(status, headers=[]):
    res = {
        'type': 'http.response.start',
        'status': status.value,
        'headers': headers
    }
    return res


def make_body(content: bytes):
    res = {
        'type': 'http.response.body',
        'body': content,
        'more_body': False
    }
    return res


async def Response(send, content: Union[str, bytes], status=HTTPStatus.OK,):
    start = make_start(status=status)
    await send(start)

    if isinstance(content, str):
        body = make_body(content.encode())
    else:
        body = make_body(content)

    await send(body)
