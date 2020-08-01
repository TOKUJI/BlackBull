from peewee import *
import json
import uuid
from datetime import datetime, timedelta

# private programs
from BlackBull.logger import get_logger_set
logger, log = get_logger_set('data')


db = SqliteDatabase('data.db', pragmas={'foreign_keys': 1})

async def init_db():
    tables = [User,Account, SessionManager]
    db.connect()
    db.drop_tables(tables)
    db.create_tables(tables)
    await User.register('alice', 'password', 1000000)


class BaseModel(Model):
    class Meta:
        database = db


class User(BaseModel):
    id = UUIDField(unique=True)
    name = CharField()
    password = TextField()

    @staticmethod
    async def register(name, password, amount):
        User.create(id=uuid.uuid4(), name=name, password=password)
        await Account.register(user=User.get(name=name), amount=amount)


    @staticmethod
    async def authenticate(name, password):
        user = User.get(name=name)
        return user.password == password


class UserEncoder(json.JSONEncoder):
    """docstring for UserEncoder"""
    def default(self, obj):
        if isinstance(obj, User):
            return "'id':'{}', 'name':'{}', 'password':'{}'".format(obj.id, obj.name, obj.password)
        return json.JSONEncoder().default(self, obj)


class Account(BaseModel):
    id = UUIDField(unique=True)
    user = UUIDField()
    # ForeignKeyField(User, )
    amount = DecimalField()

    @staticmethod
    async def register(user, amount):
        logger.info(user)
        Account.create(id=uuid.uuid4(), user=user.id, amount=amount)

class SessionManager(BaseModel):
    id = UUIDField(unique=True) # Sesssion ID
    user = UUIDField() # User ID
    # user = ForeignKeyField(User, )
    last_access = DateTimeField()

    @log
    def isExpired(self):
        logger.debug('isExpired is called')
        try:
            res = self.last_access + timedelta(days=1) < datetime.now()
        except Exception as e:
            logger.warning(e)
            res = False
        return res


    @staticmethod
    def register(user):
        id_ = uuid.uuid4()
        logger.info('{} {} {} '.format(id_, user, user.id))
        SessionManager.create(id=id_, user=user.id, last_access=datetime.now())
        return id_

    @staticmethod
    async def verify(id):
        try:
            logger.debug(id)
            session = SessionManager.get(id=id)
            logger.debug(session)
            return not session.isExpired()
        except:
            return False

    def __str__(self):
        return '{} {} {}'.format(self.id, self.user, self.last_access)
