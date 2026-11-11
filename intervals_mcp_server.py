# intervals_mcp_server.py 
import os
import sys
import httpx
import uvicorn
import urllib.parse
import json
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()
MCP_SECRET_TOKEN = os.environ.get("MCP_SECRET_TOKEN")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID")

if not all([MCP_SECRET_TOKEN, INTERVALS_API_KEY, INTERVALS_ATHLETE_ID]):
    print("Error fatal: Faltan variables MCP_SECRET_TOKEN, INTERVALS_API_KEY o INTERVALS_ATHLETE_ID en el archivo .env.", file=sys.stderr)
    sys.exit(1)

mcp_server = Server("IntervalsICU_Server")

@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_athlete_info",
            description="Obtiene biometría básica, y umbrales (FTP, Ritmo) del atleta.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_recent_activities",
            description="Obtiene resumen fisiológico y de carga (TSS) de las actividades deportivas recientes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Número de días hacia atrás (por defecto 7)"}
                },
            },
        ),
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    async with httpx.AsyncClient(auth=("API_KEY", INTERVALS_API_KEY), timeout=15.0) as client:
        if name == "get_athlete_info":
            url = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}"
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            
            bike_ftp = None
            run_pace = None
            for sport in data.get("sportSettings", []):
                if "Ride" in sport.get("types", []):
                    bike_ftp = sport.get("ftp")
                if "Run" in sport.get("types", []):
                    run_pace = sport.get("threshold_pace")

            result = {
                "id": data.get("id"),
                "resting_hr": data.get("icu_resting_hr"),
                "weight": data.get("icu_weight"),
                "bike_ftp": bike_ftp,
                "run_threshold_pace": run_pace
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_recent_activities":
            days = arguments.get("days", 7)
            now = datetime.now(UTC)
            oldest_dt = now - timedelta(days=days)
            oldest = oldest_dt.strftime("%Y-%m-%dT%H:%M:%S")
            newest = now.strftime("%Y-%m-%dT%H:%M:%S")
            
            url = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}/activities"
            params = {"oldest": oldest, "newest": newest}
            response = await client.get(url, params=params)
            response.raise_for_status()
            activities = response.json()
            
            clean_activities = []
            for act in activities:
                clean_activities.append({
                    "start_date_local": act.get("start_date_local"),
                    "type": act.get("type"),
                    "distance": act.get("distance"),
                    "moving_time": act.get("moving_time"),
                    "average_heartrate": act.get("average_heartrate"),
                    "max_heartrate": act.get("max_heartrate"),
                    "average_watts": act.get("average_watts"),
                    "normalized_watts": act.get("normalized_watts"),
                    "tss": act.get("icu_training_load"),
                    "intensity": act.get("icu_intensity"),
                    "rpe": act.get("rpe"),
                    "feel": act.get("feel")
                })
            return [TextContent(type="text", text=json.dumps(clean_activities, indent=2))]

    return [TextContent(type="text", text='{"error": "Herramienta no encontrada"}')]

session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    json_response=True,
    stateless=True,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    async with session_manager.run():
        yield

app = FastAPI(title="Intervals MCP Streamable Gateway", lifespan=lifespan)

async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})

class IntervalsGateway:
    def __init__(self, fastapi_app):
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path in ["/mcp", "/sse"]:
                headers = dict(scope.get("headers", []))
                auth_header = headers.get(b"authorization", b"").decode("utf-8")
                
                query_string = scope.get("query_string", b"").decode("utf-8")
                parsed_query = urllib.parse.parse_qs(query_string)
                query_token = parsed_query.get("token", [""])[0] if "token" in parsed_query else ""
                
                expected_bearer = f"Bearer {MCP_SECRET_TOKEN}"
                
                if auth_header != expected_bearer and query_token != MCP_SECRET_TOKEN:
                    await _send_json(send, 401, {"error": "Acceso denegado: Token inválido"})
                    return
                
                await session_manager.handle_request(scope, receive, send)
                return

        await self.fastapi_app(scope, receive, send)

final_app = CORSMiddleware(
    app=IntervalsGateway(app),
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)

if __name__ == "__main__":
    uvicorn.run(
        final_app,
        host="0.0.0.0",
        port=9898,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
