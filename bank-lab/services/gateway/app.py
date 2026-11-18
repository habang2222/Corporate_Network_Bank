"""Fixed upstream reverse proxy; never accepts a caller-selected URL."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
UPSTREAM = os.environ.get('CUSTOMER_URL', 'http://customer:8080')


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'web'}


@app.get('/')
def home():
    return FileResponse(Path(__file__).with_name('index.html'))


@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def proxy(path: str, request: Request):
    if path.split('/')[0] not in {'login', 'logout', 'accounts', 'transfers'}:
        return JSONResponse({'detail': 'route not exposed'}, status_code=404)
    content = await request.body()
    if len(content) > 64 * 1024:
        return JSONResponse({'detail': 'request too large'}, status_code=413)
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in {'authorization', 'content-type', 'x-operation-id', 'x-run-id'}}
    try:
        response = await request.app.state.client.request(
            request.method, UPSTREAM + '/' + path,
            params=request.query_params, headers=headers, content=content)
    except httpx.HTTPError:
        return JSONResponse({'detail': 'customer service unavailable'}, status_code=503)
    return Response(response.content, status_code=response.status_code,
                    media_type=response.headers.get('content-type', 'application/json'))
