# -*- coding:ascii -*-
from mako import runtime, filters, cache
UNDEFINED = runtime.UNDEFINED
STOP_RENDERING = runtime.STOP_RENDERING
__M_dict_builtin = dict
__M_locals_builtin = locals
_magic_number = 10
_modified_time = 1551003078.2265003
_enable_loop = True
_template_filename = 'templates/dummy.html'
_template_uri = 'templates/dummy.html'
_source_encoding = 'ascii'
_exports = []


def render_body(context,**pageargs):
    __M_caller = context.caller_stack._push_frame()
    try:
        __M_locals = __M_dict_builtin(pageargs=pageargs)
        data = context.get('data', UNDEFINED)
        __M_writer = context.writer()
        __M_writer('<html>\n\n<head>\n    <style type="text/css"></style>\n</head>\n\n<body>\n\t<div>\n\t\t')
        __M_writer(str(data['user'].name))
        __M_writer('\n\t</div>\n</body>\n\n</html>')
        return ''
    finally:
        context.caller_stack._pop_frame()


"""
__M_BEGIN_METADATA
{"filename": "templates/dummy.html", "uri": "templates/dummy.html", "source_encoding": "ascii", "line_map": {"16": 0, "22": 1, "23": 9, "24": 9, "30": 24}}
__M_END_METADATA
"""
