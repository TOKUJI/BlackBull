from mako.template import Template

def render_login_page(data=None):
    login_template = Template(filename='templates/login.html', )
    return login_template.render_unicode()

def render_dummy_page(data=None):
    template = Template(filename='templates/dummy.html', module_directory='templates/modules/')
    return template.render_unicode(data=data)

def render_table_page(columns, data):
    template = Template(filename='templates/table.html', module_directory='templates/modules/')
    return template.render_unicode(columns=columns, data=data)

def render_403_page():
    try:
        template = Template(filename='templates/403.html', module_directory='templates/modules/')
        return template.render_unicode()
    except Exception as e:
        logger.error(e)
        return 'Failed to render_403_page'
