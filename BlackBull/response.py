import asyncio
from http import HTTPStatus

from BlackBull.logger import get_logger_set
logger, log = get_logger_set()


def make_start(status, headers=[]):
    res = {
        'type': 'http.response.start',
        'status': status.value,
        'headers': headers
    }
    return res


def make_body(content):
    res = {
        'type': 'http.response.body',
        'body': content,
        'more_body': False
    }
    return res


async def respond(send, content, status=HTTPStatus.OK,):
    start = make_start(status=status)
    await send(start)

    body = make_body(content)
    await send(body)
