"""
================================================================================
[API Master Router] app/api/router.py
================================================================================
"""

from fastapi import APIRouter
from app.api.endpoints import simulation, principles

api_router = APIRouter()
api_router.include_router(simulation.router)
api_router.include_router(principles.router)
