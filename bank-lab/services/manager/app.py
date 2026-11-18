import secrets
from pathlib import Path
import httpx
from fastapi import FastAPI, Header, HTTPException

app = FastAPI(docs_url=None, redoc_url=None)

@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'manager'}

@app.get('/status')
async def status(x_service_key: str = Header(default='')):
    key = Path('/secrets/service-key').read_text().strip()
    if not secrets.compare_digest(x_service_key.encode('utf-8'), key.encode('utf-8')):
        raise HTTPException(403, 'management credential required')
    result = {}
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        for name in ('core', 'events', 'receiver'):
            try:
                response = await client.get(f'http://{name}:8080/health')
                result[name] = {'status_code': response.status_code}
            except httpx.HTTPError as error:
                result[name] = {'unavailable': type(error).__name__}
    return result
