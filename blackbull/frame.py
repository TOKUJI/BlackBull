import asyncio
import typing
from enum import Enum, auto
from io import BytesIO
from hpack import Encoder, Decoder

# private programs
# from . import message
from .logger import get_logger_set
from .utils import pop_safe
logger, log = get_logger_set('frame')


class FrameTypes(Enum):
    DATA = b'\x00'
    HEADERS = b'\x01'
    PRIORITY = b'\x02'
    RST_STREAM = b'\x03'
    SETTINGS = b'\x04'
    PUSH_PROMISE = b'\x05'
    PING = b'\x06'
    GOAWAY = b'\x07'
    WINDOW_UPDATE = b'\x08'
    COTINUATION = b'\x09'


class FrameFactory:
    """docstring for FrameFactory"""
    def __init__(self):
        super().__init__()
        self.decoder = Decoder()
        self._factory = {klass.FrameType(): klass for klass in FrameBase.__subclasses__()}

    def create(self, type_, flags, stream_id, *, data=b'', **kwds):
        logger.info(f'type:{type_}, flags:{flags}, id:{stream_id}')

        frame = self._factory[type_](
            len(data),
            type_.value,
            flags if type(flags) is int else flags.value,
            stream_id,
            data=data,
            decoder=self.decoder,
            )
        return frame

    def load(self, data):
        if len(data) < 9:
            logger.error(data)
            raise Exception('not enough data length: {}'.format(data))

        length = int.from_bytes(data[:3], 'big', signed=False)
        type_ = data[3:4]
        flags = int.from_bytes(data[4:5], 'big', signed=False)
        stream_id = int.from_bytes(data[5:9], 'big', signed=False)
        payload = data[9: 9 + length]

        return self.create(FrameTypes(type_), flags, stream_id, data=payload)

    def __setattr__(self, key, value):
        if key == 'header_table_size':
            try:
                self.decoder.header_table_size = value
            except Exception as e:
                logger.error(e)
        else:
            super().__setattr__(key, value)


class FrameBase:
    """docstring for FrameBase"""
    factory = None

    def __init__(self, length: int, type_, flags: int, stream_id: int):
        self.length = length
        self.type_ = type_
        self.flags = flags
        self.stream_id = stream_id
        logger.debug(f'type={self.type_}, '
                     f'flag={self.flags}, '
                     f'stream_id={self.stream_id} '
                     f'and payload size={self.length}')

    @log
    def save(self):
        res = b''
        res += self.length.to_bytes(3, 'big', signed=False)
        res += self.type_
        res += self.flags.to_bytes(1, 'big', signed=False)
        res += self.stream_id.to_bytes(4, 'big', signed=False)

        logger.debug(f'FrameBase is saving a frame {res}')
        return res

    def has_continuation(self):
        return False

    def __eq__(self, other):
        return self.type_ == other.type_ and\
               self.flags == other.flags and\
               self.stream_id == other.stream_id

    @staticmethod
    def FrameType():
        raise NotImplementedError('A subclass of FrameBase should implement FrameType() method')

    def __repr__(self):
        return f'{self.FrameType().name}: stream_id={self.stream_id}'


class SettingFlags(Enum):
    INIT = 0x0
    ACK = 0x1


class SettingFrame(FrameBase):
    """docstring for SettingFrame"""
    initial_window_size = None

    def __init__(self, length: int, type_, flags: int, stream_id: int, *, data=None, **kwds):
        super(SettingFrame, self).__init__(length, type_, flags, stream_id)
        logger.debug('SettingFrame is called.')

        self.params = {b'\x00\x01': self.set_header_table_size,
                       b'\x00\x02': self.set_enable_push,
                       b'\x00\x03': self.set_max_concurrent_streams,
                       b'\x00\x04': self.set_initial_window_size,
                       b'\x00\x05': self.set_max_frame_size,
                       b'\x00\x06': self.set_max_header_list_size,
                       }

        payload = BytesIO(data)
        while True:
            identifier = payload.read(2)
            if len(identifier) != 2:
                break

            value = payload.read(4)
            if len(value) != 4:
                break
            try:
                self.params[identifier](value)
            except KeyError:
                logger.error(f'unknown identifier: {identifier}, '
                             f'{int.from_bytes(value, "big", signed=False)}')

    def set_header_table_size(self, value):
        self.header_table_size = int.from_bytes(value, 'big', signed=False)
        logger.debug('header_table_size: {}'.format(self.header_table_size))

    def set_enable_push(self, value):
        self.enable_push = int.from_bytes(value, 'big', signed=False)
        logger.debug('enable_push: {}'.format(self.enable_push))

    def set_initial_window_size(self, value):
        self.initial_window_size = int.from_bytes(value, 'big', signed=False)
        logger.debug('initial_window_size: {}'.format(self.initial_window_size))

    def set_max_concurrent_streams(self, value):
        self.max_concurrent_streams = int.from_bytes(value, 'big', signed=False)
        logger.debug('max_concurrent_streams: {}'.format(self.max_concurrent_streams))

    def set_max_frame_size(self, value):
        self.max_frame_size = int.from_bytes(value, 'big', signed=False)
        logger.debug('max_frame_size: {}'.format(self.max_frame_size))

    def set_max_header_list_size(self, value):
        self.max_header_list_size = int.from_bytes(value, 'big', signed=False)
        logger.debug('max_frame_size: {}'.format(self.max_frame_size))

    def save(self):
        base = super().save()
        # TODO: enable to alter settings parameters
        return base

    @staticmethod
    def FrameType():
        return FrameTypes.SETTINGS


class WindowUpdate(FrameBase):
    """docstring for WindowUpdate"""
    def __init__(self, length: int, type_, flags: bytes, stream_id: int, *, data=None, **kwds):
        super(WindowUpdate, self).__init__(length, type_, flags, stream_id)
        logger.debug('WindowUpdate is called.')

        payload = BytesIO(data)
        self.set_window_size(data)

    def set_window_size(self, value):
        self.window_size = int.from_bytes(value, 'big', signed=False)
        logger.debug('window_size: {}'.format(self.window_size))

    def save(self):
        base = super(WindowUpdate, self).save()
        # TODO: enable to alter settings parameters
        return base

    @staticmethod
    def FrameType():
        return FrameTypes.WINDOW_UPDATE


class HeadersFlags(Enum):
    END_STREAM = 0x1
    END_HEADERS = 0x4
    PADDED = 0x8
    PRIORITY = 0x20


class Headers(FrameBase, dict):

    def __init__(self, length: int, type_, flags: bytes, stream_id: int, *, data=None, decoder=None):
        super(Headers, self).__init__(length, type_, flags, stream_id)
        logger.debug('Headers is called.')
        # Read flags
        self.end_stream = HeadersFlags.END_STREAM.value & self.flags
        self.end_headers = HeadersFlags.END_HEADERS.value & self.flags
        self.padded = HeadersFlags.PADDED.value & self.flags
        self.priority = HeadersFlags.PRIORITY.value & self.flags
        logger.debug(f'stream_id = {stream_id}, '
                     f'end_stream = {self.end_stream > 0}, '
                     f'end_header = {self.end_headers > 0}, '
                     f'padded = {self.padded > 0},'
                     f'priority = {self.priority > 0}, '
                     )
        # set decoder
        self.decoder = decoder

        if self.length <= 0:
            return
        # handle payload

        payload = BytesIO(data)

        if self.padded:
            payload.read(1)

        if self.priority:  # TODO: handle priority properly
            self.stream_dependency = int.from_bytes(payload.read(4), 'big', signed=False)
            self.priority_weight = int.from_bytes(payload.read(1), 'big', signed=False)
            logger.debug(f'stream_id = {stream_id}, '
                         f'stream_dependency: {self.stream_dependency}, '
                         f'priority_weight: {self.priority_weight}')

        fields = self.decoder.decode(payload.read())
        for k, v in fields:
            self[k] = v
            logger.debug('{}: {}'.format(k, v))

    def set_table_size(self, size):
        self.table_size = size

    @log
    def save(self):
        encoder = Encoder()
        payload = encoder.encode(self)
        # self.payload = payload
        self.length = len(payload)

        base = super().save()
        logger.debug(base + payload)
        return base + payload

    def has_continuation(self):
        if not self.end_stream:
            return True
        return False

    def __getattr__(self, key):
        if key == 'stream_dependency':
            self.stream_dependency = 0
            return self.stream_dependency

        elif key == 'priority_weight':
            self.priority_weight = 1
            return self.priority_weight

        else:
            raise AttributeError(key)

    @staticmethod
    def FrameType():
        return FrameTypes.HEADERS

    def __repr__(self):
        head = [super(Headers, self).__repr__(), ]
        head += [f'{k}: {v}' for k, v in self.items()]

        return ', '.join(head)


class GoAway(FrameBase):
    def __init__(self, length: int, type_, flags: int, stream_id: int, *, data=None, **kwds):
        super().__init__(length, type_, flags, stream_id)
        logger.debug('GoAway is called.')

        payload = BytesIO(data)
        self.stream_id = int.from_bytes(payload.read(4), 'big', signed=False)
        self.error_code = int.from_bytes(payload.read(4), 'big', signed=False)
        self.append_data = payload.read()
        logger.debug('stream_id: {}'.format(self.stream_id))
        logger.debug('error_code: {}'.format(self.error_code))
        logger.debug('append_data: {}'.format(self.append_data))

    @staticmethod
    def FrameType():
        return FrameTypes.GOAWAY


class RstStream(FrameBase):
    def __init__(self, length: int, type_, flags: int, stream_id: int, *, data=None, **kwds):
        super().__init__(length, type_, flags, stream_id)
        logger.debug('RstStream is called.')
        if len(data) != 4:
            raise Exception('Frame size error')

        self.error_code = ErrorCodes(int.from_bytes(data, 'big', signed=False))
        logger.debug(f'error_code: {self.error_code}')

    @staticmethod
    def FrameType():
        return FrameTypes.RST_STREAM


class ErrorCodes(Enum):
    NO_ERROR = 0x0
    PROTOCOL_ERROR = 0x1
    INTERNAL_ERROR = 0x2
    FLOW_CONTROL_ERROR = 0x3
    SETTINGS_TIMEOUT = 0x4
    STREAM_CLOSED = 0x5
    FRAME_SIZE_ERROR = 0x6
    REFUSED_STREAM = 0x7
    CANCEL = 0x8
    COMPRESSION_ERROR = 0x9
    CONNECT_ERROR = 0xa
    ENHANCE_YOUR_CALM = 0xb
    INADEQUATE_SECURITY = 0xc
    HTTP_1_1_REQUIRED = 0xd


class DataFlags(Enum):
    END_STREAM = 0x1
    PADDED = 0x8


class Data(FrameBase):
    """docstring for Data"""
    def __init__(self, length: int, type_, flags: int, stream_id: int, *, data=None, **kwds):
        super().__init__(length, type_, flags, stream_id)
        logger.debug('Data is called.')
        self.end_stream = DataFlags.END_STREAM.value & self.flags
        self.padded = DataFlags.PADDED.value & self.flags

        if isinstance(data, str):
            payload = BytesIO(data.encode())
        else:
            payload = BytesIO(data)

        if self.padded:
            pad_length = int.from_bytes(payload.read(1), 'big', signed=False)
            # TODO: add checking logic of pad_length.
            data_length = length - pad_length - 1
        else:
            data_length = length

        self.payload = payload.read(data_length)
        logger.debug(self.payload)

    def save(self):
        self.length = len(self.payload)
        base = super().save()
        logger.info('payload is {}'.format(self.payload))
        return base + self.payload

    @staticmethod
    def FrameType():
        return FrameTypes.DATA

    def __repr__(self):
        s = super().__repr__()
        return f'{s}: end_stream={True if self.end_stream else False}: {self.payload}'


class Priority(FrameBase):
    def __init__(self, length: int, type_, flags: int, stream_id: int, *, data=None, **kwds):
        super().__init__(length, type_, flags, stream_id)
        logger.debug('Priority is called.')

        payload = BytesIO(data)

        _t = int.from_bytes(payload.read(4), 'big', signed=False)
        logger.info(_t)

        self.exclusion = 0x80000000 & _t
        self.dependent_stream = _t & 0x7fffffff
        self.weight = int.from_bytes(payload.read(1), 'big', signed=False)
        logger.info(f'exclusion: {self.exclusion}, dependent_stream: {self.dependent_stream}, weight: {self.weight}')

    def save(self):
        base = super().save()

        _t = self.exclusion | self.dependent_stream
        payload = _t.to_bytes(4, 'big', signed=False) + self.weight.to_bytes(1, 'big', signed=False)
        self.length = 5

        return base + payload

    @staticmethod
    def FrameType():
        return FrameTypes.PRIORITY


class Ping(FrameBase):
    """docstring for Ping"""
    def __init__(self, length: int, type_, flags: int, stream_id: int, *, data, **kwds):
        super().__init__(length, type_, flags, stream_id)
        logger.debug('Ping is called.')
        logger.debug('FRAME_SIZE: {}'.format(length))
        if length != 8:
            raise Exception('FRAME_SIZE_ERROR: {}'.format(length))

        self.payload = data
        logger.debug('payload is {}'.format(self.payload))

    def save(self):
        base = super().save()
        res = base + self.payload
        logger.debug('Ping is saving: {}'.format(res))
        return res

    @staticmethod
    def FrameType():
        return FrameTypes.PING

    def __eq__(self, other):
        return super().__eq__(other) and (self.payload == other.payload)
