# -*- coding:ascii -*-
from mako import runtime, filters, cache
UNDEFINED = runtime.UNDEFINED
STOP_RENDERING = runtime.STOP_RENDERING
__M_dict_builtin = dict
__M_locals_builtin = locals
_magic_number = 10
_modified_time = 1550512325.1074927
_enable_loop = True
_template_filename = 'templates/403.html'
_template_uri = 'templates/403.html'
_source_encoding = 'ascii'
_exports = []


def render_body(context,**pageargs):
    __M_caller = context.caller_stack._push_frame()
    try:
        __M_locals = __M_dict_builtin(pageargs=pageargs)
        __M_writer = context.writer()
        __M_writer('<html>\n\n<head>\n    <style type="text/css"></style>\n</head>\n\n<body>\n\t<div>\n\t\tAuthorization failed.\n\t</div>\n</body>\n\n</html>')
        return ''
    finally:
        context.caller_stack._pop_frame()


"""
__M_BEGIN_METADATA
{"filename": "templates/403.html", "uri": "templates/403.html", "source_encoding": "ascii", "line_map": {"16": 0, "21": 1, "27": 21}}
__M_END_METADATA
"""
