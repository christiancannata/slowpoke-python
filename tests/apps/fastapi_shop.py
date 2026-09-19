from fastapi import APIRouter, FastAPI, HTTPException
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from slowpoke.asgi import SlowpokeMiddleware

from . import sa_shop


def create_app(engine, async_engine):
    app = FastAPI()

    @app.middleware("http")
    async def user_middleware(request, call_next):  # a BaseHTTPMiddleware runs the app in another task
        return await call_next(request)

    app.add_middleware(SlowpokeMiddleware)

    @app.get("/orders")
    def orders():
        return sa_shop.customer_names(engine)

    @app.api_route("/orders/{order_id}", methods=["GET", "PUT"])
    def order(order_id: int):
        total = sa_shop.order_total(engine, order_id)
        if total is None:
            raise HTTPException(status_code=404)
        return {"total": total}

    @app.get("/async/orders")
    async def async_orders():
        return await sa_shop.async_customer_names(async_engine)

    @app.get("/lookup")
    def lookup(email: str):
        return sa_shop.find_by_email(engine, email)

    @app.get("/boom")
    def boom():
        sa_shop.order_total(engine, 1)
        raise RuntimeError("broken endpoint")

    router = APIRouter(prefix="/api")

    @router.get("/items/{item_id}")
    async def item(item_id: str):
        return {"id": item_id}

    app.include_router(router)

    sub = FastAPI()

    @sub.get("/reports/{year}")
    def report(year: int):
        return sa_shop.order_total(engine, 2)

    app.mount("/sub", sub)
    return app


def create_starlette_app(engine):
    def hello(request):
        return PlainTextResponse(str(sa_shop.order_total(engine, 1)))

    def nested(request):
        return PlainTextResponse("nested")

    app = Starlette(routes=[
        Route("/hello/{name}", hello),
        Mount("/admin", routes=[Route("/users/{user_id:int}", nested)]),
    ])
    app.add_middleware(SlowpokeMiddleware)
    return app
