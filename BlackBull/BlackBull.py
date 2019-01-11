from functools import partial, reduce
from collections import OrderedDict

# import from this package
from .util import RouteRecord
from .logger import get_logger_set
logger, log = get_logger_set('BlackBull')

@log
async def scheme(scope, ctx, next_):
    if scope['type'] == 'http':
        ctx['response'] = OrderedDict()
        ctx['response']['start'] = {'type': 'http.response.start', 'status': 200, 'headers': [], }
        ctx['response']['body'] = {'type': 'http.response.body', 'body': None}
        ctx['response']['disconnect'] = {'type': 'http.disconnect', }

    ctx = await next_(scope, ctx)

    if scope['type'] == 'http':
        ctx['response']['body']['body'] = ctx['response']['message'].encode()

    return ctx


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
        def _fn(scope):
            endpoint, methods = self._router.find(scope['path'])
            logger.debug(endpoint)

            @log
            async def __fn(receive, send):
                event = await receive()
                nonlocal endpoint

                endpoint = reduce(lambda a, b: partial(b, next_=a),
                                  reversed(self.stack),
                                  endpoint)

                ctx = await endpoint(scope, {'event': event})

                for v in ctx['response'].values():
                    await send(v)
            return __fn

        f = _fn(scope)
        return f

    def route(self, method='GET', path='/'):
        """ set endpoint function here"""
        return self._router.route(method=method, path=path)