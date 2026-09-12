"""
FastAPI app entrypoint.

Responsibility:
- Create the FastAPI app instance.
- Register routers (customer, calls, search) under /api.
- Wire up startup/shutdown hooks (e.g. DB connection check).
- CORS / middleware configuration.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, calls, customer, search, users

app = FastAPI(title="GhorerBazar Customer 360 Dashboard API")

# DEV ONLY: the frontend prototype is opened as a static file (file://)
# or from a dev server on a different port than uvicorn, so its Origin
# won't match the API's. allow_origins=["*"] unblocks that for local
# testing — restrict this to the real frontend origin before any shared/
# production deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(customer.router, prefix="/api")
app.include_router(calls.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
