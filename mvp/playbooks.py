from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Playbook:
    playbook_id: str
    vertical: str
    geo: Dict[str, Any]
    risk: Dict[str, Any]
    ops: Dict[str, Any]
    fallback: Dict[str, Any]


_lock = threading.Lock()
_cache: Optional[List[Playbook]] = None


def _default_playbooks_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "playbooks")


def load_playbooks() -> List[Playbook]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache

        playbooks: List[Playbook] = []
        directory = os.getenv("MVP_PLAYBOOKS_DIR", _default_playbooks_dir())
        try:
            entries = sorted(os.listdir(directory))
        except Exception:
            entries = []

        for name in entries:
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except Exception:
                continue
            try:
                playbooks.append(
                    Playbook(
                        playbook_id=str(obj.get("playbook_id") or name),
                        vertical=str(obj.get("vertical") or "unknown"),
                        geo=dict(obj.get("geo") or {}),
                        risk=dict(obj.get("risk") or {}),
                        ops=dict(obj.get("ops") or {}),
                        fallback=dict(obj.get("fallback") or {}),
                    )
                )
            except Exception:
                continue

        _cache = playbooks
        return playbooks


def resolve_playbook(*, country: Optional[str], vertical: Optional[str] = None) -> Optional[Playbook]:
    """
    Resolve a playbook by geo (and optionally vertical). Returns the first match.
    """
    if not country:
        return None
    country = str(country).upper()[:2]
    desired = (vertical or "").lower() if vertical else None

    # Explicit override by env for controlled rollout.
    override = os.getenv("MVP_PLAYBOOK_ID")
    if override:
        for pb in load_playbooks():
            if pb.playbook_id == override:
                return pb

    for pb in load_playbooks():
        pb_country = str((pb.geo or {}).get("country") or "").upper()[:2]
        if pb_country != country:
            continue
        if desired and str(pb.vertical or "").lower() != desired:
            continue
        return pb
    return None


def risk_thresholds_for_geo(*, country: Optional[str]) -> Dict[str, Any]:
    pb = resolve_playbook(country=country)
    return dict(pb.risk) if pb else {}


def ops_config_for_geo(*, country: Optional[str]) -> Dict[str, Any]:
    pb = resolve_playbook(country=country)
    return dict(pb.ops) if pb else {}


def fallback_config_for_geo(*, country: Optional[str]) -> Dict[str, Any]:
    pb = resolve_playbook(country=country)
    return dict(pb.fallback) if pb else {}
