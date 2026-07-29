"""Minimal FastAPI app matching The Benchmarker's reference."""
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI()


@app.get("/")
async def homepage():
    return PlainTextResponse("")


@app.get("/user/{user_id}")
async def user(user_id: str):
    return PlainTextResponse(user_id)


@app.post("/user")
async def userinfo():
    return PlainTextResponse("")
