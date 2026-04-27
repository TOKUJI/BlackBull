import asyncio
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello():
    return "Hello, world!"

if __name__ == '__main__':
    asyncio.run(app.run(port=8000))