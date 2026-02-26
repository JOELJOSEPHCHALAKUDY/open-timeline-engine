from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session
from tce_shared.events import OperationMode, RuntimeModeConfig

from .config import get_settings
from .models import RuntimeSetting

MODE_KEY = "runtime_mode"


def _default_mode_payload() -> dict:
    settings = get_settings()
    mode = settings.default_operation_mode
    if mode not in {OperationMode.TIMELINE_ONLY.value, OperationMode.CLONE_ADVISOR.value}:
        mode = OperationMode.TIMELINE_ONLY.value
    return {"mode": mode, "clone_enabled": mode == OperationMode.CLONE_ADVISOR.value}


def get_runtime_mode(db: Session) -> RuntimeModeConfig:
    row = db.get(RuntimeSetting, MODE_KEY)
    if row is None:
        row = RuntimeSetting(
            key=MODE_KEY,
            value=_default_mode_payload(),
            updated_at=datetime.now(tz=UTC),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    stored_mode = str(row.value.get("mode", OperationMode.TIMELINE_ONLY.value))
    if stored_mode not in {OperationMode.TIMELINE_ONLY.value, OperationMode.CLONE_ADVISOR.value}:
        stored_mode = OperationMode.TIMELINE_ONLY.value
    mode = OperationMode(stored_mode)
    clone_enabled = bool(row.value.get("clone_enabled", mode == OperationMode.CLONE_ADVISOR))
    return RuntimeModeConfig(
        mode=mode,
        clone_enabled=clone_enabled,
        updated_at=row.updated_at,
        updated_by=str(row.value.get("updated_by", "system")),
    )


def set_runtime_mode(db: Session, mode: OperationMode, updated_by: str) -> RuntimeModeConfig:
    row = db.get(RuntimeSetting, MODE_KEY)
    if row is None:
        row = RuntimeSetting(key=MODE_KEY, value={}, updated_at=datetime.now(tz=UTC))
        db.add(row)

    row.value = {
        "mode": mode.value,
        "clone_enabled": mode == OperationMode.CLONE_ADVISOR,
        "updated_by": updated_by,
    }
    row.updated_at = datetime.now(tz=UTC)
    db.commit()
    db.refresh(row)
    return RuntimeModeConfig(
        mode=mode,
        clone_enabled=bool(row.value["clone_enabled"]),
        updated_at=row.updated_at,
        updated_by=updated_by,
    )
