import asyncio
import json

from BlackBull.client import Client, Listener, RespondFactory
from BlackBull.logger import get_logger_set, ColoredFormatter
logger, log = get_logger_set()
from BlackBull.frame import FrameTypes, HeadersFlags, Stream, DataFlags

logger, log = get_logger_set('plugin')

class LedgerInfo:
    def __init__(self, prefix=None, currencyCode=None, currencyScale=None, connectors=[], minBalance=0, maxBalance=0):
        self.prefix = prefix
        self.currencyCode = currencyCode
        self.currencyScale = currencyScale
        self.connectors = connectors
        self.minBalance = minBalance
        self.maxBalance = maxBalance

    def __eq__(self, rhs):
        return self.prefix == rhs.prefix and \
               self.currencyCode == rhs.currencyCode and \
               self.currencyScale == rhs.currencyScale and \
               self.connectors == rhs.connectors and \
               self.minBalance == rhs.minBalance and \
               self.maxBalance == rhs.maxBalance

    def __str__(self):
        return 'prefix: {prefix}, '.format(prefix=self.prefix) + \
               'currencyCode: {currencyCode}, '.format(currencyCode=self.currencyCode) + \
               'currencyScale: {currencyScale}, '.format(currencyScale=self.currencyScale) + \
               'connectors: {connectors}, '.format(connectors=self.connectors) + \
               'minBalance: {minBalance}, '.format(minBalance=self.minBalance) + \
               'maxBalance: {maxBalance}, '.format(maxBalance=self.maxBalance)


    class Encoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, LedgerInfo):
                dct = {'prefix': obj.prefix,
                        'currencyCode': obj.currencyCode,
                        'currencyScale': obj.currencyScale,
                        'connectors': obj.connectors,
                        'minBalance': obj.minBalance,
                        'maxBalance': obj.maxBalance,
                        }
                return dct
            else:
                return super().default(obj)

    class Decoder(json.JSONDecoder):
        def decode(self, s):
            dct = super().decode(s) 
            l = LedgerInfo(**dct)
            return l


class Plugin(Client):
    """ Interledger plugin class for this sample ledger applicaiton. """

    def __init__(self, *args, **kwds):
        super().__init__(*args, **kwds)
        # self.add_handler('incoming_response', self.a)
        self.is_login = False

    @log
    async def a(self, frame):
        logger.debug(f'{frame.FrameType()}, {frame.flags& DataFlags.END_STREAM.value}')

        if frame.FrameType() == FrameTypes.HEADERS and frame.flags & HeadersFlags.END_STREAM.value or \
           frame.FrameType() == FrameTypes.DATA and frame.flags & DataFlags.END_STREAM.value:
            logger.info('Got END_STREAM')
            await RespondFactory.create(frame).respond(self)
            self.events[frame.stream_identifier].set()

        else:
            await RespondFactory.create(frame).respond(self)


    async def login(self, name, password):
        # Get an available stream and set an event to the stream.
        s = self.get_stream()
        event = self.register_event(s.identifier)
        logger.debug(f'An event for stream No. {s.identifier} is prepared')

        async with s.lock:
            # Send a request to get information of the ledger.
            header = self.factory.create(FrameTypes.HEADERS,
                                         HeadersFlags.END_HEADERS.value,
                                         s.identifier,)
            header[':method'] = 'POST'
            header[':path'] = '/login'
            header['content-type'] = 'text/html'
            body = self.factory.create(FrameTypes.DATA,
                                       DataFlags.END_STREAM.value,
                                       s.identifier,
                                       data=bytearray(f'user_name={name}&user_password={password}', 'utf-8'),)
            await self.send_frame(header)
            await self.send_frame(body)
            logger.debug('Plugin has sent login request')

            await event.wait()
            logger.debug(s)
            logger.debug(s.event)
            payload = json.loads(s.event['body'], cls=LedgerInfo.Decoder)
            logger.debug(payload)

            self.is_login = True

    @log
    async def get_info(self):

        if not self.is_connected():
            raise Exception('Not connected')

        # Get available stream
        s = await self.get_stream()

        def eos(frame): # eos: end of stream
            logger.debug(f'{frame}, {frame.FrameType()}, {frame.flags}')
            if frame.FrameType() == FrameTypes.DATA and frame.flags & DataFlags.END_STREAM.value > 0 or\
                frame.FrameType() == FrameTypes.HEADERS and frame.flags & HeadersFlags.END_STREAM.value > 0:
                return True
            return False

        event = self.register_event(s.identifier, condition=eos)
        logger.debug(f'An event for stream No. {s.identifier} is prepared')

        # Send a request to get information of the ledger.
        header = self.factory.create(FrameTypes.HEADERS,
                                     HeadersFlags.END_HEADERS.value | HeadersFlags.END_STREAM.value,
                                     s.identifier)
        header[':method'] = 'GET'
        header[':scheme'] = 'http'
        header[':path'] = '/info'
        await self.send_frame(header)

        # need to get the result, unmarshal it and return ledger info.
        await event.wait()
        logger.debug(s.event)
        payload = json.loads(s.event['body'], cls=LedgerInfo.Decoder)
        logger.debug(payload)

        s.release()
        return payload


    async def get_account(self):
        if not await self.is_connected():
            raise Exception('Not connected')

        s = self.get_stream()

        logger.debug('An event for stream No. {} is prepared'.format(s.identifier))
        # TODO: use register_event()
        event = asyncio.Event()
        self.events[s.identifier] = event

        header = self.factory.create(FrameTypes.HEADERS,
                                     HeadersFlags.END_HEADERS.value | HeadersFlags.END_STREAM.value,
                                     s.identifier)
        header[':method'] = 'GET'
        header[':scheme'] = 'http'
        header[':path'] = '/'
        await self.send_frame(header)

        # need to get the result, unmarshal it and return ledger info.
        await event.wait()
        logger.debug(s)
        logger.debug(s.event)
        payload = json.loads(s.event['body']['body'], cls=LedgerInfo.Decoder)
        logger.debug(payload)

        return payload
        # raise NotImplementedError("This method is not completely implemented")


if __name__ == '__main__':
    import logging

    def logger_config(logger):
        logger.setLevel(logging.DEBUG)

        handler = logging.StreamHandler()
        handler.setLevel(logging.WARNING)
        cf = ColoredFormatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
        handler.setFormatter(cf)
        logger.addHandler(handler)

        fh = logging.FileHandler('plugin.log','w')
        fh.setLevel(logging.DEBUG)
        ff = logging.Formatter('%(levelname)-17s:%(name)s:%(lineno)d %(message)s')
        fh.setFormatter(ff)
        logger.addHandler(fh)

    logger_config(logging.getLogger())

    async def main():
        c = Plugin(name='test', port=8000)
        await c.connect()
        f = c.listen()
        info = await c.get_info()
        assert type(info) == LedgerInfo
        assert info.prefix == 'local.Abank'

        c.disconnect()
    asyncio.run(main())
