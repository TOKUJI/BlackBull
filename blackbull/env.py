import os
from enum import StrEnum


class Environment(StrEnum):
    PRODUCTION  = 'production'
    DEVELOPMENT = 'development'
    TEST        = 'test'


def get_env() -> Environment:
    raw = os.environ.get('BLACKBULL_ENV', 'development').lower()
    try:
        return Environment(raw)
    except ValueError:
        return Environment.DEVELOPMENT
