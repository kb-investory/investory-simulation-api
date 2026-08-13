"""
================================================================================
[API v1 Master Router] app/api/v1/router.py
================================================================================
"""

from fastapi import APIRouter
from app.api.v1.endpoints import simulation, principles

api_v1_router = APIRouter()
api_v1_router.include_router(simulation.router)
api_v1_router.include_router(principles.router)

