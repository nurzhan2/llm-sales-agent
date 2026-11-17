"""Хранилище правок каталога: протокол + InMemory + SqlAlchemy.

Тот же приём, что у `OpsStore`, `TouchStore` и `DialogStore`. Логика правок
(`CatalogEditor` ниже) не знает, где лежат данные, поэтому проверяется без
поднятого Postgres — а SQL-реализация отдельно проверяется на настоящем.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.kb.overrides import Override

logger = logging.getLogger("parmangal.kb.overrides")


@dataclass(frozen=True)
class OverrideRecord:
    """Строка журнала — то, что видно в /admin/catalog и в боте."""

    id: int
    path: str
    value: Any
    previous_value: Any
    field_key: str | None
    zone_id: str | None
    changed_by: int
    comment: str | None
    created_at: datetime | None
    reverted_at: datetime | None = None
    reverted_by: int | None = None

    @property
    def is_active(self) -> bool:
        return self.reverted_at is None


class OverrideStore(Protocol):
    async def list_active(self) -> list[OverrideRecord]: ...
    async def list_journal(self, limit: int = 100) -> list[OverrideRecord]: ...
    async def add(
        self,
        *,
        path: str,
        value: Any,
        previous_value: Any,
        field_key: str | None,
        zone_id: str | None,
        changed_by: int,
        comment: str | None = None,
    ) -> OverrideRecord: ...
    async def revert(self, override_id: int, reverted_by: int) -> OverrideRecord | None: ...
    async def last_active(self) -> OverrideRecord | None: ...


@dataclass
class InMemoryOverrideStore:
    rows: list[OverrideRecord] = field(default_factory=list)
    _next_id: int = 1

    async def list_active(self) -> list[OverrideRecord]:
        return [r for r in self.rows if r.is_active]

    async def list_journal(self, limit: int = 100) -> list[OverrideRecord]:
        return sorted(self.rows, key=lambda r: r.id, reverse=True)[:limit]

    async def add(
        self,
        *,
        path: str,
        value: Any,
        previous_value: Any,
        field_key: str | None,
        zone_id: str | None,
        changed_by: int,
        comment: str | None = None,
    ) -> OverrideRecord:
        record = OverrideRecord(
            id=self._next_id,
            path=path,
            value=value,
            previous_value=previous_value,
            field_key=field_key,
            zone_id=zone_id,
            changed_by=changed_by,
            comment=comment,
            created_at=datetime.now(UTC),
        )
        self._next_id += 1
        self.rows.append(record)
        return record

    async def revert(self, override_id: int, reverted_by: int) -> OverrideRecord | None:
        for i, row in enumerate(self.rows):
            if row.id == override_id and row.is_active:
                reverted = OverrideRecord(
                    **{**row.__dict__, "reverted_at": datetime.now(UTC), "reverted_by": reverted_by}
                )
                self.rows[i] = reverted
                return reverted
        return None

    async def last_active(self) -> OverrideRecord | None:
        active = await self.list_active()
        return max(active, key=lambda r: r.id) if active else None


def _to_record(row) -> OverrideRecord:
    return OverrideRecord(
        id=row.id,
        path=row.path,
        value=row.value,
        previous_value=row.previous_value,
        field_key=row.field_key,
        zone_id=row.zone_id,
        changed_by=row.changed_by,
        comment=row.comment,
        created_at=row.created_at,
        reverted_at=row.reverted_at,
        reverted_by=row.reverted_by,
    )


class SqlAlchemyOverrideStore:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def list_active(self) -> list[OverrideRecord]:
        """По возрастанию id — порядок наложения обязан быть хронологическим:
        последняя правка того же пути должна побеждать."""
        from sqlalchemy import select

        from app.db.models import CatalogOverride

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(CatalogOverride)
                        .where(CatalogOverride.reverted_at.is_(None))
                        .order_by(CatalogOverride.id)
                    )
                )
                .scalars()
                .all()
            )
        return [_to_record(r) for r in rows]

    async def list_journal(self, limit: int = 100) -> list[OverrideRecord]:
        from sqlalchemy import select

        from app.db.models import CatalogOverride

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(CatalogOverride).order_by(CatalogOverride.id.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_to_record(r) for r in rows]

    async def add(
        self,
        *,
        path: str,
        value: Any,
        previous_value: Any,
        field_key: str | None,
        zone_id: str | None,
        changed_by: int,
        comment: str | None = None,
    ) -> OverrideRecord:
        from app.db.models import CatalogOverride

        async with self._session_factory() as session:
            row = CatalogOverride(
                path=path,
                value=value,
                previous_value=previous_value,
                field_key=field_key,
                zone_id=zone_id,
                changed_by=changed_by,
                comment=comment,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_record(row)

    async def revert(self, override_id: int, reverted_by: int) -> OverrideRecord | None:
        from sqlalchemy import select

        from app.db.models import CatalogOverride

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(CatalogOverride).where(CatalogOverride.id == override_id)
                )
            ).scalar_one_or_none()
            if row is None or row.reverted_at is not None:
                return None
            row.reverted_at = datetime.now(UTC)
            row.reverted_by = reverted_by
            await session.commit()
            await session.refresh(row)
            return _to_record(row)

    async def last_active(self) -> OverrideRecord | None:
        from sqlalchemy import select

        from app.db.models import CatalogOverride

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(CatalogOverride)
                    .where(CatalogOverride.reverted_at.is_(None))
                    .order_by(CatalogOverride.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return _to_record(row) if row is not None else None


def to_overrides(records: list[OverrideRecord]) -> list[Override]:
    return [Override(path=r.path, value=r.value) for r in records]
