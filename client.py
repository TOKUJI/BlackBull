import asyncio

#
from BlackBull.client import Client
from BlackBull.frame import FrameFactory, FrameTypes

async def main():
    c = Client(name='alice', port=8000)
    await c.ping()
    future = c.run()

    try:
        is_connected = await c.is_connected()
        print(is_connected)
        await future

    except Exception as e:
        print(e)

if __name__ == '__main__':
    import logging
    from BlackBull.logger import get_logger_set, ColoredFormatter
    logger, log = get_logger_set()

    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
    handler.setFormatter(cf)
    logger.addHandler(handler)

    fh = logging.FileHandler('test.log', 'w')
    fh.setLevel(logging.DEBUG)
    ff = logging.Formatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
    fh.setFormatter(ff)
    logger.addHandler(fh)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt is requested')
