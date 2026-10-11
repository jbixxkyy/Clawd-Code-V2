"""Session management with persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field

from .conversation import Conversation


@dataclass
class Session:
    """Session manager with persistence."""
    session_id: str
    provider: str
    model: str
    conversation: Conversation = field(default_factory=Conversation)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    workspace: str | None = None
    # Browser-visible tool/text activity for each finished turn, keyed by the
    # conversation message index where the turn started. The web UI replays
    # these so tool cards survive leaving the chat or reloading the page.
    turn_timelines: list[dict] = field(default_factory=list)

    def save(self):
        """Save session to disk."""
        session_dir = Path.home() / ".clawd" / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)

        session_file = session_dir / f"{self.session_id}.json"

        session_data = {
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "conversation": self.conversation.to_dict(),
            "created_at": self.created_at,
            "updated_at": datetime.now().isoformat(),
            "workspace": self.workspace,
            "turn_timelines": self.turn_timelines,
        }

        # Write-then-replace so an interrupted save never leaves a truncated file.
        temp_file = session_file.with_suffix(".json.tmp")
        with open(temp_file, 'w') as f:
            json.dump(session_data, f, indent=2)
        os.replace(temp_file, session_file)

        self.updated_at = datetime.now().isoformat()

    @classmethod
    def load(cls, session_id: str) -> Optional['Session']:
        """Load session from disk."""
        session_file = Path.home() / ".clawd" / "sessions" / f"{session_id}.json"

        if not session_file.exists():
            return None

        with open(session_file, 'r') as f:
            data = json.load(f)

        return cls(
            session_id=data["session_id"],
            provider=data["provider"],
            model=data["model"],
            conversation=Conversation.from_dict(data["conversation"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            workspace=data.get("workspace"),
            turn_timelines=[
                turn for turn in data.get("turn_timelines", []) if isinstance(turn, dict)
            ],
        )

    @classmethod
    def create(cls, provider: str, model: str) -> 'Session':
        """Create a new session."""
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        return cls(
            session_id=session_id,
            provider=provider,
            model=model
        )
