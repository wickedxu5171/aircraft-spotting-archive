from dataclasses import asdict, dataclass
from datetime import date, datetime


ALGORITHM_VERSION = "weighted-v1"


def normalize_identifier(value: str | None) -> str | None:
    return value.replace(" ", "").upper() if value else None


@dataclass(frozen=True)
class SpottingEvidence:
    registration: str | None
    observed_at: datetime | None
    airport_code: str | None
    flight_number: str | None
    route_origin: str | None = None
    route_destination: str | None = None
    spotting_date: date | None = None
    airline_iata: str | None = None
    airline_icao: str | None = None
    observed_at_source: str | None = None


@dataclass(frozen=True)
class FlightCandidate:
    registration: str | None
    first_seen: datetime
    last_seen: datetime
    callsign: str | None
    airport_codes: tuple[str, ...] = ()
    route_origin: str | None = None
    route_destination: str | None = None


@dataclass(frozen=True)
class MatchBreakdown:
    algorithm_version: str
    registration_score: float
    time_score: float
    airport_score: float
    callsign_score: float
    route_score: float
    total_score: float
    status: str
    explanation: dict

    def to_dict(self):
        return asdict(self)


def _time_score(
    observed_at: datetime | None,
    spotting_date: date | None,
    candidate: FlightCandidate,
) -> tuple[float, str]:
    if observed_at is None:
        if spotting_date and candidate.first_seen.date() <= spotting_date <= candidate.last_seen.date():
            return 10.0, "Flight overlaps the spotting date; no exact spotting time"
        return 0.0, "No exact spotting time or date overlap"
    if candidate.first_seen <= observed_at <= candidate.last_seen:
        return 25.0, "Spotting time falls inside the ADS-B flight window"
    delta_minutes = min(
        abs((observed_at - candidate.first_seen).total_seconds()),
        abs((observed_at - candidate.last_seen).total_seconds()),
    ) / 60
    if delta_minutes <= 15:
        return 22.0, f"Nearest flight boundary is {delta_minutes:.1f} minutes away"
    if delta_minutes <= 30:
        return 18.0, f"Nearest flight boundary is {delta_minutes:.1f} minutes away"
    if delta_minutes <= 60:
        return 10.0, f"Nearest flight boundary is {delta_minutes:.1f} minutes away"
    if delta_minutes <= 120:
        return 4.0, f"Nearest flight boundary is {delta_minutes:.1f} minutes away"
    return 0.0, f"Nearest flight boundary is {delta_minutes:.1f} minutes away"


def callsign_aliases(
    flight_number: str | None,
    airline_iata: str | None = None,
    airline_icao: str | None = None,
) -> set[str]:
    normalized_flight = normalize_identifier(flight_number)
    aliases = {normalized_flight} if normalized_flight else set()
    normalized_iata = normalize_identifier(airline_iata)
    normalized_icao = normalize_identifier(airline_icao)
    if (
        normalized_flight
        and normalized_iata
        and normalized_icao
        and normalized_flight.startswith(normalized_iata)
    ):
        suffix = normalized_flight[len(normalized_iata) :]
        if suffix:
            aliases.add(f"{normalized_icao}{suffix}")
    return aliases


def score_candidate(
    spotting: SpottingEvidence, candidate: FlightCandidate
) -> MatchBreakdown:
    registration_equal = (
        normalize_identifier(spotting.registration) is not None
        and normalize_identifier(spotting.registration)
        == normalize_identifier(candidate.registration)
    )
    registration_score = 40.0 if registration_equal else 0.0

    time_score, time_reason = _time_score(
        spotting.observed_at, spotting.spotting_date, candidate
    )
    if spotting.observed_at_source and spotting.observed_at is not None:
        time_reason = f"{spotting.observed_at_source}: {time_reason}"

    normalized_airport = normalize_identifier(spotting.airport_code)
    candidate_airports = {
        normalize_identifier(code) for code in candidate.airport_codes if code
    }
    airport_equal = bool(
        normalized_airport and normalized_airport in candidate_airports
    )
    airport_score = 15.0 if airport_equal else 0.0

    expected_callsigns = callsign_aliases(
        spotting.flight_number, spotting.airline_iata, spotting.airline_icao
    )
    callsign_equal = normalize_identifier(candidate.callsign) in expected_callsigns
    callsign_score = 15.0 if callsign_equal else 0.0

    origin_equal = (
        normalize_identifier(spotting.route_origin) is not None
        and normalize_identifier(spotting.route_origin)
        == normalize_identifier(candidate.route_origin)
    )
    destination_equal = (
        normalize_identifier(spotting.route_destination) is not None
        and normalize_identifier(spotting.route_destination)
        == normalize_identifier(candidate.route_destination)
    )
    route_score = 5.0 if origin_equal and destination_equal else 2.5 if (origin_equal or destination_equal) else 0.0

    total = registration_score + time_score + airport_score + callsign_score + route_score
    status = "matched" if total >= 80 else "review" if total >= 60 else "unmatched"

    return MatchBreakdown(
        algorithm_version=ALGORITHM_VERSION,
        registration_score=registration_score,
        time_score=time_score,
        airport_score=airport_score,
        callsign_score=callsign_score,
        route_score=route_score,
        total_score=total,
        status=status,
        explanation={
            "registration": "Exact registration match" if registration_equal else "Registration does not match",
            "time": time_reason,
            "airport": "Airport agrees with candidate" if airport_equal else "Airport unavailable or different",
            "callsign": "Flight number agrees with callsign" if callsign_equal else "Callsign unavailable or different",
            "route": "Both route endpoints agree" if route_score == 5 else "One route endpoint agrees" if route_score else "Route unavailable or different",
        },
    )
