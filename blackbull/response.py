import asyncio
import json
from typing import Union
from http import HTTPStatus

from .logger import get_logger_set
logger, log = get_logger_set('response')


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


def make_websocket_body(content: Union[str, bytes]):
    ret = {
        'type': 'websocket.send',
    }
    if isinstance(content, str):
        ret['text'] = content
    elif isinstance(content, bytes):
        ret['bytes'] = content
    else:
        logger.error(f'Type error: expected str or bytes, but {type(content)} is provided.')

    return ret


async def Response(send, content: Union[str, bytes], status=HTTPStatus.OK, more_body=False):
    start = make_start(status=status)
    await send(start)

    if isinstance(content, str):
        body = make_body(content.encode())
    elif isinstance(content, bytes):
        body = make_body(content)
    else:
        raise TypeError(f'Parameter "content" ({type(content)}) is not str nor bytes.')

    await send(body)


async def JSONResponse(send, content, status=HTTPStatus.OK):
    start = make_start(status=status)
    await send(start)

    try:
        body = make_body(json.dumps(content).encode())
        logger.debug(body)

    except BaseException as e:
        logger.error(e)

    await send(body)


async def WebSocketResponse(send, content, status=HTTPStatus.OK):
    try:
        body = make_websocket_body(json.dumps(content))
        logger.debug(body)

    except BaseException as e:
        logger.error(e)

    await send(body)
