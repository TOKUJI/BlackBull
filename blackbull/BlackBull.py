from functools import partial, reduce
from http import HTTPStatus
import asyncio
import sys
import traceback

# import from this package
from .utils import Router, do_nothing
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
async def scheme(scope, ctx, next_):
    try:
        logger.info(scope)
        ret = make_response_template(scope)
    except Exception as e:
        logger.error(e)

    try:
        response = await next_(scope, ctx)
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

    @property
    def loop(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    async def not_found_fn(self, scope, receive, send, next_=do_nothing):
        await Response(send, b'NOT FOUND', status=HTTPStatus.NOT_FOUND)

    async def use(self, fn):
        """ fn must require 3 arguments
        scope, ctx (context) and next_
        """
        self.stack.append(fn)

    async def __call__(self, scope, receive, send):
        logger.info((scope, receive, send))
        logger.debug(scope['path'])
        function, methods = self._router[scope['path']]
        logger.debug((self, function))

        await function(scope, receive, send)
        # event = await receive()
        """
        -> stack := (func(scope, receive, send), next_func(scope, receive, send))
        stack := (scheme(scope, ctx, next_), funcA(scope, ctx, next_))
        reduce(lambda a, b: partial(b, next_=a),
                            reversed(self.stack),
                            endpoint)
        <=> partial(scheme, next_=partial(funcA, next_=endpoint)))
        <=> scheme(scope, ctx, next_=funcA(scope, ctx, next_=endpoint))
        """
        # middleware_stack = reduce(lambda a, b: partial(b, next_=a),
        #                           reversed(self.stack),
        #                           endpoint)

        # ret = await middleware_stack(scope, receive, send)
        # logger.debug(f'ASGI app has made the result {ret}')
        # for k, v in ret.items():
        #     logger.debug(f'{k}: {v}')
        #     await send(v)

    def route(self, methods=['GET'], path='/', functions=[]):
        """
        Set endpoint function here.
        The endpoint function should have 2 input variable
        """
        return self._router.route(methods=methods, path=path, functions=functions)

    def route_404(self, fn):
        return self._router.route_404()(fn)

    def create_server(self, port=0):
        from .server import ASGIServer
        self.server = ASGIServer(self, certfile='server.crt', keyfile='server.key', loop=self.loop)
        self.server.open_socket(port)
        logger.info(self.server)

    async def run(self, port=0, debug=False):
        logger.info('Run is called.')
        tasks = []

        if not hasattr(self, 'server'):
            self.create_server(port)

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
