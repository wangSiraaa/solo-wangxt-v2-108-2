"""SQLAlchemy models: batches, *raw* samples, and sourced events.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A manual correction supersedes the previous row instead of deleting it, so
  every value keeps its ``source`` / ``created_by`` provenance.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DATABASE_URL

# Plan lifecycle states.  Confirmed versions are immutable: editing a bound
# plan always mints a NEW row/version, it never rewrites history.
PLAN_DRAFT = "draft"
PLAN_CONFIRMED = "confirmed"
PLAN_RETIRED = "retired"
PLAN_STATES = (PLAN_DRAFT, PLAN_CONFIRMED, PLAN_RETIRED)

# Assessment review states.  Judgement rows are append-only: correcting an
# anchor marks the current row ``needs_review`` instead of deleting it, and
# recomputing marks the old row ``superseded``; every past judgement plus its
# basis stays queryable.
ASSESSMENT_CURRENT = "current"
ASSESSMENT_NEEDS_REVIEW = "needs_review"
ASSESSMENT_SUPERSEDED = "superseded"


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    roaster: Mapped[str] = mapped_column(String(120), default="synthetic")
    bean: Mapped[str] = mapped_column(String(120), default="")
    charge_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    charge_temp_c: Mapped[float] = mapped_column(Float)
    ambient_temp_c: Mapped[float] = mapped_column(Float)
    target_drop_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )


class Sample(Base):
    """One raw probe reading.  Temperatures are NULL when the probe was
    briefly lost — the missing reading is preserved as missing, not invented."""

    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("batch_id", "t_s", name="uq_sample_batch_t"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Seconds since charge.  Intervals are intentionally uneven.
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bean_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    env_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # turning_point | first_crack_start | first_crack_end | drop |
    # damper_change | charge | custom
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    # auto = detected from the raw series; manual = operator entry.
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    # Numeric payload, e.g. new damper position (%) for damper_change.
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="events")


class RoastPlan(Base):
    """A named, versioned roast target curve (a "烘焙方案").

    A plan is a chain of :class:`RoastPlanVersion` rows.  Only one version per
    chain is confirmed at a time; confirming a new version retires the old one.
    The plan identity never moves — batches keep pointing at the exact version
    they were judged against even after newer versions appear."""

    __tablename__ = "roast_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="roast-lead")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    versions: Mapped[list["RoastPlanVersion"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="RoastPlanVersion.version_no",
    )


class RoastPlanVersion(Base):
    """One immutable revision of a plan: anchor-relative target segments.

    Rows are mutated ONLY through the explicit lifecycle transitions
    draft -> confirmed -> retired (plus revision increments inside a draft).
    Once confirmed, ``definition_json`` / ``content_hash`` never change, and no
    service path updates them.  ``based_on_version_id`` carries the provenance
    chain used for optimistic-conflict detection between two editors."""

    __tablename__ = "roast_plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version_no", name="uq_plan_version_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("roast_plans.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Canonical definition: {"segments": [...], "anchor_schedule": {...}}.
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    based_on_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("roast_plan_versions.id"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="roast-lead")
    # Bumped on every accepted draft edit; clients send it for conflict checks.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    plan: Mapped[RoastPlan] = relationship(back_populates="versions")


class BatchPlanBinding(Base):
    """Which (immutable, confirmed) plan version a batch is judged against.

    The binding is immutable: there is no update/delete endpoint.  Rebinding a
    batch to a newer confirmed version inserts a NEW row, and only the latest
    binding is current — the superseded row keeps the old judgement attached so
    the historical decision (and its export) remains reproducible."""

    __tablename__ = "batch_plan_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("roast_plan_versions.id"), index=True
    )
    bound_by: Mapped[str] = mapped_column(String(80), default="operator")
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlanAssessment(Base):
    """Persistent deviation conclusion for one batch/version at one point in
    time.  Append-only: corrections mark the row ``needs_review`` (retaining
    the old verdict + basis) and recomputing marks it ``superseded``."""

    __tablename__ = "plan_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[int] = mapped_column(
        ForeignKey("batch_plan_bindings.id"), index=True
    )
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("roast_plan_versions.id"), index=True
    )
    # current | needs_review | superseded
    review_state: Mapped[str] = mapped_column(String(20), default=ASSESSMENT_CURRENT)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_gap_fill_s: Mapped[float] = mapped_column(Float, nullable=False)
    assessed_by: Mapped[str] = mapped_column(String(80), default="system")
    # Anchor events used by a stale judgement, captured when a correction lands.
    needs_review_reason: Mapped[str] = mapped_column(Text, default="")
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_assessments.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
