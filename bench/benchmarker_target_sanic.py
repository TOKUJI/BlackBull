"""Minimal Sanic app matching The Benchmarker's reference.
https://github.com/the-benchmarker/web-frameworks/blob/develop/python/sanic/server.py
"""
from sanic import Sanic
from sanic.response import text

app = Sanic("benchmark")


@app.route("/")
async def index(request):
    return text("")


@app.route("/user/<id:int>", methods=["GET"])
async def user_info(request, id):
    return text(str(id))


@app.route("/user", methods=["POST"])
async def user(request):
    return text("")
