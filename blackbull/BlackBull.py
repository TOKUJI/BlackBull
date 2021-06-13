# from functools import partial, reduce
from http import HTTPStatus
import asyncio
import sys
import traceback

# import from this package
from .utils import do_nothing, Scheme, HTTPMethods
from .router import Router
from .response import Response
from .logger import get_logger_set
from .watch import Watcher, force_reload
logger, log = get_logger_set(__name__)


class BlackBull:
    def __init__(self,
                 router=Router(),
                 loop=None,
                 logger=logger,
                 log=log,
                 ):
        self._router = router
        self._router.route_404()(self.not_found_fn)

        self._loop = loop
        self._certfile = None
        self._keyfile = None

    @property
    def loop(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    async def not_found_fn(self, scope, receive, send, inner=do_nothing):
        await Response(send, b'NOT FOUND', status=HTTPStatus.NOT_FOUND)

    @property
    def certfile(self):
        return self._certfile

    @property
    def keyfile(self):
        return self._keyfile

    async def __call__(self, scope, receive, send):
        logger.info((scope, receive, send))

        if scope['type'] == 'http':
            scheme = Scheme.http
        elif scope['type'] == 'websocket':
            scheme = Scheme.websocket
        else:
            logger.error(f'Invalid scheme ({scope["type"]}) is requested.')
            raise Exception('Invalid scheme is requested.')

        path = scope['path']
        logger.debug((path, scheme))
        function, methods = self._router[(path, scheme)]
        logger.debug((self, function))

        try:
            await function(scope, receive, send)
        except Exception:
            logger.error(traceback.format_exc())

    def route(self, methods=[HTTPMethods.get], path='/', scheme=Scheme.http, functions=[]):
        """
        Set endpoint functions here.
        methods: HTTP method of functions.
        functions: functions that will be registered in the router.
        """
        return self._router.route(
            methods=methods,
            path=path,
            scheme=scheme,
            functions=functions,
            )

    def route_404(self, fn):
        return self._router.route_404()(fn)

    def has_server(self):
        return hasattr(self, 'server')

    def create_server(self, certfile=None, keyfile=None, port=0, ):
        from .server import ASGIServer
        self.server = ASGIServer(
            self,
            certfile=certfile,
            keyfile=keyfile,
            loop=self.loop)
        self.server.open_socket(port)
        logger.info(self.server)

    async def run(self, certfile=None, keyfile=None, port=0, debug=False):
        logger.info('Run is called.')
        tasks = []

        if not self.has_server():
            self.create_server(
                certfile=certfile,
                keyfile=keyfile,
                port=port,
                )

        if debug:
            watcher = Watcher(loop=self.loop)
            watcher.add_watch(sys.argv[0], self.reload)
            watcher.add_watch('blackbull', self.reload)
            tasks.append(watcher.watch())

        tasks.append(self.server.run(port=port))

        try:
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.exceptions.CancelledError:
            logger.info('The tasks have been cancelled.')

        except BaseException:
            logger.error(traceback.format_exc())

    def reload(self):
        self.stop()
        force_reload(sys.argv[0])

    def stop(self):
        self.server.close()

    @property
    def port(self):
        return self.server.port
