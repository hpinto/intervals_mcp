# intervals_mcp_server.py
import os
import sys
import httpx
import uvicorn
import urllib.parse
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, List
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()
MCP_SECRET_TOKEN = os.environ.get("MCP_SECRET_TOKEN")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID")

if not all([MCP_SECRET_TOKEN, INTERVALS_API_KEY, INTERVALS_ATHLETE_ID]):
    print("Error fatal: Faltan variables MCP_SECRET_TOKEN, INTERVALS_API_KEY o INTERVALS_ATHLETE_ID en el archivo .env.", file=sys.stderr)
    sys.exit(1)

mcp = FastMCP(
    "IntervalsICU_Server",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

@mcp.tool()
async def get_athlete_info() -> Dict[str, Any]:
    """Obtiene biometría básica, y umbrales (FTP, Ritmo) del atleta."""
    url = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}"
    # Implementamos AsyncClient y un timeout explícito de 15 segundos
    async with httpx.AsyncClient(auth=("API_KEY", INTERVALS_API_KEY), timeout=15.0) as client:
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

        return {
            "id": data.get("id"),
            "resting_hr": data.get("icu_resting_hr"),
            "weight": data.get("icu_weight"),
            "bike_ftp": bike_ftp,
            "run_threshold_pace": run_pace
        }

@mcp.tool()
async def get_recent_activities(days: int = 7) -> List[Dict[str, Any]]:
    """Obtiene resumen fisiológico y de carga (TSS) de las actividades deportivas recientes."""
    now = datetime.now(UTC)
    oldest_dt = now - timedelta(days=days)
    
    # Aseguramos formato ISO estándar sin microsegundos para compatibilidad total con Intervals.icu
    oldest = oldest_dt.strftime("%Y-%m-%dT%H:%M:%S")
    newest = now.strftime("%Y-%m-%dT%H:%M:%S")
    
    url = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}/activities"
    params = {"oldest": oldest, "newest": newest}
    
    async with httpx.AsyncClient(auth=("API_KEY", INTERVALS_API_KEY), timeout=15.0) as client:
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
        return clean_activities

class ASGITokenAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Absorber peticiones de escaneo (OPTIONS/HEAD) de Claude para evitar el error 405
        if scope["method"] in ["OPTIONS", "HEAD"]:
            status = 204 if scope["method"] == "OPTIONS" else 200
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"access-control-allow-origin", b"*"),
                    (b"access-control-allow-methods", b"GET, POST, OPTIONS, HEAD"),
                    (b"access-control-allow-headers", b"*")
                ]
            })
            await send({
                "type": "http.response.body",
                "body": b"",
                "more_body": False
            })
            return

        if not scope["path"].endswith("/sse"):
            return await self.app(scope, receive, send)
            
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8")
        
        query_string = scope.get("query_string", b"").decode("utf-8")
        parsed_query = urllib.parse.parse_qs(query_string)
        query_token = parsed_query.get("token", [""])[0] if "token" in parsed_query else ""
        
        expected_bearer = f"Bearer {MCP_SECRET_TOKEN}"
        
        if auth_header != expected_bearer and query_token != MCP_SECRET_TOKEN:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"access-control-allow-origin", b"*")
                ]
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error": "Acceso denegado: Token invalido"}',
                "more_body": False
            })
            return

        return await self.app(scope, receive, send)

if __name__ == "__main__":
    app = mcp.sse_app()
    
    app.add_middleware(ASGITokenAuthMiddleware)
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=9898,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
