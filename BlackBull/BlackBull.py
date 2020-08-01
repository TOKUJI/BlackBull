from functools import partial, reduce
from collections import OrderedDict
from http import HTTPStatus
import sys
import traceback

# import from this package
from .util import RouteRecord
from .logger import get_logger_set
logger, log = get_logger_set('BlackBull')

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

        if type(response) == dict: # assume the response contains start, body, response.
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
                 router = RouteRecord(),
                 ):
        self._router = router
        self.stack = [scheme, ]

    async def use(self, fn):
        """ fn must require 3 arguments
        scope, ctx (context) and next_
        """
        self.stack.append(fn)

    def __call__(self, scope):
        logger.info(scope)
        endpoint, methods = self._router.find(scope['path'])
        logger.debug(endpoint)

        @log
        async def __fn(receive, send):
            event = await receive()
            nonlocal endpoint
            nonlocal scope

            """
            stack := (scheme(scope, ctx, next_), funcA(scope, ctx, next_))
            reduce(lambda a, b: partial(b, next_=a),
                                reversed(self.stack),
                                endpoint)
            <=> partial(scheme, next_=partial(funcA, next_=endpoint)))
            <=> scheme(scope, ctx, next_=funcA(scope, ctx, next_=endpoint))
            """
            middleware_stack = reduce(lambda a, b: partial(b, next_=a),
                              reversed(self.stack),
                              endpoint)

            ret = await middleware_stack(scope, event)
            logger.debug(f'ASGI app has made the result {ret}')
            for k, v in ret.items():
                logger.debug('{}: {}'.format(k, v))
                await send(v)

        return __fn


    def route(self, method='GET', path='/'):
        """ Set endpoint function here.
        The endpoint function should have 2 input variable

        """
        return self._router.route(method=method, path=path)
