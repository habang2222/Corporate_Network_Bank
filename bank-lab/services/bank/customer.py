"""Customer boundary: each request revalidates an opaque session."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import Login
from .common import AUTH_URL, CORE_URL, bearer, call, identity, response_data
from .core import Transfer

router = APIRouter()


def proxy(request: Request, method: str, path: str, body=None):
    identity(request, {"customer"})
    response = call(CORE_URL, method, path, token=bearer(request), body=body)
    return JSONResponse(response.json(), response.status_code)


@router.post("/login")
def login(body: Login):
    result = response_data(call(AUTH_URL, "POST", "/login", body=body.model_dump()))
    if result["user"]["role"] != "customer":
        call(AUTH_URL, "POST", "/logout", token=result["token"])
        raise HTTPException(403, "customer login required")
    return result


@router.post("/logout")
def logout(request: Request):
    identity(request, {"customer"})
    return response_data(call(AUTH_URL, "POST", "/logout", token=bearer(request)))


@router.get("/accounts")
def accounts(request: Request):
    return proxy(request, "GET", "/accounts")


@router.get("/accounts/{account_id}")
def account(account_id: str, request: Request):
    from urllib.parse import quote
    return proxy(request, "GET", "/accounts/" + quote(account_id, safe=""))


@router.get("/accounts/{account_id}/transactions")
def history(account_id: str, request: Request):
    from urllib.parse import quote
    return proxy(request, "GET", "/accounts/" + quote(account_id, safe="") + "/transactions")


@router.post("/transfers")
def transfer(body: Transfer, request: Request):
    return proxy(request, "POST", "/transfers", body.model_dump())
