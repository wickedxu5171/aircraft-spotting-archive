import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


PROTECTED_PARTITIONS = {
    "flights_data4": "day",
    "state_vectors_data4": "hour",
}
TABLE_ALLOWLIST = frozenset(PROTECTED_PARTITIONS)
ICAO24_PATTERN = re.compile(r"^[0-9a-f]{6}$")
AIRPORT_PATTERN = re.compile(r"^[A-Z0-9]{4}$")


class OpenSkyConfigurationError(RuntimeError):
    pass


class UnsafeOpenSkyQueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenSkyTrinoConfig:
    username: str
    host: str = "trino.opensky-network.org"
    port: int = 443
    catalog: str = "minio"
    schema: str = "osky"

    @classmethod
    def from_env(cls):
        username = os.getenv("OPENSKY_USERNAME", "").strip().lower()
        if not username:
            raise OpenSkyConfigurationError(
                "Set OPENSKY_USERNAME in .env before connecting to OpenSky."
            )
        return cls(
            username=username,
            host=os.getenv("OPENSKY_TRINO_HOST", cls.host).strip(),
        )


def utc_day_partition(value: date) -> int:
    return int(datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def utc_hour_partition(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return int(value.timestamp())


def iter_utc_hour_partitions(start: datetime, end: datetime):
    current = datetime.fromtimestamp(utc_hour_partition(start), tz=timezone.utc)
    end_partition = utc_hour_partition(end)
    while int(current.timestamp()) <= end_partition:
        yield int(current.timestamp())
        current += timedelta(hours=1)


def validate_partitioned_query(query: str):
    lowered = re.sub(r"\s+", " ", query.lower())
    if lowered.lstrip().startswith(("describe ", "show ", "explain ")):
        return
    for table, partition in PROTECTED_PARTITIONS.items():
        if table in lowered and not re.search(rf"\b{partition}\s*=\s*\d+", lowered):
            raise UnsafeOpenSkyQueryError(
                f"Queries against {table} must include an exact {partition} partition."
            )


class OpenSkyTrinoClient:
    def __init__(
        self,
        config: OpenSkyTrinoConfig,
        connection=None,
        *,
        console_authentication: bool = False,
    ):
        self.config = config
        self._connection = connection
        self.console_authentication = console_authentication

    @classmethod
    def from_env(cls, *, console_authentication: bool = False):
        return cls(
            OpenSkyTrinoConfig.from_env(),
            console_authentication=console_authentication,
        )

    def connect(self):
        if self._connection is not None:
            return self._connection
        if self.console_authentication:
            os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        from trino.auth import ConsoleRedirectHandler, OAuth2Authentication
        from trino.dbapi import connect

        authentication = (
            OAuth2Authentication(
                redirect_auth_url_handler=ConsoleRedirectHandler()
            )
            if self.console_authentication
            else OAuth2Authentication()
        )
        self._connection = connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.username,
            auth=authentication,
            http_scheme="https",
            catalog=self.config.catalog,
            schema=self.config.schema,
            timezone="UTC",
            source="aircraft-spotting-archive",
        )
        return self._connection

    def execute(self, query: str) -> list[dict]:
        validate_partitioned_query(query)
        cursor = self.connect().cursor()
        cursor.execute(query)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def test_connection(self) -> list[dict]:
        return self.describe_table("flights_data4")

    def describe_table(self, table: str) -> list[dict]:
        normalized = table.strip().lower()
        if normalized not in TABLE_ALLOWLIST:
            raise ValueError(f"Unsupported OpenSky table: {table}")
        return self.execute(f"DESCRIBE {normalized}")

    def build_airport_flights_query(self, flight_date: date, airport_icao: str) -> str:
        airport_icao = airport_icao.strip().upper()
        if not AIRPORT_PATTERN.fullmatch(airport_icao):
            raise ValueError("Airport must be a four-character ICAO code.")
        day = utc_day_partition(flight_date)
        return f"""
SELECT
    lower(icao24) AS icao24,
    firstseen,
    lastseen,
    trim(upper(callsign)) AS callsign,
    upper(estdepartureairport) AS estdepartureairport,
    upper(estarrivalairport) AS estarrivalairport,
    day
FROM flights_data4
WHERE day = {day}
  AND (
      upper(estdepartureairport) = '{airport_icao}'
      OR upper(estarrivalairport) = '{airport_icao}'
  )
ORDER BY firstseen, icao24
""".strip()

    def fetch_airport_flights(self, flight_date: date, airport_icao: str) -> tuple[str, list[dict]]:
        query = self.build_airport_flights_query(flight_date, airport_icao)
        return query, self.execute(query)

    def build_state_vectors_query(
        self,
        *,
        icao24: str,
        hour_partition: int,
        start_epoch: int,
        end_epoch: int,
        sample_seconds: int = 60,
    ) -> str:
        icao24 = icao24.strip().lower()
        if not ICAO24_PATTERN.fullmatch(icao24):
            raise ValueError("ICAO24 must contain exactly six hexadecimal characters.")
        if sample_seconds < 5 or sample_seconds > 300:
            raise ValueError("sample_seconds must be between 5 and 300.")
        if end_epoch < start_epoch:
            raise ValueError("Track end time must not precede its start time.")
        if hour_partition != utc_hour_partition(
            datetime.fromtimestamp(hour_partition, tz=timezone.utc)
        ):
            raise ValueError("hour_partition must be aligned to a UTC hour.")
        return f"""
WITH ranked AS (
    SELECT
        time,
        lower(icao24) AS icao24,
        lat,
        lon,
        velocity,
        heading,
        vertrate,
        trim(upper(callsign)) AS callsign,
        onground,
        baroaltitude,
        geoaltitude,
        row_number() OVER (
            PARTITION BY icao24, floor(time / {sample_seconds})
            ORDER BY time DESC
        ) AS sample_rank
    FROM state_vectors_data4
    WHERE hour = {int(hour_partition)}
      AND time BETWEEN {int(start_epoch)} AND {int(end_epoch)}
      AND lower(icao24) = '{icao24}'
      AND lat IS NOT NULL
      AND lon IS NOT NULL
      AND time - lastcontact <= 15
)
SELECT
    time, icao24, lat, lon, velocity, heading, vertrate,
    callsign, onground, baroaltitude, geoaltitude
FROM ranked
WHERE sample_rank = 1
ORDER BY time
""".strip()

    def fetch_track(
        self,
        *,
        icao24: str,
        start: datetime,
        end: datetime,
        sample_seconds: int = 60,
    ) -> tuple[list[str], list[dict]]:
        start_epoch = int(start.replace(tzinfo=timezone.utc).timestamp()) if start.tzinfo is None else int(start.timestamp())
        end_epoch = int(end.replace(tzinfo=timezone.utc).timestamp()) if end.tzinfo is None else int(end.timestamp())
        queries = []
        rows = []
        for hour in iter_utc_hour_partitions(start, end):
            query = self.build_state_vectors_query(
                icao24=icao24,
                hour_partition=hour,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                sample_seconds=sample_seconds,
            )
            queries.append(query)
            rows.extend(self.execute(query))
        return queries, rows


def write_query_artifact(
    output_directory: str | Path,
    *,
    label: str,
    queries: list[str],
    rows: list[dict],
) -> Path:
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    query_hash = hashlib.sha256("\n".join(queries).encode("utf-8")).hexdigest()[:12]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_directory / f"{timestamp}_{label}_{query_hash}.json"
    payload = {
        "source": "OpenSky Historical Database (Trino)",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query_hash": query_hash,
        "queries": queries,
        "row_count": len(rows),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
