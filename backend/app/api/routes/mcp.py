from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_mcp_gateway_factory
from app.database.models import User
from app.database.session import get_db
from app.mcp.errors import MCPToolError
from app.workflows.chat_service import GatewayFactory

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/tools")
def list_tools(user: User = Depends(get_current_user), db: Session = Depends(get_db),
               gateway_factory: GatewayFactory = Depends(get_mcp_gateway_factory)) -> dict:
    """Live MCP tool discovery (tools/list) against the Gmail and Calendar MCP servers. Calls no tool."""
    servers: dict[str, dict] = {}
    with gateway_factory(db, user) as gateway:
        for name in gateway.servers:
            try:
                servers[name] = {"tools": [t.to_dict() for t in gateway.list_tools(name)]}
            except MCPToolError as exc:
                servers[name] = {"error": exc.to_dict()}
        return {"transport": gateway.transport, "servers": servers}
