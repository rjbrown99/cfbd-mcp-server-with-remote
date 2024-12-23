# College Football Data API
# api.collegefootballdata.com

from typing import TypedDict, Optional, List, Union

# custom classes
class GameClock(TypedDict):
    minutes: int
    seconds: int

class GameRecord(TypedDict):
    games: int
    wins: int
    losses: int
    ties: int

class Rank(TypedDict):
    rank: int
    school: str
    conference: Optional[str]  # Optional since team might not have conference
    firstPlaceVotes: Optional[int]  # Optional since not all polls report this
    points: Optional[int]  # Optional since not all polls report points

class Poll(TypedDict):
    poll: str
    ranks: List[Rank]

class TeamStat(TypedDict):
    category: str
    stat: str

class Team(TypedDict):
    schoolId: int
    school: str
    conference: Optional[str]  # Optional since team might not have conference
    homeAway: str  # Presumably "home" or "away"
    points: Optional[int]  # Optional since game might not be completed
    stats: List[TeamStat]

class QuarterBreakdown(TypedDict):
    """Breakdown of statistics by quarter and total"""
    total: float
    quarter1: float
    quarter2: float
    quarter3: float
    quarter4: float

class PPABreakdown(TypedDict):
    """Predicted Points Added breakdown by play type and quarter"""
    total: float
    quarter1: float
    quarter2: float
    quarter3: float
    quarter4: float

class TeamPPA(TypedDict):
    """Team-level PPA statistics"""
    team: str
    plays: int
    overall: PPABreakdown
    passing: PPABreakdown
    rushing: PPABreakdown

class SuccessRateBreakdown(TypedDict):
    """Success rate statistics by quarter"""
    team: str
    overall: QuarterBreakdown
    standardDowns: QuarterBreakdown
    passingDowns: QuarterBreakdown

class Explosiveness(TypedDict):
    """Explosiveness statistics by quarter"""
    team: str
    overall: QuarterBreakdown

class RushingStats(TypedDict):
    """Detailed rushing statistics"""
    team: str
    powerSuccess: float
    stuffRate: float
    lineYards: float
    lineYardsAverage: float
    secondLevelYards: float
    secondLevelYardsAverage: float
    openFieldYards: float
    openFieldYardsAverage: float

class HavocStats(TypedDict):
    """Havoc rate statistics"""
    team: str
    total: float
    frontSeven: float
    db: float

class ScoringOpportunities(TypedDict):
    """Scoring opportunity statistics"""
    team: str
    opportunities: int
    points: int
    pointsPerOpportunity: float

class FieldPosition(TypedDict):
    """Field position statistics"""
    team: str
    averageStart: float
    averageStartingPredictedPoints: float

class PlayerUsage(TypedDict):
    """Player usage statistics"""
    player: str
    team: str
    position: str
    total: float
    quarter1: float
    quarter2: float
    quarter3: float
    quarter4: float
    rushing: float
    passing: float

class PlayerPPAStats(TypedDict):
    """Player PPA statistics breakdown"""
    total: float
    quarter1: float
    quarter2: float
    quarter3: float
    quarter4: float
    rushing: float
    passing: float

class PlayerPPA(TypedDict):
    """Complete player PPA statistics"""
    player: str
    team: str
    position: str
    average: PlayerPPAStats
    cumulative: PlayerPPAStats

class TeamStats(TypedDict):
    """Collection of all team-level statistics"""
    ppa: List[TeamPPA]
    cumulativePpa: List[TeamPPA]
    successRates: List[SuccessRateBreakdown]
    explosiveness: List[Explosiveness]
    rushing: List[RushingStats]
    havoc: List[HavocStats]
    scoringOpportunities: List[ScoringOpportunities]
    fieldPosition: List[FieldPosition]

class PlayerStats(TypedDict):
    """Collection of all player-level statistics"""
    usage: List[PlayerUsage]
    ppa: List[PlayerPPA]

################################################################################

# Endpoint parameters & responses
class getGames(TypedDict): # /games endpoint
    year: int
    week: Optional[int]
    season_type: Optional[str]
    team: Optional[str]
    conference: Optional[str]
    category: Optional[str]
    game_id: Optional[int]

class GamesResponse(TypedDict): # /games response
    id: int
    season: int
    week: int
    season_type: str
    start_date: str
    start_time_tbd: bool
    completed: bool
    neutral_site: bool
    conference_game: bool
    attendance: Optional[int]  # Making optional since it might be null
    venue_id: Optional[int]
    venue: Optional[str]
    home_id: int
    home_team: str
    home_conference: Optional[str]  # Making optional since some teams might not have conferences
    home_division: Optional[str]
    home_points: Optional[int]  # Optional since game might not be completed
    home_line_scores: List[int]
    home_post_win_prob: Optional[float]  # Using float for probability
    home_pregame_elo: Optional[float]
    home_postgame_elo: Optional[float]
    away_id: int
    away_team: str
    away_conference: Optional[str]
    away_division: Optional[str]
    away_points: Optional[int]
    away_line_scores: List[int]
    away_post_win_prob: Optional[float]
    away_pregame_elo: Optional[float]
    away_postgame_elo: Optional[float]
    excitement_index: Optional[float]
    highlights: Optional[str]
    notes: Optional[str]

class getAdvancedBoxScore(TypedDict): # /game/box/advanced endpoint
    gameId: int

class AdvancedBoxScoreResponse(TypedDict): # /game/box/advanced endpoint
    teams: TeamStats
    players: PlayerStats

class getTeamRecords(TypedDict): # /records endpoint
    year: Optional[int]
    team: Optional[str]
    conference: Optional[str]

class TeamRecordResponse(TypedDict): # /records repsonse
    year: int
    teamId: int
    team: str
    conference: Optional[str]  # Optional since team might not have conference
    division: Optional[str]
    expectedWins: float  # Using float since expected wins can be fractional
    total: GameRecord
    conferenceGames: GameRecord
    homeGames: GameRecord
    awayGames: GameRecord

class getGamesTeams(TypedDict): # /games/teams endpoint
    year: int
    week: Optional[int]
    season_type: Optional[str]
    team: Optional[str]
    conference: Optional[str]
    game_id: Optional[int]
    classification: Optional[str]

class GamesTeamsResponse(TypedDict): # /games/teams response
    id: int
    teams: List[Team]

class getPlays(TypedDict): # /plays endpoint
    year: int
    week: int
    season_type: Optional[str]
    team: Optional[str]
    offense: Optional[str]
    defense: Optional[str]
    conference: Optional[str]
    offense_conference: Optional[str]
    defense_conference: Optional[str]
    play_type: Optional[int]
    classification: Optional[str]

class PlaysResponse(TypedDict): # /plays response
    id: int
    drive_id: int
    game_id: int
    drive_number: int
    play_number: int
    offense: str
    offense_conference: Optional[str]  # Optional since team might not have conference
    offense_score: int
    defense: str
    home: str
    away: str
    defense_conference: Optional[str]
    defense_score: int
    period: int
    clock: GameClock
    offense_timeouts: int
    defense_timeouts: int
    yard_line: int
    yards_to_goal: int
    down: Optional[int]  # Optional since some plays might not have downs (kickoffs, etc)
    distance: Optional[int]
    yards_gained: int
    scoring: bool
    play_type: str
    play_text: str
    ppa: Optional[float]  # Using float for predicted points added
    wallclock: Optional[str]  # Timestamp of the play

class getDrives(TypedDict): # /drives endpoint
    year: int
    season_type: Optional[str]
    week: Optional[int]
    team: Optional[str]
    offense: Optional[str]
    defense: Optional[str]
    conference: Optional[str]
    offense_conference: Optional[str]
    defense_conference: Optional[str]
    classification: Optional[str]

class DrivesResponse(TypedDict): # /drives response
    offense: str
    offense_conference: Optional[str]  # Optional since team might not have conference
    defense: str
    defense_conference: Optional[str]
    game_id: int
    id: int
    drive_number: int
    scoring: bool
    start_period: int
    start_yardline: int
    start_yards_to_goal: int
    start_time: GameClock
    end_period: int
    end_yardline: int
    end_yards_to_goal: int
    end_time: GameClock
    plays: int
    yards: int
    drive_result: str
    is_home_offense: bool
    start_offense_score: int
    start_defense_score: int
    end_offense_score: int
    end_defense_score: int

class getPlayStats(TypedDict): # /play/stats endpoint
    year: Optional[int]
    week: Optional[int]
    team: Optional[str]
    game_id: Optional[int]
    athlete_id: Optional[int]
    stat_type_id: Optional[int]
    season_type: Optional[str]
    conference: Optional[str]

class PlayStatsResponse(TypedDict): # /play/stats response
    gameId: int
    season: int
    week: int
    team: str
    conference: Optional[str]  # Optional since team might not have conference
    opponent: str
    teamScore: Optional[int]  # Optional since game might not be completed
    opponentScore: Optional[int]
    driveId: int
    playId: int
    period: int
    clock: GameClock
    yardsToGoal: int
    down: Optional[int]  # Optional since some plays don't have downs (kickoffs, etc)
    distance: Optional[int]
    athleteId: int
    athleteName: str
    statType: str
    stat: int  # The numerical value of the statistic

class getRankings(TypedDict): # /rankings endpoint
    year: int
    week: Optional[int]
    season_type: Optional[str]

class RankingsResponse(TypedDict): # /rankings response
    season: int
    seasonType: str
    week: int
    polls: List[Poll]

class getMetricsPregameWp(TypedDict): # /metrics/wp/pregame endpoint
    year: Optional[int]
    week: Optional[int]
    team: Optional[str]
    season_type: Optional[str]

class MetricsPregameWpResponse(TypedDict): # /metrics/wp/pregame response
    season: int
    seasonType: str
    week: int
    gameId: int
    homeTeam: str
    awayTeam: str
    spread: float  # Using float since spread can be decimal
    homeWinProb: float  # Using float for probability (0-1)

# Since the API returns a list of win probabilities
GamesResponseList = List[GamesResponse]
TeamRecordResponseList = List[TeamRecordResponse]
GamesTeamsResponseList = List[GamesTeamsResponse]
PlaysResponseList = List[PlaysResponse]
DrivesResponseList = List[DrivesResponse]
PlayStatsResponseList = List[PlayStatsResponse]
RankingsResponseList = List[RankingsResponse]
MetricsPregameWpResponseList = List[MetricsPregameWpResponse]


VALID_SEASONS = range(2001, 2024)
VALID_WEEKS = range(1, 16)
VALID_SEASON_TYPES = ['regular', 'postseason']
VALID_DIVISIONS = ['fbs', 'fcs', 'ii', 'iii']
