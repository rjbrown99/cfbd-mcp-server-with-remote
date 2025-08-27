import asyncio
import os
import sys
import time
import logging
from importlib.metadata import metadata
from dotenv import load_dotenv
from typing import Any, TypedDict, Type, cast, Union
import httpx
import hashlib
from urllib.parse import urlparse
from datetime import datetime, timezone
import redis.asyncio as redis
import json

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

from .schema_helpers import create_tool_schema

from .cfbd_schema import (
    # Request parameter types
    getGames, getTeamRecords, getGamesTeams, getPlays, getDrives,
    getPlaysStats, getRankings, getMetricsPregameWp, getAdvancedBoxScore, getRoster,
    getCoaches, getLines,
    
    # Response types
    GamesResponse, TeamRecordResponse, GamesTeamsResponse, PlaysResponse,
    DrivesResponse, PlaysStatsResponse, RankingsResponse,
    MetricsPregameWpResponse, AdvancedBoxScoreResponse, RosterResponse,
    CoachesResponse, BettingGame,
    
    # Constants
    VALID_SEASONS, VALID_WEEKS, VALID_SEASON_TYPES, VALID_DIVISIONS
)

# Load environment variables
load_dotenv()

# Initialize server and API configuration
server = Server("cfbd")
API_KEY = os.getenv("CFB_API_KEY")
API_BASE_URL = 'https://apinext.collegefootballdata.com/'
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/1")

# --- Debug level (0=off, 1=basic, 2=verbose) ---
DEBUG_LEVEL = int(os.getenv("DEBUG_LEVEL", "1"))
logger = logging.getLogger("anthropic-server")

# Gate httpx/httpcore verbosity by our DEBUG_LEVEL
if DEBUG_LEVEL >= 2:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
else:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(level=logging.DEBUG if DEBUG_LEVEL > 0 else logging.INFO)

def _dbg(level: int, msg: str, *args):
    if DEBUG_LEVEL >= level:
        logger.debug(msg, *args)

def _trim(s: str, limit: int = 4000) -> str:
    return s if len(s) <= limit else s[:limit] + f"... <trimmed {len(s)-limit} chars>"

if not API_KEY:
    raise ValueError("CFB_API_KEY must be set in .env file")

# -----------------------------
# Redis cache (by full CFBD URL)
# -----------------------------
_redis: redis.Redis | None = None

async def _get_redis() -> redis.Redis | None:
    """Create/return a shared async Redis client. Returns None if unavailable."""
    global _redis
    if _redis is not None:
        return _redis
    try:
        _redis = await redis.from_url(REDIS_URL, decode_responses=True)  # store UTF-8 strings
        await _redis.ping()
        _dbg(1, "Redis connected: %s", REDIS_URL)
        return _redis
    except Exception as e:
        _dbg(1, "Redis unavailable (%s) — continuing without cache", e)
        return None

# TTLs by endpoint path (no trailing slash)
TTL_BY_ENDPOINT = {
    "/lines":   5 * 60,                 # 5 minutes
    "/coaches": 60 * 60 * 24 * 30,      # 30 days
    # add more here as needed:
    # "/games":   60 * 60,              # 1 hour
    # "/records": 60 * 60 * 24,         # 1 day
}

def _endpoint_path_from_url(full_url: str) -> str:
    """Normalize to a path without trailing slash, e.g. '/lines'."""
    p = urlparse(full_url).path
    return p[:-1] if p.endswith("/") and p != "/" else p

def _ttl_for_endpoint_path(path: str) -> int:
    path = _endpoint_path_from_url(path)
    return TTL_BY_ENDPOINT.get(path, 60 * 60)  # default 1 hour if not listed

def _url_cache_key(full_url: str) -> str:
    """Hash the full URL to keep keys short and safe."""
    h = hashlib.sha256(full_url.encode("utf-8")).hexdigest()
    return f"cfbd:url:{h}"

# Set up API client session
async def get_api_client() -> httpx.AsyncClient:
    """Create an API client with authentication headers."""
    # Pretty-print JSON bodies if possible
    def _pp_json(s: str) -> str:
        try:
            import json
            return json.dumps(json.loads(s), indent=2, ensure_ascii=False)
        except Exception:
            return s

    async def _log_req(request: httpx.Request) -> None:
        if DEBUG_LEVEL <= 0:
            return
        _dbg(1, "→ CFBD %s %s", request.method, str(request.url))
        # Level 1: show request body for non-GET (no headers)
        if request.method in {"POST", "PUT", "PATCH"} and request.content and DEBUG_LEVEL >= 1:
            try:
                _dbg(1, "CFBD req body: %s", _pp_json(request.content.decode("utf-8", "replace")))
            except Exception:
                _dbg(1, "CFBD req body (raw): %r", request.content)
        # Level 2: also show headers
        if DEBUG_LEVEL >= 2:
            _dbg(2, "CFBD req headers: %s", dict(request.headers))

    async def _log_resp(response: httpx.Response) -> None:
        if DEBUG_LEVEL <= 0:
            return
        req = response.request
        # Ensure the response body is fully read before accessing .text/.json in the hook
        await response.aread()
        _dbg(1, "← CFBD %s %s %d", req.method, str(req.url), response.status_code)
        # Level 1: always show response body (no headers)
        _dbg(1, "CFBD reply: %s", _pp_json(response.text))
        # Level 2: also show headers
        if DEBUG_LEVEL >= 2:
            _dbg(2, "CFBD resp headers: %s", dict(response.headers))

    return httpx.AsyncClient(
        base_url=API_BASE_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json"
        },
        timeout=30.0,
        event_hooks={
            "request": [_log_req],
            "response": [_log_resp],
        },
    )

@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    """List available endpoint schemas as resources."""
    return [
        types.Resource(
            uri="schema://games",
            name="Games endpoint schema",
            description="Get game information with scores, teams and metadata",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://records",
            name="Team records endpoint schema",
            description="Get team season records",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://plays",
            name="Plays endpoint",
            description="Schema for the /plays endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://drives",
            name="Drives endpoint",
            description="Schema for the /drives endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://plays/stats",
            name="Play/stats endpoint",
            description="Schema for the /plays/stats endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://rankings",
            name="Rankings endpoint",
            description="Schema for the /rankings endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://roster",
            name="Roster endpoint",
            description="Schema for the /roster endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://coaches",
            name="Coaches endpoint",
            description="Schema for the /coaches endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://lines",
            name="Lines endpoint",
            description="Schema for the /lines endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://metrics/wp/pregame",
            name="Metrics/wp/pregame endpoint",
            description="Schema for the pregame win probability endpoint",
            mimeType="text/plain"
        ),
        types.Resource(
            uri="schema://game/box/advanced",
            name="Advanced box score endpoint",
            description="Schema for the advanced box score endpoint",
            mimeType="text/plain"
        )
    ]

@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Return the schema for the requested endpoint."""
    # Map URIs to schema classes
    schema_map = {
        "schema://games": {
            "endpoint": "/games",
            "parameters": getGames.__annotations__,
            "response": GamesResponse.__annotations__,
            "description": "Get game information for specified parameters"
        },
        "schema://records": {
            "endpoint": "/records",
            "parameters": getTeamRecords.__annotations__,
            "response": TeamRecordResponse.__annotations__,
            "description": "Get team records for specified parameters"
        },
        "schema://plays": {
            "endpoint": "/plays",
            "parameters": getPlays.__annotations__,
            "response": PlaysResponse.__annotations__,
            "description": "Get play records for specified parameters"
        },
        "schema://drives": {
            "endpoint": "/drives",
            "parameters": getDrives.__annotations__,
            "response": DrivesResponse.__annotations__,
            "description": "Get drive records for specified parameters"
        },
        "schema://plays/stats": {
            "endpoint": "/plays/stats",
            "parameters": getPlaysStats.__annotations__,
            "response": PlaysStatsResponse.__annotations__,
            "description": "Get play by play records for specified parameters"
        },
        "schema://rankings": {
            "endpoint": "/rankings",
            "parameters": getRankings.__annotations__,
            "response": RankingsResponse.__annotations__,
            "description": "Get rankings records for specified parameters"
        },
        "schema://roster": {
            "endpoint": "/roster",
            "parameters": getRoster.__annotations__,
            "response": RosterResponse.__annotations__,
            "description": "Get team roster information for specified parameters"
        },
        "schema://coaches": {
            "endpoint": "/coaches",
            "parameters": getCoaches.__annotations__,
            "response": CoachesResponse.__annotations__,
            "description": "Get coaches information for specified parameters"
        },
        "schema://lines": {
            "endpoint": "/lines",
            "parameters": getLines.__annotations__,
            "response": BettingGame.__annotations__,
            "description": "Get betting data lines for specified parameters"
        },
        "schema://metrics/wp/pregame": {
            "endpoint": "/metrics/wp/pregame",
            "parameters": getMetricsPregameWp.__annotations__,
            "response": MetricsPregameWpResponse.__annotations__,
            "description": "Get pregame win probability records for specified parameters"
        },
        "schema://game/box/advanced": {
            "endpoint": "/game/box/advanced",
            "parameters": getAdvancedBoxScore.__annotations__,
            "response": AdvancedBoxScoreResponse.__annotations__,
            "description": "Get advanced box score data"
        }
    }

    if uri not in schema_map:
        raise ValueError(f"Unknown schema URI: {uri}")
    
    schema_info = schema_map[uri]
    # Format the schema information into a readable string
    schema_text = f"""
Endpoint: {schema_info['endpoint']}
Description: {schema_info['description']}

Input Parameters:
{_format_annotations(schema_info['parameters'])}

Response Schema:
{_format_annotations(schema_info['response'])}

Valid Values:
- Seasons: {min(VALID_SEASONS)} to {max(VALID_SEASONS)}
- WEEKS: {min(VALID_WEEKS)} to {max(VALID_WEEKS)}
- Season Types: {', '.join(VALID_SEASON_TYPES)}
- Divisions: {', '.join(VALID_DIVISIONS)}
"""
    return schema_text

def _format_annotations(annotations: dict) -> str:
    """Helper function to format type annotations into readable text."""
    formatted = []
    for name, type_hint in annotations.items():
        if str(type_hint).startswith("typing."):
            # Clean up typing notation
            type_str = str(type_hint).replace("typing.", "")
        else:
            type_str = str(type_hint)
        formatted.append(f"- {name}: {type_str}")
    return "\n".join(formatted)

def validate_params(params: dict, schema_class: Type[TypedDict]) -> dict:
    """Validate parameters against a TypedDict schema."""
    try:
        # Get the annotations from the schema class
        expected_types = schema_class.__annotations__
        validated_params = {}

        # Validate each parameter
        for key, value in params.items():
            if key not in expected_types:
                raise ValueError(f"Unexpected parameter: {key}")

            expected_type = expected_types[key]

            # Special handling for classification parameter
            if key == "classification" and value is not None:
                value = value.lower()
                if value not in VALID_DIVISIONS:
                    raise ValueError(f"Invalid Classification: Must be one of: {', '.join(VALID_DIVISIONS)}")

            # Handle Optional types
            if hasattr(expected_type, "__origin__") and expected_type.__origin__ is Union:
                if type(None) in expected_type.__args__:
                    # Parameter is optional
                    if value is not None:
                        # Validate against the non-None type
                        non_none_type = next(t for t in expected_type.__args__ if t != type(None))
                        # Handle primitive types
                        if non_none_type in (str, int, float, bool):
                            if not isinstance(value, non_none_type):
                                raise ValueError(f"Parameter {key} must be of type {non_none_type.__name__}")
                        validated_params[key] = value
                    else:
                        validated_params[key] = None
            else:
                # Parameter is required
                if not isinstance(value, expected_type):
                    raise ValueError(f"Parameter {key} must be of type {expected_type.__name__}")
                validated_params[key] = value

        # Check for required parameters
        for param, param_type in expected_types.items():
            is_optional = (hasattr(param_type, "__origin__") and 
                         param_type.__origin__ is Union and 
                         type(None) in param_type.__args__)
            if not is_optional and param not in params:
                raise ValueError(f"Missing required parameter: {param}")

        return validated_params
    
    except Exception as e:
        raise ValueError(f"Parameter validation failed: {str(e)}")

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    """List available prompt templates."""
    return [
        types.Prompt(
            name="analyze-game",
            description="Get detailed analysis of a specific game",
            arguments=[
                types.PromptArgument(
                    name="game_id",
                    description="Game ID to analyze",
                    required=True
                ),
                types.PromptArgument(
                    name="include_advanced_stats",
                    description="Whether to include advanced statistics (true/false)",
                    required=False
                )
            ]
        ),
        types.Prompt(
            name="analyze-team",
            description="Analyze a team's performance for a given season",
            arguments=[
                types.PromptArgument(
                    name="team",
                    description="Team name (e.g. Alabama)",
                    required=True
                ),
                types.PromptArgument(
                    name="year",
                    description="Season year",
                    required=True
                )
            ]
        ),
        types.Prompt(
            name="analyze-trends",
            description="Analyze trends over a season",
            arguments=[
                types.PromptArgument(
                    name="year",
                    description="Season year",
                    required=True
                ),
                types.PromptArgument(
                    name="metric",
                    description="Metric to analyze (scoring, attendance, upsets)",
                    required=True
                )
            ]
        ),
        types.Prompt(
            name="compare-teams",
            description="Compare the performance of two teams",
            arguments=[
                types.PromptArgument(
                    name="team1",
                    description="First team name",
                    required=True
                ),
                types.PromptArgument(
                    name="team2",
                    description="Second team name",
                    required=True
                ),
                types.PromptArgument(
                    name="year",
                    description="Season year",
                    required=True
                )
            ]
        ),
        types.Prompt(
            name="analyze-rivalry",
            description="Analyze historical rivalry matchups",
            arguments=[
                types.PromptArgument(
                    name="team1",
                    description="First team name",
                    required=True
                ),
                types.PromptArgument(
                    name="team2",
                    description="Second team name",
                    required=True
                ),
                types.PromptArgument(
                    name="start_year",
                    description="Starting year for analysis",
                    required=False
                )
            ]
        )
    ]

@server.get_prompt()
async def handle_get_prompt(
    name: str,
    arguments: dict[str, str] | None
) -> types.GetPromptResult:
    """Get a specific prompt template."""
    if not arguments:
        raise ValueError("Arguments are required")
    
    if name == "analyze-team":
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"I'll help analyze {arguments['team']}'s performance for the {arguments['year']} season by checking the College Football Data API. "
                            f"I'll review their record, key games, rankings and overall statistics."
                    )
                )
            ]
        )
    elif name == "compare-teams":
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Let me check the College Football Data API to compare {arguments['team1']} and {arguments['team2']} "
                            f"in the {arguments['year']} season. I'll look at their head-to-head matchup if they played, "
                            f"their records, common opponents, and statistical performance."
                    )
                )
            ]
        )
    else:
        raise ValueError(f"Unknown prompt: {name}")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools for querying the API."""
    base_description = """Note: When using this tool, please explicitly mention that you are retrieving data from the College Football Data API. You must mention "College Football Data API" in every response.

    """

    return [
        types.Tool(
            name="get-games",
            description=base_description + """Get college football game data.
            Required: year
            Optional: week, season_type, team, conference, category, game_id
            Example valid queries:
            - year=2023
            - year=2023, team="Alabama"
            - year=2023, week=1, conference="SEC"
            """,
            inputSchema=create_tool_schema(getGames)
        ),
        types.Tool(
            name="get-records",
            description=base_description + """Get college football team record data.
            Optional: year, team, conference
            Example valid queries:
            - year=2023
            - team="Alabama"
            - conference="SEC"
            - year=2023, team="Alabama"
            """,
            inputSchema=create_tool_schema(getTeamRecords)
        ),
        types.Tool(
            name="get-games-teams",
            description=base_description + """Get college football team game data.
            Required: year plus at least one of: week, team or conference.
            Example valid queries:
            - year=2023, team="Alabama"
            - year=2023, week=1
            - year=2023, conference="SEC"
            """,
            inputSchema=create_tool_schema(getGamesTeams)
        ),
        types.Tool(
            name="get-plays",
            description=base_description + """Get college football play-by-play data.
            Required: year AND week
            Optional: season_type, team, offense, defense, conference, offense_conference, defense_conference, play_type, classification
            Example valid queries:
            - year=2023, week=1
            - year=2023, week=1, team="Alabama"
            - year=2023, week=1, offense="Alabama", defense="Auburn"
            """,
            inputSchema=create_tool_schema(getPlays)
        ),
        types.Tool(
            name="get-drives",
            description=base_description + """Get college football drive data.
            Required: year
            Optional: season_type, week, team, offense, defense, conference, offense_conference, defense_conference, classification
            Example valid queries:
            - year=2023
            - year=2023, team="Alabama"
            - year=2023, offense="Alabama", defense="Auburn"
            """,
            inputSchema=create_tool_schema(getDrives)
        ),
        types.Tool(
            name="get-plays-stats",
            description=base_description + """Get college football play statistic data.
            Optional: year, week, team, game_id, athlete_id, stat_type_id, season_type, conference
            At least one parameter is required
            Example valid queries:
            - year=2023
            - game_id=401403910
            - team="Alabama", year=2023
            """,
            inputSchema=create_tool_schema(getPlaysStats)
        ),
        types.Tool(
            name="get-rankings",
            description=base_description + """Get college football rankings data.
            Required: year
            Optional: week, season_type
            Example valid queries:
            - year=2023
            - year=2023, week=1
            - year=2023, season_type="regular"
            """,
            inputSchema=create_tool_schema(getRankings)
        ),
        types.Tool(
            name="get-roster",
            description=base_description + """Get college football roster data and player names and numbers.
            Optional: team, year
            At least one parameter is required
            Example valid queries:
            - team="Alabama"
            - team="Alabama", year=2023
            """,
            inputSchema=create_tool_schema(getRoster)
        ),
        types.Tool(
            name="get-coaches",
            description=base_description + """Get college football coach and coaching staff names, teams, and work history.
            Optional: firstName, lastName, team, year, minYear, maxYear
            At least one parameter is required
            Example valid queries:
            - firstname="james", lastname="franklin"
            - team="Alabama", minyear=2021, maxyear=2025
            - team="Alabama"
            - team="Alabama", year=2023
            """,
            inputSchema=create_tool_schema(getCoaches)
        ),
        types.Tool(
            name="get-lines",
            description=base_description + """Get college football historical betting data and lines.
            Required: year OR gameId
            Optional: seasonType, week, team, home, away, conference, provider
            At least one parameter is required
            Example valid queries:
            - year=2023
            - gameId=401403910
            - year=2023, seasonType="regular"
            - team="Alabama", year=2023
            - year=2025, home="Penn State", away="Ohio State"
            - year=2025, conference="SEC"
            """,
            inputSchema=create_tool_schema(getLines)
        ),
        types.Tool(
            name="get-pregame-win-probability",
            description=base_description + """Get college football pregame win probability data.
            Optional: year, week, team, season_type
            At least one parameter is required
            Example valid queries:
            - year=2023
            - team="Alabama"
            - year=2023, week=1
            """,
            inputSchema=create_tool_schema(getMetricsPregameWp)
        ),
        types.Tool(
            name="get-advanced-box-score",
            description=base_description + """Get advanced box score data for college football games.
            Required: gameId
            Example valid queries:
            - gameId=401403910
            """,
            inputSchema=create_tool_schema(getAdvancedBoxScore)
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle tool execution requests."""
    if not arguments:
        raise ValueError("Arguments are required")

    # Map tool names to their parameter schemas
    schema_map = {
        "get-games": getGames,
        "get-records": getTeamRecords,
        "get-games-teams": getGamesTeams,
        "get-plays": getPlays,
        "get-drives": getDrives,
        "get-plays-stats": getPlaysStats,
        "get-rankings": getRankings,
        "get-roster": getRoster,
        "get-coaches": getCoaches,
        "get-lines": getLines,
        "get-pregame-win-probability": getMetricsPregameWp,
        "get-advanced-box-score": getAdvancedBoxScore
    }

    if name not in schema_map:
        raise ValueError(f"Unknown tool: {name}")

    # Validate parameters against schema
    try:
        validated_params = validate_params(arguments, schema_map[name])
    except ValueError as e:
        return [types.TextContent(
            type="text",
            text=f"Validation error: {str(e)}"
        )]

    endpoint_map = {
        "get-games": "/games",
        "get-records": "/records",
        "get-games-teams": "/games/teams",
        "get-plays": "/plays",
        "get-drives": "/drives",
        "get-plays-stats": "/plays/stats",
        "get-rankings": "/rankings",
        "get-roster": "/roster",
        "get-coaches": "/coaches",
        "get-lines": "/lines",
        "get-pregame-win-probability": "/metrics/wp/pregame",
        "get-advanced-box-score": "/game/box/advanced"
    }
   
    async with await get_api_client() as client:
        try:
            start = time.monotonic()
            method = "GET"
            url = endpoint_map[name]
            endpoint_path = endpoint_map[name]

            # log request
            _dbg(1, "→ %s %s params=%s", method, endpoint_path, validated_params if DEBUG_LEVEL >= 2 else "{...}")
            if DEBUG_LEVEL >= 2:
                _dbg(2, "Request headers: %s", dict(client.headers))
                if method == "POST":
                    _dbg(2, "Request body: %s", arguments)

            # Build canonical request to get exact full URL (sorted/encoded)
            req = client.build_request(method, endpoint_path, params=validated_params)
            full_url = str(req.url)

            # -----------------------------
            # Cache lookup by FULL URL (hashed)
            # -----------------------------
            r = await _get_redis()
            cache_key = _url_cache_key(full_url)
            if r is not None:
                try:
                    cached_text = await r.get(cache_key)
                    if cached_text:
                        _dbg(1, "CFBD cache HIT: %s", cache_key)
                        data = json.loads(cached_text)
                        elapsed_ms = (time.monotonic() - start) * 1000
                        _dbg(1, "← %s %s %d in %.1fms (cache)", method, full_url, 200, elapsed_ms)
                        return [types.TextContent(type="text", text=str(data))]
                except Exception as e:
                    _dbg(1, "CFBD cache get error: %s", e)

            # Cache miss → call API
            _dbg(1, "→ CFBD %s %s", method, full_url)
            response = await client.send(req)
            elapsed_ms = (time.monotonic() - start) * 1000

            # log response
            _dbg(1, "← %s %s %d in %.1fms", method, full_url, response.status_code, elapsed_ms)
            if DEBUG_LEVEL >= 2:
                _dbg(2, "Response headers: %s", dict(response.headers))

            response.raise_for_status()
            raw_text = response.text
            data = json.loads(raw_text)

            # -----------------------------
            # Cache store by FULL URL (hashed)
            # -----------------------------
            if r is not None:
                try:
                    ttl = _ttl_for_endpoint_path(full_url)
                    await r.set(cache_key, raw_text, ex=ttl)
                    endpoint_path = _endpoint_path_from_url(full_url)
                    _dbg(1, "CFBD cache SET: %s (%s, ttl=%ds)", cache_key, endpoint_path, ttl)
                except Exception as e:
                    _dbg(1, "CFBD cache set error: %s", e)

            # For POSTs at level 1, also log request/response body
            if DEBUG_LEVEL >= 1 and method == "POST":
                _dbg(1, "POST body: %s", arguments)
                _dbg(1, "POST reply: %s", _trim(str(data)))

            return [types.TextContent(
                type="text",
                text=str(data)
            )]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return [types.TextContent(
                    type="text",
                    text="401: API authentication failed. Please check your API key."
                )]
            elif e.response.status_code == 403:
                return [types.TextContent(
                    type="text",
                    text="403: API access forbidden. Please check your permission."
                )]
            elif e.response.status_code == 429:
                return [types.TextContent(
                    type="text",
                    text="429: Rate limit exceeded. Please try again later."
                )]
            else:
                return [types.TextContent(
                    type="text",
                    text=f"API Error: {e}"
                )]
        except httpx.RequestError as e:
            return [types.TextContent(
                type="text",
                text=f"Network error: {str(e)}"
            )]

async def main() -> None:
    """Run the server."""
    try:
        # Get metadata from project.toml
        pkg_metadata = metadata("cfbd-mcp-server")
        server_name = pkg_metadata["Name"]
        server_version = pkg_metadata["Version"]
    except Exception as e:
        # Fallback values in case metadata can't be read
        print(f"Warning: Could not read package metadata: {e}", file=sys.stderr)
        server_name = "cfbd-mcp-server"
        server_version = "0.4.0"
    
    # Add this line for startup confirmation
    print("CFB Data MCP Server starting...", file=sys.stderr)
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        print("Server initialized and ready for connections", file=sys.stderr)
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=server_name,
                server_version=server_version,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())
