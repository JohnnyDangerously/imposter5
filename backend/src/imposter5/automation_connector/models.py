"""Request models for automation connector routes."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from imposter5.automation_connector.platforms import DEFAULT_AUTOMATION_URL


class AutomationConnectorSessionRequest(BaseModel):
    """Request body for starting, updating, or verifying a browser session."""

    website_url: str = Field(default=DEFAULT_AUTOMATION_URL, max_length=500)
    status: str = Field(default="pending", max_length=40)
    mode: str = Field(default="interactive", max_length=40)
    session_blob: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=500)


class AutomationConnectorTargetRequest(BaseModel):
    """Request body for adding a website observation target."""

    website_url: str = Field(default=DEFAULT_AUTOMATION_URL, max_length=500)
    label: str | None = Field(default=None, max_length=160)
    payload_prompt: str | None = Field(default=None, max_length=1000)
    check_interval_minutes: int = Field(default=60, ge=5, le=1440)
    # Custom variation guide (a.k.a. "variation guides") for activity mix on static/high-volume paths
    # like the LinkedIn experiment. Lets you specify which micro-behaviors and chances to enable
    # (profile peeks, notifications, bidirectional scrolls, comment expands, etc.) without using
    # the full natural-language prompt interpreter (that's for the general/agent control plane).
    # Example: {"profile_peeks": true, "profile_peek_chance": 0.3, "notifications_check": true, "max_side_actions": 2, "bidirectional_scroll": true}
    variation_guide: dict[str, Any] | None = Field(default=None)


class AutomationConnectorTargetStateRequest(BaseModel):
    """Request body for enabling or disabling a website observation target."""

    enabled: bool = True


class Imposter5RunRequest(BaseModel):
    """Request body for launching an Imposter5 red team simulation."""

    url: str = Field(default="https://en.wikipedia.org/wiki/Artificial_intelligence", max_length=500)
    provider: str = Field(default="generic", max_length=40)
    prompt: str | None = Field(default=None, max_length=1000)
    persona: str | None = Field(default=None, max_length=100)
    completion: str | None = Field(default=None, max_length=100)
    variations: dict[str, bool] | None = None
    human_config: dict[str, Any] | None = None
    run_fp_agent: bool = False
    # When set, a green first run enrolls this task on a recurring schedule at
    # this cadence (minutes). None = run once, do not schedule.
    schedule_interval_minutes: int | None = Field(default=None, ge=5, le=1440)

