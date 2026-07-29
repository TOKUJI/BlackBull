"""Minimal BlackBull app matching The Benchmarker's Starlette reference:
https://github.com/the-benchmarker/web-frameworks/blob/develop/python/starlette/server.py

Routes:
  GET  /                → empty response
  GET  /user/{user_id}  → returns user_id as text
  POST /user            → empty response
"""
import os
import sys

# Allow running as `python bench/benchmarker_target.py` from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from http import HTTPMethod

from blackbull import BlackBull

app = BlackBull()


@app.route(path="/")
async def homepage():
    return b""


@app.route(path="/user/{user_id}")
async def user(user_id: str):
    return user_id


@app.route(path="/user", methods=[HTTPMethod.POST])
async def userinfo():
    return b""


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    app.run(port=args.port)
