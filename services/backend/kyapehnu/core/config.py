import logging
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Final, Literal, Self, cast
from urllib.parse import urlsplit

import yaml
from limits import parse
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from kyapehnu.core.errors import InvalidInputError
from kyapehnu.core.schemas import Rating

PACKAGE_DIR = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)

DEFAULT_DATA_DIR: Final = Path("/data")
DEFAULT_PUBLIC_URL: Final = "http://localhost:8080"
DEFAULT_VALKEY_URL: Final = "redis://localhost:6379/0"
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
DEFAULT_FILES_REGION: Final = "ap-south-1"
BUCKET_NAME_PATTERN: Final = (
    r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"  # S3's own rule: 3 to 63 of these, no edge dash or dot
)
DEFAULT_SWEEP_MAX_DELETES: Final = 500
MAX_SWEEP_DELETES: Final = 100_000

MAX_UPLOAD_FILES: Final = 200
MAX_UPLOAD_FILE_BYTES: Final = 25 * 1024 * 1024
MAX_BODY_BYTES: Final = MAX_UPLOAD_FILES * MAX_UPLOAD_FILE_BYTES + 1024 * 1024  # + multipart framing
MAX_JSON_BODY_BYTES: Final = 1024 * 1024  # every non-upload body is read into memory whole


class Settings(BaseSettings):
    """Every machine- and deployment-specific value, read from KYAPEHNU_* environment variables (no .env file is read
    here: compose passes the variables in)."""

    # hide_input_in_errors: a refused valkey_url must not print the credential it carried into a startup error
    # env_ignore_empty: compose passes `${VAR:-}` for every optional variable, and an empty one must mean "not set"
    model_config = SettingsConfigDict(
        env_prefix="KYAPEHNU_", extra="ignore", populate_by_name=True, hide_input_in_errors=True, env_ignore_empty=True
    )

    llm_provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "KYAPEHNU_ANTHROPIC_API_KEY")
    )
    anthropic_model: str = "claude-sonnet-5-5"
    openai_base_url: str = "http://localhost:11434/v1"
    openai_api_key: str = "ollama"
    openai_model: str = "qwen2.5vl:7b"
    data_dir: Path = DEFAULT_DATA_DIR
    files_bucket: str | None = Field(default=None, pattern=BUCKET_NAME_PATTERN)  # set: pictures live in this S3 bucket
    files_region: str = DEFAULT_FILES_REGION
    sweep_max_deletes: int = Field(default=DEFAULT_SWEEP_MAX_DELETES, ge=1, le=MAX_SWEEP_DELETES)
    config_file: Path = Field(default_factory=lambda data: data["data_dir"] / "config.yaml")
    public_url: str = DEFAULT_PUBLIC_URL
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    valkey_url: str = DEFAULT_VALKEY_URL
    valkey_password: SecretStr | None = None
    session_idle_days: int = Field(default=30, ge=1, le=365)
    auth_rate_limit: str = "20/minute"
    docs_enabled: bool = False
    dev_login: bool = False  # POST /api/auth/dev-login, loopback public URL only (compose.dev.yaml)

    @field_validator("public_url")
    @classmethod
    def _origin_only(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise InvalidInputError("public_url must be http:// or https:// with a host")
        if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise InvalidInputError("public_url is an origin: no credentials, path, query or fragment")
        return f"{parts.scheme}://{parts.netloc.lower()}"  # browsers send the Origin header in lower case

    @field_validator("valkey_url")
    @classmethod
    def _no_credential_in_url(cls, v: str) -> str:
        if urlsplit(v).password or urlsplit(v).username:
            raise InvalidInputError("valkey_url must not carry a credential: set KYAPEHNU_VALKEY_PASSWORD")
        return v

    @field_validator("auth_rate_limit")
    @classmethod
    def _parsable_rate(cls, v: str) -> str:
        parse(v)  # raises ValueError for anything but "<n>/<period>"
        return v

    @property
    def public_origin(self) -> str:
        return self.public_url

    @property
    def public_host(self) -> str:
        return cast(str, urlsplit(self.public_url).hostname)

    @property
    def public_https(self) -> bool:
        return urlsplit(self.public_url).scheme == "https"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host header values the request guard accepts: the public host and the loopback names."""
        return frozenset({self.public_host, *LOOPBACK_HOSTS})

    @property
    def google_configured(self) -> bool:
        return bool(
            self.google_client_id and self.google_client_secret and self.google_client_secret.get_secret_value()
        )


class IngestPrefs(BaseModel):
    min_bbox_area: float = 0.02
    bbox_padding: float = 0.05
    # With OpenAI as the provider, make a clean picture of every garment of an upload (one image generation each).
    auto_clean_pictures: bool = True
    # Keep only the item a photo is mainly showing: for photos of a person wearing clothes, for every photo, or never.
    main_item_only: Literal["worn", "always", "never"] = "worn"
    # With OpenAI as the provider, ask its decision model first whether a photo shows a garment at all (and not
    # innerwear); a photo that does not is turned away before any tagging.
    screen_photos: bool = True
    # Photos of one upload processed at a time: each costs a screening, a tagging and often a clean-picture call.
    workers: int = Field(default=3, ge=1, le=8)


class DedupePrefs(BaseModel):
    llm_confidence: float = 0.7
    item_phash_distance: int = 6
    photo_phash_distance: int = 4
    max_delta_e: float = 15.0


class ProfileDefaults(BaseModel):
    budget_min: int = 1000
    budget_max: int = 5000


def _ordered_range(rng: tuple[int, int]) -> tuple[int, int]:
    if rng[0] > rng[1]:
        raise InvalidInputError("formality range must run from low to high")
    return rng


FormalityRange = Annotated[tuple[Rating, Rating], AfterValidator(_ordered_range)]


class Weights(BaseModel):
    """Score weights. They are relative: whatever is given is scaled to sum to 1."""

    model_config = ConfigDict(allow_inf_nan=False)  # an infinite weight would normalise to nan

    colour: float = Field(default=0.25, ge=0)
    pattern: float = Field(default=0.10, ge=0)
    formality: float = Field(default=0.20, ge=0)
    weather: float = Field(default=0.20, ge=0)
    repetition: float = Field(default=0.20, ge=0)
    forgotten: float = Field(default=0.05, ge=0)
    preference: float = Field(default=0.10, ge=0)  # what the user's thumbs taught (neutral until they rate something)

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        total = sum(self.model_dump().values())
        if total <= 0:
            raise InvalidInputError("weights must not all be zero")
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) / total)
        return self


class FormalityRanges(BaseModel):
    """Acceptable item formality per event type (one field each, so a partial override keeps the rest)."""

    gym: FormalityRange = (1, 1)
    casual: FormalityRange = (1, 3)
    travel: FormalityRange = (1, 3)
    date: FormalityRange = (2, 4)
    party: FormalityRange = (2, 4)
    festival: FormalityRange = (3, 5)
    office: FormalityRange = (3, 4)
    interview: FormalityRange = (4, 5)
    wedding: FormalityRange = (4, 5)


class OutfitPrefs(BaseModel):
    weights: Weights = Weights()
    # ponytail: ranking cost grows steeply with this (8: ~0.1 s, 20 with 30 items per slot: ~7 s); lower the ceiling if
    # someone sets it high and finds suggest slow
    per_slot: int = Field(default=8, ge=1, le=20)  # best items kept per slot before combining
    max_candidates: int = Field(default=300, ge=1, le=1000)
    formality: FormalityRanges = FormalityRanges()
    # Pictures of suggested outfits on a mannequin: for every card, only on request, or never.
    mannequin: Literal["auto", "button", "off"] = "auto"


class RepetitionPrefs(BaseModel):
    half_life_days: float = Field(default=14, gt=0)
    outfit_window_days: int = Field(default=30, ge=1)
    audience_window_days: int = Field(default=90, ge=1)
    forgotten_days: int = Field(default=60, ge=1)


BandLayer = Literal["none", "optional", "wanted"]


class WeatherBand(BaseModel):
    """Warmth wanted when the feels-like midpoint is at least from_c (None: any lower)."""

    from_c: float | None = None
    warmth_min: Rating
    warmth_max: Rating
    layer: BandLayer

    @model_validator(mode="after")
    def _warmth_order(self) -> Self:
        if self.warmth_min > self.warmth_max:
            raise InvalidInputError("bands: warmth_min must not exceed warmth_max")
        return self


def _default_bands() -> list[WeatherBand]:
    rows: list[tuple[float | None, int, int, BandLayer]] = [
        (30, 1, 2, "none"),
        (24, 1, 3, "none"),
        (18, 2, 4, "optional"),
        (10, 3, 5, "wanted"),
        (None, 4, 5, "wanted"),
    ]
    return [WeatherBand(from_c=c, warmth_min=lo, warmth_max=hi, layer=layer) for c, lo, hi, layer in rows]


class WeatherPrefs(BaseModel):
    bands: list[WeatherBand] = Field(default_factory=_default_bands, min_length=1)
    humid_pct: float = 70
    humid_feels_like_c: float = 28
    humid_fabrics: list[str] = ["polyester", "nylon", "wool", "leather", "velvet"]
    rain_probability_pct: float = 40
    uv_index: float = 8
    aqi: float = 200
    wind_kmh: float = 35
    not_rain_safe_words: list[str] = ["suede", "canvas", "satin", "silk", "velvet", "mesh"]
    default_event_hours: int = Field(default=3, ge=1, le=24)

    @field_validator("bands")
    @classmethod
    def _bands_descend_to_an_open_end(cls, bands: list[WeatherBand]) -> list[WeatherBand]:
        *bounded, last = bands
        thresholds = [b.from_c for b in bounded if b.from_c is not None]
        if last.from_c is not None or len(thresholds) != len(bounded):
            raise InvalidInputError("bands: only the last band has no from_c")
        if any(hotter <= cooler for hotter, cooler in pairwise(thresholds)):
            raise InvalidInputError("bands: from_c must strictly descend")
        return bands


class Prefs(BaseModel):
    """User preferences from config.yaml. Every field has a default, so the file is optional."""

    region: str = "IN"
    home_city: str = "Gurugram"
    currency: str = "INR"
    ingest: IngestPrefs = IngestPrefs()
    dedupe: DedupePrefs = DedupePrefs()
    profile: ProfileDefaults = ProfileDefaults()
    outfits: OutfitPrefs = OutfitPrefs()
    repetition: RepetitionPrefs = RepetitionPrefs()
    weather: WeatherPrefs = WeatherPrefs()


def load_prefs(path: Path) -> Prefs:
    if not path.exists():
        log.warning("%s not found; using default preferences (see config.example.yaml)", path)
        return Prefs()
    return Prefs.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@cache
def get_settings() -> Settings:
    return Settings()
