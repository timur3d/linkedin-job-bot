"""Loads config.yaml (what to look for) and the environment (the repository's secrets)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """config.yaml or the environment is wrong; the message says what to fix."""


@dataclass(frozen=True)
class Rule:
    name: str
    title: tuple[str, ...]
    title_not: tuple[str, ...] = ()
    description: str | None = None
    min_description_hits: int = 1
    or_employment: tuple[str, ...] = ()
    without_description: str = "reject"

    @property
    def needs_details(self) -> bool:
        return bool(self.description or self.or_employment)


@dataclass(frozen=True)
class Track:
    id: str
    label: str
    emoji: str
    search_terms: tuple[str, ...]
    exclude_title: tuple[str, ...]
    rules: tuple[Rule, ...]
    star: tuple[str, ...] = ()


@dataclass(frozen=True)
class Highlight:
    name: str
    title: str | None = None
    description: str | None = None
    employment: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    tracks: tuple[Track, ...]
    highlights: tuple[Highlight, ...]
    term_groups: dict[str, tuple[str, ...]]

    locations: tuple[str, ...] = ("Israel",)
    first_run_hours: int = 24
    max_lookback_hours: int = 72
    results_per_query: int = 50
    pause_between_queries: tuple[float, float] = (4, 9)
    read_descriptions: bool = True
    max_descriptions_per_cycle: int = 60
    pause_between_descriptions: tuple[float, float] = (2, 5)
    exclude_locations: tuple[str, ...] = ()
    exclude_companies: tuple[str, ...] = ()

    show_match_reason: bool = True
    repost_window_days: int = 14
    keep_history_days: int = 120

    def track(self, track_id: str) -> Track | None:
        return next((track for track in self.tracks if track.id == track_id), None)


@dataclass(frozen=True)
class Settings:
    """Runtime settings that come from the environment rather than config.yaml."""

    bot_token: str | None
    chat_id: int | None
    db_path: Path
    proxies: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        chat_id = os.getenv("CHAT_ID", "").strip()
        try:
            parsed_chat_id = int(chat_id) if chat_id else None
        except ValueError:
            raise ConfigError(f"CHAT_ID must be a number, got {chat_id!r}") from None
        proxies = tuple(p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip())
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "").strip() or None,
            chat_id=parsed_chat_id,
            db_path=Path(os.getenv("DB_PATH", "data/jobs.db")),
            proxies=proxies,
        )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping at the top level")
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> Config:
    _only_keys(data, "config", {"linkedin", "notifications", "tracks", "highlights", "term_groups"})

    groups = _term_groups(data.get("term_groups"))
    linkedin = _section(
        data,
        "linkedin",
        {
            "locations",
            "first_run_hours",
            "max_lookback_hours",
            "results_per_query",
            "pause_between_queries",
            "read_descriptions",
            "max_descriptions_per_cycle",
            "pause_between_descriptions",
            "exclude_locations",
            "exclude_companies",
        },
    )
    notifications = _section(data, "notifications", {"show_match_reason", "repost_window_days", "keep_history_days"})

    highlights = _highlights(data.get("highlights"), groups)
    tracks = _tracks(data.get("tracks"), groups, {h.name for h in highlights} | {"remote"})

    defaults = Config(tracks=(), highlights=(), term_groups={})
    config = Config(
        tracks=tracks,
        highlights=highlights,
        term_groups=groups,
        locations=_strings(linkedin.get("locations", list(defaults.locations)), "linkedin.locations"),
        first_run_hours=int(_number(linkedin, "linkedin.first_run_hours", defaults.first_run_hours, minimum=1)),
        max_lookback_hours=int(
            _number(linkedin, "linkedin.max_lookback_hours", defaults.max_lookback_hours, minimum=1)
        ),
        results_per_query=int(_number(linkedin, "linkedin.results_per_query", defaults.results_per_query, minimum=1)),
        pause_between_queries=_range(linkedin, "linkedin.pause_between_queries", defaults.pause_between_queries),
        read_descriptions=_boolean(linkedin, "linkedin.read_descriptions", defaults.read_descriptions),
        max_descriptions_per_cycle=int(
            _number(
                linkedin,
                "linkedin.max_descriptions_per_cycle",
                defaults.max_descriptions_per_cycle,
                minimum=0,
            )
        ),
        pause_between_descriptions=_range(
            linkedin, "linkedin.pause_between_descriptions", defaults.pause_between_descriptions
        ),
        exclude_locations=_strings(linkedin.get("exclude_locations") or [], "linkedin.exclude_locations"),
        exclude_companies=_strings(linkedin.get("exclude_companies") or [], "linkedin.exclude_companies"),
        show_match_reason=_boolean(notifications, "notifications.show_match_reason", defaults.show_match_reason),
        repost_window_days=int(
            _number(notifications, "notifications.repost_window_days", defaults.repost_window_days, minimum=0)
        ),
        keep_history_days=int(
            _number(notifications, "notifications.keep_history_days", defaults.keep_history_days, minimum=1)
        ),
    )
    if not config.locations:
        raise ConfigError("linkedin.locations needs at least one location")
    return config


# ── sections ────────────────────────────────────────────────────────────────


def _term_groups(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("term_groups must be a mapping of group name -> list of terms")
    return {str(name): _strings(terms, f"term_groups.{name}") for name, terms in raw.items()}


def _highlights(raw: Any, groups: dict[str, tuple[str, ...]]) -> tuple[Highlight, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ConfigError("highlights must be a mapping of tag name -> settings")
    highlights = []
    for name, spec in raw.items():
        where = f"highlights.{name}"
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a mapping")
        _only_keys(spec, where, {"title", "description", "employment"})
        highlights.append(
            Highlight(
                name=str(name),
                title=_group_ref(spec.get("title"), f"{where}.title", groups),
                description=_group_ref(spec.get("description"), f"{where}.description", groups),
                employment=_strings(spec.get("employment") or [], f"{where}.employment"),
            )
        )
    return tuple(highlights)


def _tracks(raw: Any, groups: dict[str, tuple[str, ...]], known_flags: set[str]) -> tuple[Track, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("tracks must be a non-empty list")
    tracks: list[Track] = []
    for index, spec in enumerate(raw):
        if not isinstance(spec, dict) or not spec.get("id"):
            raise ConfigError(f"tracks[{index}] must be a mapping with an id")
        where = f"track '{spec['id']}'"
        _only_keys(spec, where, {"id", "label", "emoji", "star", "search_terms", "exclude_title", "rules"})
        if any(track.id == str(spec["id"]) for track in tracks):
            raise ConfigError(f"{where}: this id is used twice")
        search_terms = _strings(spec.get("search_terms") or [], f"{where}.search_terms")
        if not search_terms:
            raise ConfigError(f"{where} needs at least one search term")
        star = _strings(spec.get("star") or [], f"{where}.star")
        for flag in star:
            if flag not in known_flags:
                raise ConfigError(f"{where}.star: '{flag}' is not defined under highlights")
        rules_raw = spec.get("rules")
        if not isinstance(rules_raw, list) or not rules_raw:
            raise ConfigError(f"{where} needs at least one rule")
        tracks.append(
            Track(
                id=str(spec["id"]),
                label=str(spec.get("label") or spec["id"]),
                emoji=str(spec.get("emoji") or "💼"),
                search_terms=search_terms,
                exclude_title=_group_refs(spec.get("exclude_title"), f"{where}.exclude_title", groups),
                rules=tuple(_rule(rule, f"{where}, rule {n + 1}", groups) for n, rule in enumerate(rules_raw)),
                star=star,
            )
        )
    return tuple(tracks)


def _rule(raw: Any, where: str, groups: dict[str, tuple[str, ...]]) -> Rule:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    _only_keys(
        raw,
        where,
        {
            "name",
            "title",
            "title_not",
            "description",
            "min_description_hits",
            "or_employment",
            "without_description",
        },
    )
    title = _group_refs(raw.get("title"), f"{where}.title", groups)
    if not title:
        raise ConfigError(f"{where} needs `title` with at least one term group")
    without_description = str(raw.get("without_description", "reject"))
    if without_description not in ("accept", "reject"):
        raise ConfigError(f"{where}.without_description must be 'accept' or 'reject'")
    return Rule(
        name=str(raw.get("name") or " + ".join(title)),
        title=title,
        title_not=_group_refs(raw.get("title_not"), f"{where}.title_not", groups),
        description=_group_ref(raw.get("description"), f"{where}.description", groups),
        min_description_hits=int(_number(raw, f"{where}.min_description_hits", 1, minimum=1)),
        or_employment=_strings(raw.get("or_employment") or [], f"{where}.or_employment"),
        without_description=without_description,
    )


# ── small validators ────────────────────────────────────────────────────────


def _section(data: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{name} must be a mapping")
    _only_keys(section, name, allowed)
    return section


def _only_keys(mapping: dict[str, Any], where: str, allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown setting {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _strings(raw: Any, where: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list")
    values = tuple(str(item).strip() for item in raw if item is not None)
    if any(not value for value in values):
        raise ConfigError(f"{where} contains an empty entry")
    return values


def _group_refs(raw: Any, where: str, groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    names = _strings(raw or [], where)
    for name in names:
        if name not in groups:
            raise ConfigError(f"{where}: '{name}' is not defined under term_groups")
    return names


def _group_ref(raw: Any, where: str, groups: dict[str, tuple[str, ...]]) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError(f"{where} must be the name of one term group")
    return _group_refs([raw], where, groups)[0]


def _number(mapping: dict[str, Any], where: str, default: float, *, minimum: float) -> float:
    value = mapping.get(where.rsplit(".", 1)[-1], default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number")
    if value < minimum:
        raise ConfigError(f"{where} must be at least {minimum}")
    return value


def _boolean(mapping: dict[str, Any], where: str, default: bool) -> bool:
    value = mapping.get(where.rsplit(".", 1)[-1], default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false")
    return value


def _range(mapping: dict[str, Any], where: str, default: tuple[float, float]) -> tuple[float, float]:
    value = mapping.get(where.rsplit(".", 1)[-1], list(default))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value, value]
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)
        or value[0] < 0
        or value[1] < value[0]
    ):
        raise ConfigError(f"{where} must be [min, max] seconds, e.g. [4, 9]")
    return float(value[0]), float(value[1])
