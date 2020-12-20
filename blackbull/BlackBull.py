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
logger, log = get_logger_set()


def response(headers, body, status=200):
    ret = {}
    ret['start'] = {'type': 'http.response.start', 'status': status, 'headers': scope['headers'], }
    ret['body'] = {'type': 'http.response.body', 'body': ""}
    ret['disconnect'] = {'type': 'http.disconnect', }

    return ret


def make_response_template(scope):
    ret = {}
    if scope['type'] == 'http':
        ret['start'] = {'type': 'http.response.start', 'status': 200, 'headers': scope['headers'], }
        ret['body'] = {'type': 'http.response.body', 'body': ""}
        ret['disconnect'] = {'type': 'http.disconnect', }

    return ret


@log
async def scheme(scope, ctx, inner):
    try:
        logger.info(scope)
        ret = make_response_template(scope)
    except Exception as e:
        logger.error(e)

    try:
        response = await inner(scope, ctx)
        logger.debug(response)

        if type(response) == dict:  # assume the response contains start, body, response.
            ret = response
        elif type(response) == bytes:
            ret['body']['body'] = response
        elif type(response) == str:
            ret['body']['body'] = response.encode()
        else:
            raise BaseException('Invalid type of response from Application')

    except Exception as e:
        logger.error(e)
        logger.error(traceback.extract_tb(sys.exc_info()[2]).format())
        ret['body']['body'] = str(e)
        ret['start']['status'] = HTTPStatus.INTERNAL_SERVER_ERROR.value

    return ret


class BlackBull:
    def __init__(self,
                 router=Router(),
                 loop=None,

                 ):
        self._router = router
        self._router.route_404()(self.not_found_fn)

        self.stack = [scheme, ]

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

    async def use(self, fn):
        """ fn must require 3 arguments
        scope, ctx (context) and inner
        """
        self.stack.append(fn)

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

        await function(scope, receive, send)
        # event = await receive()
        """
        -> stack := (func(scope, receive, send), innerfunc(scope, receive, send))
        stack := (scheme(scope, ctx, inner), funcA(scope, ctx, inner))
        reduce(lambda a, b: partial(b, inner=a),
                            reversed(self.stack),
                            endpoint)
        <=> partial(scheme, inner=partial(funcA, inner=endpoint)))
        <=> scheme(scope, ctx, inner=funcA(scope, ctx, inner=endpoint))
        """
        # middleware_stack = reduce(lambda a, b: partial(b, inner=a),
        #                           reversed(self.stack),
        #                           endpoint)

        # ret = await middleware_stack(scope, receive, send)
        # logger.debug(f'ASGI app has made the result {ret}')
        # for k, v in ret.items():
        #     logger.debug(f'{k}: {v}')
        #     await send(v)

    def route(self, methods=[HTTPMethods.get], path='/', scheme=Scheme.http, functions=[]):
        """
        Set endpoint function here.
        The endpoint function should have 2 input variable
        """
        wrapper = self._router.route(
            methods=methods,
            path=path,
            scheme=scheme,
            functions=functions,
            )

        if scheme == Scheme.websocket:
            logger.info(scheme)
            logger.info(functions)
            logger.info(wrapper)

        return wrapper

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
            from .watch import Watcher, force_reload
            watcher = Watcher(loop=self.loop)
            # watcher.add_watch(__file__, force_reload(__file__))
            watcher.add_watch('BlackBull', force_reload(__file__))
            tasks.append(watcher.watch())

        tasks.append(self.server.run(port=port))

        try:
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.exceptions.CancelledError:
            logger.info('The tasks have been cancelled.')

        except BaseException:
            logger.error(traceback.format_exc())

    def stop(self):
        self.server.close()

    @property
    def port(self):
        return self.server.port
