"""The teams settings: schema and validation (design §3.4).

One JSON file, named by `PMO_SKILL_CONFIG` (design §3.4: missing → refuse;
never a guessed path). Shape:

    {
      "version": 1,
      "pmo_identity": "<the PMO's AI name>",
      "secretary_identity": "<the secretary's AI name>",
      "dispatch_channel": "<the channel dispatch messages arrive on>",
      "pmo_sidecar": "<http(s) address>",      optional; absent = the PMO is
                                                not in a sidecar
      "wake_text": "<the poke job's fixed sentence>",
                                                optional; default below
      "teams": [<team record>, ...]
    }

Team record:

    {
      "key": "<project>-<team code>",
      "pm": "<the team PM's AI name>",
      "channel": "team-<project>-<team code>",
      "sidecar": "<http(s) address>" | "worker",
      "tui": <bool>, "life": <bool>, "retired": <bool>,
      "provider": "<…>", "model": "<…>"        optional, as a pair
    }

Validation (design §3.4 “驗證規則照現行工具：AI 名用 ainame 驗、key、名字、
channel 一致、不重複”): AI names via `ainame`; the channel is `team-` plus the
key; the key and the PM name agree (`key` is the PM name without its role);
keys, PM names and channels do not repeat; the dispatch channel is not any
team channel. Unknown fields are preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ainame import is_valid, parse as parse_name

from . import _io, _jsonutil
from ._jsonutil import describe, fail, pick_extra

WHERE = "teams settings"

#: The poke job's fixed sentence when the settings do not name one.
DEFAULT_WAKE_TEXT = "定時叫醒：請跑 pmo tick"

# A channel name and a team key share the name grammar's outer rules: lowercase
# letters, digits and '-', not empty, no '-' at either end, no '--'.
_SLUG_RE = re.compile(r"[a-z0-9-]+")

_KNOWN = ("version", "pmo_identity", "secretary_identity", "dispatch_channel", "pmo_sidecar", "wake_text", "teams")
_KNOWN_TEAM = ("key", "pm", "channel", "sidecar", "tui", "life", "retired", "provider", "model")


def _check_slug(value: str, where: str, label: str) -> None:
    if (
        not value
        or not _SLUG_RE.fullmatch(value)
        or value.startswith("-")
        or value.endswith("-")
        or "--" in value
    ):
        fail(where, f"the field {label!r} is not a lowercase slug of a-z, 0-9 and '-' ({describe(value)})")


def _check_ai_name(value: str, where: str, label: str) -> None:
    if not is_valid(value):
        fail(where, f"the field {label!r} is not a valid AI name ({describe(value)})")


def _check_address(value: str, where: str, label: str, *, allow_worker: bool) -> None:
    if allow_worker and value == "worker":
        return
    if not (value.startswith("http://") or value.startswith("https://")) or value in ("http://", "https://"):
        fail(
            where,
            f"the field {label!r} is neither "
            + ("an http(s) address nor 'worker'" if allow_worker else "an http(s) address")
            + f" ({describe(value)})",
        )


@dataclass(frozen=True)
class Team:
    """One team's record in the teams settings."""

    key: str
    pm: str
    channel: str
    sidecar: str
    tui: bool
    life: bool
    retired: bool
    provider: str | None = None
    model: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known = {
            "key": self.key,
            "pm": self.pm,
            "channel": self.channel,
            "sidecar": self.sidecar,
            "tui": self.tui,
            "life": self.life,
            "retired": self.retired,
        }
        if self.provider is not None:
            known["provider"] = self.provider
            known["model"] = self.model
        known.update(self.extra)
        return known


@dataclass(frozen=True)
class TeamsConfig:
    """The whole teams settings file, taken apart."""

    pmo_identity: str
    secretary_identity: str
    dispatch_channel: str
    pmo_sidecar: str | None = None
    wake_text: str = DEFAULT_WAKE_TEXT
    teams: tuple[Team, ...] = ()
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def team(self, key: str) -> Team | None:
        """The team record with that key, if any."""
        for team in self.teams:
            if team.key == key:
                return team
        return None

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {
            "version": 1,
            "pmo_identity": self.pmo_identity,
            "secretary_identity": self.secretary_identity,
            "dispatch_channel": self.dispatch_channel,
            "teams": [team.to_dict() for team in self.teams],
        }
        if self.pmo_sidecar is not None:
            known["pmo_sidecar"] = self.pmo_sidecar
        known["wake_text"] = self.wake_text
        known.update(self.extra)
        return known


def _parse_team(raw: object, where: str) -> Team:
    _jsonutil.check_object(raw, where)
    assert isinstance(raw, dict)  # check_object guarantees it
    key = _jsonutil.check_str(raw, "key", where)
    _check_slug(key, where, "key")
    pm = _jsonutil.check_str(raw, "pm", where)
    _check_ai_name(pm, where, "pm")
    channel = _jsonutil.check_str(raw, "channel", where)
    _check_slug(channel, where, "channel")
    sidecar = _jsonutil.check_str(raw, "sidecar", where)
    _check_address(sidecar, where, "sidecar", allow_worker=True)
    tui = _jsonutil.check_bool(raw, "tui", where)
    life = _jsonutil.check_bool(raw, "life", where)
    retired = _jsonutil.check_bool(raw, "retired", where)
    provider = _jsonutil.optional_str(raw, "provider", where)
    model = _jsonutil.optional_str(raw, "model", where)
    if (provider is None) != (model is None):
        fail(where, "the fields 'provider' and 'model' go together; give both or neither")
    if provider is not None and not provider.strip():
        fail(where, "the field 'provider' is empty")
    if model is not None and not model.strip():
        fail(where, "the field 'model' is empty")

    # Key, name and channel agree: both come from the PM's AI name, and the
    # grammar that derives them lives in `ainame` (D-3).
    if "-" not in key:
        fail(where, f"the field 'key' must be '<project>-<team code>' ({describe(key)})")
    name = parse_name(pm)
    if key != name.team_key():
        fail(
            where,
            f"the key, the PM name and the channel do not agree: 'pm' has project {name.project!r} "
            f"and team {name.team!r}, so 'key' would be {name.team_key()!r}, not {key!r}",
        )
    if channel != name.channel():
        fail(where, f"the field 'channel' must be {name.channel()!r}, got {describe(channel)}")

    return Team(
        key=key,
        pm=pm,
        channel=channel,
        sidecar=sidecar,
        tui=tui,
        life=life,
        retired=retired,
        provider=provider,
        model=model,
        extra=pick_extra(raw, _KNOWN_TEAM),
    )


def parse(data: object) -> TeamsConfig:
    """Validate one teams settings file's content and take it apart."""
    _jsonutil.check_object(data, WHERE)
    assert isinstance(data, dict)
    _jsonutil.check_version(data, WHERE)
    pmo_identity = _jsonutil.check_str(data, "pmo_identity", WHERE)
    _check_ai_name(pmo_identity, WHERE, "pmo_identity")
    secretary_identity = _jsonutil.check_str(data, "secretary_identity", WHERE)
    _check_ai_name(secretary_identity, WHERE, "secretary_identity")
    dispatch_channel = _jsonutil.check_str(data, "dispatch_channel", WHERE)
    _check_slug(dispatch_channel, WHERE, "dispatch_channel")

    pmo_sidecar = _jsonutil.optional_str(data, "pmo_sidecar", WHERE)
    if pmo_sidecar is not None:
        _check_address(pmo_sidecar, WHERE, "pmo_sidecar", allow_worker=False)
    wake_text = _jsonutil.optional_str(data, "wake_text", WHERE, default=DEFAULT_WAKE_TEXT)
    if not wake_text.strip():
        fail(WHERE, "the field 'wake_text' is empty")

    teams = _jsonutil.need(data, "teams", WHERE)
    if not isinstance(teams, list):
        fail(WHERE, f"the field 'teams' must be a list, got {describe(teams)}")
    team_records = tuple(
        _parse_team(member, f"{WHERE}: teams[{index}]") for index, member in enumerate(teams)
    )

    # The key is derived from the PM name and the channel from the key (above),
    # so a repeated key is the only way a team name or channel can repeat; the
    # channel set stays because the dispatch channel must not be a team channel.
    keys: set[str] = set()
    channels: set[str] = set()
    for team in team_records:
        if team.key in keys:
            fail(WHERE, f"the team key {team.key!r} appears twice")
        keys.add(team.key)
        channels.add(team.channel)
    if dispatch_channel in channels:
        fail(WHERE, f"the dispatch channel {dispatch_channel!r} is also a team channel")

    return TeamsConfig(
        pmo_identity=pmo_identity,
        secretary_identity=secretary_identity,
        dispatch_channel=dispatch_channel,
        pmo_sidecar=pmo_sidecar,
        wake_text=wake_text,
        teams=team_records,
        extra=pick_extra(data, _KNOWN),
    )


def load(path: Path) -> TeamsConfig:
    """Read and validate the teams settings file."""
    return parse(_io.read_json(path))


def save(path: Path, config: TeamsConfig) -> None:
    """Write the canonical form of the settings (same directory, os.replace)."""
    _io.write_json(path, config.to_dict())
