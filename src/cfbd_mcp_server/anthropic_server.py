from fastapi import FastAPI, Request, HTTPException, Header, Form, Query
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from starlette.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from cfbd_mcp_server.server import handle_call_tool, handle_list_tools
import uuid
import logging
import os
from dotenv import load_dotenv
from urllib.parse import urlencode
import json
import hashlib
import base64
from starlette.responses import Response
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import Scope, Receive, Send
from collections.abc import AsyncIterator
import contextlib
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent
from .event_store import InMemoryEventStore
import time

# Load environment variables
load_dotenv()

# Debug levels: 0=OFF, 1=BASIC, 2=VERBOSE (fallback to DEBUG for backward compat)
DEBUG_LEVEL = int(os.getenv("DEBUG_LEVEL", os.getenv("DEBUG", "0")))
DEBUG_MODE = DEBUG_LEVEL > 0
logging.basicConfig(level=logging.DEBUG if DEBUG_MODE else logging.INFO)
logger = logging.getLogger("anthropic-server")

def _format_sse_chunk(chunk: bytes) -> str:
    """Decode an SSE bytes chunk and pretty-print any JSON payloads after 'data:' lines."""
    try:
        text = chunk.decode("utf-8", errors="replace").strip()
    except Exception:
        return repr(chunk)
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("data: "):
            payload = line[6:]
            try:
                obj = json.loads(payload)
                # Unescape \n, \t, etc, and pretty-print nested JSON-in-strings
                obj = _unescape_strings(obj)
                payload_pp = json.dumps(obj, indent=2, ensure_ascii=False)
                out.append("data:\n" + _trim(payload_pp))
            except Exception:
                out.append(line)
        else:
            out.append(line)
    return "\n".join(out)

def _unescape_strings(val: Any):
    """
    Recursively walk JSON-like structures.
    - For strings: unescape common sequences and pretty-print if the string itself is JSON.
    - For dicts/lists: process children.
    """
    if isinstance(val, str):
        s = val
        # Try to parse nested JSON stored as a string
        try:
            nested = json.loads(s)
            return _unescape_strings(nested)
        except Exception:
            pass
        # Replace common escapes for readability
        s = s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        return s
    if isinstance(val, list):
        return [_unescape_strings(x) for x in val]
    if isinstance(val, dict):
        return {k: _unescape_strings(v) for k, v in val.items()}
    return val

class _SSEChunkFilter(logging.Filter):
    """Intercept 'chunk: %s' logs from sse_starlette.sse and pretty-print the bytes."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name == "sse_starlette.sse" and isinstance(record.msg, str) and record.msg.startswith("chunk:"):
                if record.args and isinstance(record.args[0], (bytes, bytearray)):
                    chunk = record.args[0]
                    record.msg = "chunk:\n%s"
                    record.args = (_format_sse_chunk(chunk),)
        except Exception:
            # Never block logging due to formatting issues
            pass
        return True

# Attach the filter to the sse_starlette logger so its 'chunk' lines are prettified.
sse_logger = logging.getLogger("sse_starlette.sse")
sse_logger.addFilter(_SSEChunkFilter())

def _dbg(level: int, msg: str, *args):
    if DEBUG_LEVEL >= level:
        logger.debug(msg, *args)

def _trim(s: str, limit: int = 1000000) -> str:
    return s if len(s) <= limit else s[:limit] + f"... <trimmed {len(s)-limit} chars>"

def _is_event_stream_request(request: Request) -> bool:
    # Our /mcp route uses streamable HTTP/SSE; treat it as streaming
    accept = request.headers.get("accept", "").lower()
    return request.url.path.startswith("/mcp") or "text/event-stream" in accept

def _is_streaming_response(resp: Response) -> bool:
    mt = (resp.media_type or "").lower()
    return isinstance(resp, StreamingResponse) or "text/event-stream" in mt

# Required token for API access
ANTHROPIC_BEARER_TOKEN = os.getenv("ANTHROPIC_BEARER_TOKEN")
if not ANTHROPIC_BEARER_TOKEN:
    raise RuntimeError("ANTHROPIC_BEARER_TOKEN environment variable must be set")

# In-memory structures and file persistence for tokens
AUTH_CODES = {}
SESSION_TOKENS = {}
ISSUED_TOKENS_FILE = os.getenv("ISSUED_TOKENS_FILE", "./issued_tokens.json")

def load_issued_tokens():
    """Load issued tokens from disk."""
    if os.path.exists(ISSUED_TOKENS_FILE):
        try:
            with open(ISSUED_TOKENS_FILE, "r") as f:
                return set(json.load(f))
        except Exception as e:
            logger.error(f"Failed to load issued tokens: {e}")
    return set()

def save_issued_tokens(tokens):
    """Save issued tokens to disk with error handling."""
    try:
        with open(ISSUED_TOKENS_FILE, "w") as f:
            json.dump(list(tokens), f)
    except Exception as e:
        logger.error(f"Failed to save issued tokens: {e}")

ISSUED_TOKENS = load_issued_tokens()

def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE challenge."""
    hashed = hashlib.sha256(code_verifier.encode()).digest()
    computed_challenge = base64.urlsafe_b64encode(hashed).decode().rstrip("=")
    return computed_challenge == code_challenge

# MCP server setup
event_store = InMemoryEventStore()
server = Server("cfbd-anthropic-server")
session_manager = StreamableHTTPSessionManager(app=server, event_store=event_store, json_response=False)

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage lifecycle of the session manager."""
    async with session_manager.run():
        logger.info("Streamable session manager started")
        yield
        logger.info("Streamable session manager shutting down")

# Main FastAPI app
app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    """
    Level 1: METHOD URL, status, latency; for POST (non-streaming) also request/response bodies
    Level 2: add full request/response headers
    """
    if not DEBUG_MODE:
        return await call_next(request)

    method = request.method
    url = str(request.url)
    is_stream = _is_event_stream_request(request)

    # Request line
    _dbg(1, "→ %s %s", method, url)
    if DEBUG_LEVEL >= 2:
        _dbg(2, "Request headers: %s", dict(request.headers))

    # For POST bodies, only log if not streaming (do NOT touch SSE receive)
    post_body: Optional[bytes] = None
    if method == "POST" and not is_stream and DEBUG_LEVEL >= 1:
        try:
            post_body = await request.body()
            if post_body:
                _dbg(1, "POST body: %s", _trim(post_body.decode(errors="replace")))
            # Rehydrate body for downstream by providing it once, then delegating
            orig_receive = request._receive  # type: ignore[attr-defined]
            sent_once = False
            async def _receive():
                nonlocal sent_once
                if not sent_once:
                    sent_once = True
                    return {"type": "http.request", "body": post_body, "more_body": False}
                return await orig_receive()
            request._receive = _receive  # type: ignore[attr-defined]
        except Exception as e:
            _dbg(1, "POST body read error: %s", e)

    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000

    # Response line
    _dbg(1, "← %s %s %d in %.1fms", method, url, response.status_code, elapsed_ms)
    if DEBUG_LEVEL >= 2:
        _dbg(2, "Response headers: %s", dict(response.headers))

    # For POST replies, only capture if not streaming
    if method == "POST" and not is_stream and DEBUG_LEVEL >= 1 and not _is_streaming_response(response):
        try:
            body_bytes = b""
            async for chunk in response.body_iterator:
                body_bytes += chunk
            _dbg(1, "POST reply: %s", _trim(body_bytes.decode(errors="replace")))
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
                background=response.background,
            )
        except Exception as e:
            _dbg(1, "POST reply capture error: %s", e)
            return response

    return response

@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """Serve dynamic OAuth metadata."""
    base_url = str(request.base_url).rstrip("/")
    metadata = {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "jwks_uri": f"{base_url}/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"]
    }
    return JSONResponse(content=metadata)

@app.get("/authorize")
async def oauth_authorize(response_type: str = Query(...), client_id: str = Query(...), redirect_uri: str = Query(...), scope: str = Query(...), state: str = Query(...), code_challenge: str = Query(...), code_challenge_method: str = Query(...)):
    """Handle OAuth authorization request."""
    code = uuid.uuid4().hex
    AUTH_CODES[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge
    }
    params = urlencode({"code": code, "state": state})
    return RedirectResponse(url=f"{redirect_uri}?{params}", status_code=302)

@app.post("/token")
async def oauth_token(grant_type: str = Form(...), code: str = Form(...), redirect_uri: str = Form(...), client_id: str = Form(...), code_verifier: str = Form(...)):
    """Exchange authorization code for access token."""
    entry = AUTH_CODES.get(code)
    if not entry:
        raise HTTPException(status_code=400, detail="Invalid code")
    if entry["client_id"] != client_id or entry["redirect_uri"] != redirect_uri:
        raise HTTPException(status_code=400, detail="Client ID or redirect URI mismatch")
    if not verify_pkce(code_verifier, entry["code_challenge"]):
        raise HTTPException(status_code=400, detail="PKCE verification failed")
    token = uuid.uuid4().hex
    ISSUED_TOKENS.add(token)
    save_issued_tokens(ISSUED_TOKENS)
    SESSION_TOKENS[token] = {"session": uuid.uuid4().hex}
    return JSONResponse(content={"access_token": token, "token_type": "Bearer"})

async def handle_streamable_http_auth(scope: Scope, receive: Receive, send: Send):
    """Handle /mcp requests with bearer token verification."""
    headers = dict(scope.get("headers", []))
    auth_header = headers.get(b"authorization", b"").decode()
    if not auth_header.startswith("Bearer "):
        logger.warning("Unauthorized access attempt: Missing or invalid header")
        response = Response("Unauthorized: Missing or invalid header", status_code=401)
        await response(scope, receive, send)
        return
    token = auth_header.split(" ")[1]
    if token not in ISSUED_TOKENS:
        logger.warning(f"Unauthorized access attempt with token: {token[:8]}...")
        response = Response("Unauthorized: Token not recognized", status_code=401)
        await response(scope, receive, send)
        return

    # Keep auth log; avoid printing full token unless verbose
    if DEBUG_LEVEL >= 2:
        logger.debug(f"Authorized request with token: {token}")
    else:
        logger.info(f"Authorized request with token: {token[:8]}...")
    await session_manager.handle_request(scope, receive, send)

def root_handler(request: Request):
    """Return 200 OK for root."""
    return PlainTextResponse("OK", status_code=200)

def robots_handler(request: Request):
    """Serve robots.txt disallowing all crawlers."""
    return PlainTextResponse("User-agent: *\nDisallow: /", media_type="text/plain")

asgi_app = Starlette(
    debug=DEBUG_MODE,
    routes=[
        Route("/", endpoint=root_handler),
        Route("/robots.txt", endpoint=robots_handler),
        Mount("/mcp", app=handle_streamable_http_auth)
    ],
    lifespan=lifespan
)

app.mount("/", asgi_app)

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    """Handle tool invocation."""
    return await handle_call_tool(name, arguments)

@server.list_tools()
async def list_tools() -> List[Tool]:
    """List available tools."""
    return await handle_list_tools()

combined_app = app

