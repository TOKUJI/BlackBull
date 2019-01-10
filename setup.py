from setuptools import setup, find_packages

setup(name='BlackBull', version='0.0.1', packages=find_packages(),
    install_requires=[
        'hpack',
    ],
    extras_require={
        'develop': ['peewee',
                    'mako',
                    'pytest',
                    'daphne',
                    ],
    },
)
