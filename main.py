"""Technaptix Voice Agent — monolithic FastAPI backend."""

from dotenv import load_dotenv
from fastapi import FastAPI

from routes import register_routes

load_dotenv()

app = FastAPI(title="Technaptix Voice Agent API")
register_routes(app)
