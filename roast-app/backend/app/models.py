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
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DATABASE_URL


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
    bindings: Mapped[list["PlanBinding"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
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
    """A roasting *plan* (recipe template family).

    A plan is edited only by appending immutable :class:`RoastPlanVersion`
    rows (draft -> confirmed -> retired).  Batch curves are never compared to
    "the current plan text"; they are bound to one concrete version, and an
    export carries a snapshot of that version so historical judgments remain
    reproducible after the plan evolves.
    """

    __tablename__ = "roast_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    bean: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="roast-lead")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    versions: Mapped[list["RoastPlanVersion"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="RoastPlanVersion.version_no",
    )


class RoastPlanVersion(Base):
    """One immutable revision of a plan.

    ``content`` is the versioned, never-mutated document: anchor-relative
    target segments and tolerance bands plus the evaluation parameters.
    Only ``status`` may move (draft -> confirmed -> retired); a confirmed
    version is frozen content-wise for as long as any binding may reference
    it.  ``content_sha256`` fingerprints the exact JSON used by a judgment, so
    an export snapshot can be checked against the stored row.
    """

    __tablename__ = "roast_plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version_no", name="uq_plan_version_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("roast_plans.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # draft | confirmed | retired
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="roast-lead")
    # Optimistic-concurrency token: client edits must state the version_no they
    # edited from; if it is no longer the plan HEAD, the submit is rejected.
    base_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    plan: Mapped[RoastPlan] = relationship(back_populates="versions")
    bindings: Mapped[list["PlanBinding"]] = relationship(
        back_populates="plan_version"
    )


class PlanBinding(Base):
    """Binding of a batch to one *confirmed* immutable plan version.

    Rebinding appends a new row and supersedes the previous binding instead of
    editing it.  The version id is recorded directly (not just plan + number)
    so the link stays intact even if versions are later retired.
    """

    __tablename__ = "plan_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("roast_plans.id"))
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("roast_plan_versions.id"))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_bindings.id"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="bindings")
    plan_version: Mapped[RoastPlanVersion] = relationship(back_populates="bindings")
    evaluations: Mapped[list["PlanEvaluation"]] = relationship(
        back_populates="binding", cascade="all, delete-orphan",
        order_by="PlanEvaluation.id",
    )


class PlanEvaluation(Base):
    """A stored deviation conclusion for a binding.

    Append-only: when an anchor event is manually corrected the old row keeps
    review_state='obsolete' (its JSON result and evidence remain readable) and
    a fresh row carries the new judgment.  Nothing here is ever overwritten.
    """

    __tablename__ = "plan_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("plan_bindings.id"), index=True)
    # current | needs_review | obsolete
    review_state: Mapped[str] = mapped_column(String(20), default="current", index=True)
    # Full structured result from plan_eval.evaluate_plan (segments, verdicts,
    # unevaluable reasons, anchors, parameters, disclaimers).
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Anchor-event fingerprint the judgment was based on; a mismatch after an
    # event correction is what flips review_state to needs_review.
    anchor_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    binding: Mapped[PlanBinding] = relationship(back_populates="evaluations")


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
