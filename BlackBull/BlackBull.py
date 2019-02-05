from functools import partial, reduce
from collections import OrderedDict

# import from this package
from .util import RouteRecord
from .logger import get_logger_set
logger, log = get_logger_set('BlackBull')

@log
async def scheme(scope, ctx, next_):
    try:
        response = await next_(scope, ctx)
    except Exception as e:
        logger.error(e)

    ret = OrderedDict()
    if type(response) == dict: # assume the response contains start, body, response.
        return response
    else:
        if type(response) == bytes:
            body = response
        elif type(response) == str:
            body = response.encode()

        if scope['type'] == 'http':
            ret['start'] = {'type': 'http.response.start', 'status': 200, 'headers': [], }
            ret['body'] = {'type': 'http.response.body', 'body': body}
            ret['disconnect'] = {'type': 'http.disconnect', }

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
            for k, v in ret.items():
                logger.info('{}: {}'.format(k, v))
                await send(v)

        return __fn


    def route(self, method='GET', path='/'):
        """ set endpoint function here"""
        return self._router.route(method=method, path=path)
