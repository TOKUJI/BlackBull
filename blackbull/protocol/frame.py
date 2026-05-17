"""HTTP/2 frame factory — parsing and creation.

``FrameFactory`` produces frames of the requested type and decodes raw bytes
from the wire.  Frame-type definitions live in ``frame_types``.
"""
from hpack import Decoder, Encoder

import logging
from .frame_types import (
    FrameBase, FrameTypes, FrameFlags,
    HeaderFrameFlags, SettingFrameFlags, ErrorCodes, PushPromise,
)

logger = logging.getLogger(__name__)


class FrameFactory:
    """docstring for FrameFactory"""
    def __init__(self):
        super().__init__()
        self.decoder = Decoder()
        self.encoder = Encoder()

    def create(self, type_: FrameTypes, flags: FrameFlags | int, stream_id: int, *, data: bytes = b'', **kwds):
        logger.info(f'type:{type_}, flags:{flags}, id:{stream_id}')

        frame = FrameBase._registry[type_](
            len(data),
            type_,
            flags if isinstance(flags, int) else flags.value,
            stream_id,
            data=data,
            decoder=self.decoder,
            encoder=self.encoder,
            )
        return frame

    def window_update(self, stream_id: int, increment: int):
        """Create a WINDOW_UPDATE frame with the given increment."""
        return self.create(FrameTypes.WINDOW_UPDATE, SettingFrameFlags.INIT, stream_id,
                           data=increment.to_bytes(4, 'big', signed=False))

    def rst_stream(self, stream_id: int, error_code: ErrorCodes):
        """Create a RST_STREAM frame with the given error code."""
        return self.create(FrameTypes.RST_STREAM, SettingFrameFlags.INIT, stream_id,
                           data=int(error_code).to_bytes(4, 'big', signed=False))

    def goaway(self, last_stream_id: int = 0, error_code: ErrorCodes | int = 0):
        """Create a GOAWAY frame (always on stream 0)."""
        payload = (last_stream_id.to_bytes(4, 'big', signed=False)
                   + int(error_code).to_bytes(4, 'big', signed=False))
        return self.create(FrameTypes.GOAWAY,
                           SettingFrameFlags.INIT,
                           last_stream_id + 1,
                           data=payload)

    def settings(self, *, ack: bool = False, enable_connect_protocol: bool = False,
                 initial_window_size: int | None = None,
                 max_concurrent_streams: int | None = None):
        """Create a SETTINGS frame (INIT or ACK).

        Parameters
        ----------
        ack:
            True → ACK frame (no payload).
        enable_connect_protocol:
            Include SETTINGS_ENABLE_CONNECT_PROTOCOL=1 (RFC 8441 §3).
        initial_window_size:
            Include SETTINGS_INITIAL_WINDOW_SIZE (identifier 0x4, RFC 7540 §6.5.2).
        max_concurrent_streams:
            Include SETTINGS_MAX_CONCURRENT_STREAMS (identifier 0x3, RFC 7540 §6.5.2).
            Tells the peer the maximum number of concurrent streams we will accept.
        """
        flags = SettingFrameFlags.ACK if ack else SettingFrameFlags.INIT
        if ack:
            return self.create(FrameTypes.SETTINGS, flags, 0, data=b'')
        data = b''
        if max_concurrent_streams is not None:
            data += b'\x00\x03' + max_concurrent_streams.to_bytes(4, 'big')
        if enable_connect_protocol:
            data += b'\x00\x08\x00\x00\x00\x01'  # ENABLE_CONNECT_PROTOCOL = 1
        if initial_window_size is not None:
            data += b'\x00\x04' + initial_window_size.to_bytes(4, 'big')  # INITIAL_WINDOW_SIZE
        return self.create(FrameTypes.SETTINGS, flags, 0, data=data)

    def push_promise(self, parent_stream_id: int, promised_stream_id: int,
                     pseudo_headers: dict, headers: list) -> PushPromise:
        """Create a PUSH_PROMISE frame sent on parent_stream_id.

        promised_stream_id must be an even server-allocated ID (RFC 7540 §5.1.1).
        pseudo_headers: dict mapping PseudoHeaders → str value.
        headers: list of (name, value) regular-header tuples.
        """
        frame = self.create(FrameTypes.PUSH_PROMISE, HeaderFrameFlags.END_HEADERS,
                            parent_stream_id, data=b'')
        frame.promised_stream_id = promised_stream_id
        frame.pseudo_headers = pseudo_headers
        frame.headers = headers
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
