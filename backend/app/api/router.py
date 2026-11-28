from fastapi import APIRouter

from app.api.routes import auth, calendar, chat, conversations, executions, gmail, health, intent, mcp, workflows

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(chat.router)
api_router.include_router(mcp.router)
api_router.include_router(gmail.router)
api_router.include_router(calendar.router)
api_router.include_router(intent.router)
api_router.include_router(workflows.router)
api_router.include_router(executions.router)
