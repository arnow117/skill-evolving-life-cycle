"""Durable, privacy-minimised host observations and deterministic projection.

This module is intentionally an internal API.  A future host adapter translates
native hook data into the v1 DTO defined by :func:`ingest`; business CLIs do not
accept this DTO and cannot claim a complete one-shot result with a flag.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from . import records
from .errors import SelcError
from .util import json_hash


OBSERVATION_PROTOCOL = 1
CLASSIFICATION_VERSION = "observation-v1"
MAX_ENVELOPE_BYTES = 32_768
MAX_FACT_VALUE_BYTES = 4_096

EVENT_KINDS = {
    "task_started",
    "call_started",
    "input_ready",
    "permission_granted",
    "automatic_retry",
    "business_intervention",
    "delivery_published",
    "call_completed",
    "feedback",
    "task_interrupted",
    "task_closed",
}
PHASES = {"pre_delivery", "post_delivery", "lifecycle"}
ACTORS = {"human", "agent", "host", "unknown"}
ORIGINS = {"managed_semantic", "native_unverified", "unknown"}
CAUSATIONS = {
    "original_request",
    "input_preparation",
    "permission",
    "automatic_retry",
    "business_correction",
    "audit",
    "voluntary_feedback",
    "requirement_change",
    "unknown",
}
FACT_KEYS = {
    "task_id",
    "suite_id",
    "suite_version",
    "harness",
    "initiator",
    "call_id",
    "skill",
    "executor",
    "expected_version",
    "expected_hash",
    "trigger_kind",
    "delivery_id",
    "artifact_hash",
    "rule_version",
    "checker_id",
    "checker_result",
    "checker_input_hash",
    "checker_artifact_hash",
    "checker_resource_hash",
    "checker_composite_hash",
    "publication_ordinal",
    "predecessor_ordinal",
    "result",
    "feedback_id",
    "feedback_revision",
    "feedback_scope",
    "feedback_result",
    "feedback_body_hash",
    "actor",
    "origin",
    "causation",
    "delivery_role",
}
BODY_KEYS = {"body", "prompt", "content", "message", "complete", "complete_fixture"}


def ingest(database: sqlite3.Connection, envelope: dict[str, Any]) -> dict[str, Any]:
    """Persist an adapter-classified v1 observation, then reduce pending work.

    The first transaction stores only an allowlisted envelope, digest, and
    immutable classification facts.  Projection happens in a second transaction
    so a process failure cannot erase a received native observation.
    """

    normalized = _normalize_envelope(envelope)
    stored = _store_raw(database, normalized)
    if stored["status"] in {"conflict", "quarantined"}:
        _refresh_assessment_for_event(database, stored["event_id"])
        return stored
    replay_result = replay(database)
    return {**stored, "replay": replay_result}


def replay(database: sqlite3.Connection) -> dict[str, int]:
    """Apply persisted pending observations without invoking host-side actions."""

    applied = 0
    quarantined = 0
    while True:
        progressed = 0
        rows = database.execute(
            """SELECT * FROM observation_events
            WHERE status = 'pending' ORDER BY received_at, event_id"""
        ).fetchall()
        for row in rows:
            outcome = _reduce_one(database, row)
            if outcome == "applied":
                applied += 1
                progressed += 1
            elif outcome == "quarantined":
                quarantined += 1
        if progressed == 0:
            break
    pending = int(
        database.execute("SELECT COUNT(*) FROM observation_events WHERE status = 'pending'").fetchone()[0]
    )
    _refresh_assessments_with_durable_gaps(database)
    return {"applied": applied, "pending": pending, "quarantined": quarantined}


def timeline(database: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Return the stored minimum timeline for a task, including pending gaps."""

    rows = database.execute(
        """SELECT event_id, kind, occurred_at, received_at, status, reason,
                  stable_identity, source_context, source_epoch, facts_json
           FROM observation_events
           WHERE task_id = ? ORDER BY received_at, event_id""",
        (task_id,),
    ).fetchall()
    timeline_rows = []
    for row in rows:
        value = dict(row)
        value["facts"] = json.loads(value.pop("facts_json"))
        timeline_rows.append(value)
    conflicts = database.execute(
        """SELECT conflict_id, stable_identity, original_event_id, incoming_digest,
                  received_at, reason
           FROM observation_conflicts
           WHERE original_event_id IN (
             SELECT event_id FROM observation_events WHERE task_id = ?
           ) ORDER BY conflict_id""",
        (task_id,),
    ).fetchall()
    timeline_rows.extend({"kind": "gap", "status": "conflict", **dict(row)} for row in conflicts)
    return timeline_rows


def assessment(database: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = database.execute(
        "SELECT * FROM observation_assessments WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {
            "task_id": task_id,
            "first_result": "unknown",
            "final_result": "unknown",
            "reason": "no_formal_assessment",
        }
    value = dict(row)
    value["evidence"] = json.loads(value.pop("evidence_json"))
    return value


def _normalize_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise SelcError("INVALID_OBSERVATION", "observation must be an object", "send adapter DTO v1")
    if len(_encoded(envelope)) > MAX_ENVELOPE_BYTES:
        raise SelcError(
            "OBSERVATION_TOO_LARGE",
            "observation exceeds the bounded envelope size",
            "retain only allowlisted identifiers and hashes",
        )
    _required_keys(
        envelope,
        {
            "protocol",
            "adapter",
            "identity",
            "source_context",
            "kind",
            "occurred_at",
            "classification",
        },
        "observation",
    )
    if envelope["protocol"] != OBSERVATION_PROTOCOL:
        raise SelcError(
            "UNSUPPORTED_OBSERVATION_PROTOCOL",
            "observation protocol is unsupported",
            "upgrade the adapter and records runtime together",
        )
    kind = _string(envelope["kind"], "kind")
    if kind not in EVENT_KINDS:
        raise SelcError("INVALID_OBSERVATION", "observation kind is unsupported", "use a v1 event kind")
    adapter = _object(envelope["adapter"], "adapter")
    _required_keys(adapter, {"name", "version"}, "adapter")
    identity = _object(envelope["identity"], "identity")
    _required_keys(
        identity,
        {"installation_id", "host", "surface", "session_id", "turn_id", "native_event_id", "phase"},
        "identity",
    )
    phase = _string(identity["phase"], "identity.phase")
    if phase not in PHASES:
        raise SelcError("INVALID_OBSERVATION", "identity phase is unsupported", "use a v1 phase")
    source_context = _object(envelope["source_context"], "source_context")
    _required_keys(source_context, {"id", "epoch"}, "source_context")
    epoch = source_context["epoch"]
    if type(epoch) is not int or epoch < 1:
        raise SelcError(
            "INVALID_OBSERVATION", "source context epoch must be a positive integer", "use adapter epoch"
        )
    classification = _object(envelope["classification"], "classification")
    _required_keys(classification, {"version", "facts", "evidence"}, "classification")
    if classification["version"] != CLASSIFICATION_VERSION:
        raise SelcError(
            "UNSUPPORTED_CLASSIFICATION",
            "classification version is unsupported",
            "retain existing facts or supply an explicit compatible revision",
        )
    facts = _normalize_facts(_object(classification["facts"], "classification.facts"))
    evidence = classification["evidence"]
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise SelcError(
            "INVALID_OBSERVATION", "classification evidence must be string references", "omit bodies"
        )
    if any(len(item.encode("utf-8")) > 256 for item in evidence):
        raise SelcError(
            "OBSERVATION_TOO_LARGE", "classification evidence is too large", "store a stable reference"
        )
    normalized_identity = {
        key: _nullable_string(value, f"identity.{key}")
        for key, value in identity.items()
        if key in {"installation_id", "host", "surface", "session_id", "turn_id", "native_event_id"}
    }
    for key in ("installation_id", "host", "surface"):
        if not normalized_identity.get(key):
            raise SelcError("INVALID_OBSERVATION", f"identity.{key} is required", "use native context")
    stable_identity = _stable_identity(normalized_identity, kind, phase)
    return {
        "protocol": OBSERVATION_PROTOCOL,
        "adapter_name": _string(adapter["name"], "adapter.name"),
        "adapter_version": _string(adapter["version"], "adapter.version"),
        "identity": normalized_identity,
        "stable_identity": stable_identity,
        "phase": phase,
        "source_context": _string(source_context["id"], "source_context.id"),
        "source_epoch": epoch,
        "kind": kind,
        "occurred_at": _string(envelope["occurred_at"], "occurred_at"),
        "classification_version": CLASSIFICATION_VERSION,
        "facts": facts,
        "evidence": evidence,
    }


def _normalize_facts(facts: dict[str, Any]) -> dict[str, Any]:
    unsupported = set(facts).difference(FACT_KEYS)
    prohibited = set(facts).intersection(BODY_KEYS)
    if unsupported or prohibited:
        names = ", ".join(sorted(unsupported | prohibited))
        raise SelcError(
            "INVALID_OBSERVATION",
            f"observation facts contain unsupported fields: {names}",
            "store only v1 allowlisted identifiers, classifications, and hashes",
        )
    normalized: dict[str, Any] = {}
    for key, value in facts.items():
        if key in {"publication_ordinal", "predecessor_ordinal", "feedback_revision"}:
            if value is None and key == "predecessor_ordinal":
                normalized[key] = None
                continue
            if type(value) is not int or value < 1:
                raise SelcError(
                    "INVALID_OBSERVATION", f"fact {key} must be a positive integer", "fix adapter classification"
                )
            normalized[key] = value
            continue
        if not isinstance(value, str) or not value:
            raise SelcError(
                "INVALID_OBSERVATION", f"fact {key} must be a non-empty string", "fix adapter classification"
            )
        if len(value.encode("utf-8")) > MAX_FACT_VALUE_BYTES:
            raise SelcError(
                "OBSERVATION_TOO_LARGE", f"fact {key} is too large", "store a digest or reference"
            )
        normalized[key] = value
    if normalized.get("actor") not in {None, *ACTORS}:
        raise SelcError("INVALID_OBSERVATION", "actor classification is invalid", "use a v1 actor")
    if normalized.get("origin") not in {None, *ORIGINS}:
        raise SelcError("INVALID_OBSERVATION", "origin classification is invalid", "use a v1 origin")
    if normalized.get("causation") not in {None, *CAUSATIONS}:
        raise SelcError(
            "INVALID_OBSERVATION", "causation classification is invalid", "use a v1 causation"
        )
    if normalized.get("checker_result") not in {None, "pass", "fail", "unknown"}:
        raise SelcError("INVALID_OBSERVATION", "checker result is invalid", "use pass, fail, or unknown")
    if normalized.get("result") not in {None, "pass", "fail", "unknown"}:
        raise SelcError("INVALID_OBSERVATION", "event result is invalid", "use pass, fail, or unknown")
    if normalized.get("feedback_result") not in {None, "pass", "fail", "unknown"}:
        raise SelcError("INVALID_OBSERVATION", "feedback result is invalid", "use pass, fail, or unknown")
    return normalized


def _store_raw(database: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    digest = json_hash(_digest_payload(event))
    stable_identity = event["stable_identity"]
    with records.projection_transaction(database):
        if stable_identity is not None:
            original = database.execute(
                """SELECT event_id, event_digest FROM observation_events
                WHERE stable_identity = ? ORDER BY received_at LIMIT 1""",
                (stable_identity,),
            ).fetchone()
            if original is not None:
                if original["event_digest"] == digest:
                    return {"event_id": original["event_id"], "status": "replayed", "replayed": True}
                database.execute(
                    """INSERT INTO observation_conflicts(
                    stable_identity, original_event_id, incoming_digest, classification_version,
                    facts_json, received_at, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stable_identity,
                        original["event_id"],
                        digest,
                        event["classification_version"],
                        _encoded(event["facts"]),
                        records.utc_now(),
                        "same stable native identity has a different payload",
                    ),
                )
                return {
                    "event_id": original["event_id"],
                    "status": "conflict",
                    "replayed": False,
                    "reason": "same stable native identity has a different payload",
                }
        status, reason = _ingest_status(database, event)
        event_id = str(uuid.uuid4())
        identity = event["identity"]
        facts = event["facts"]
        database.execute(
            """INSERT INTO observation_events(
            event_id, stable_identity, event_digest, protocol, adapter_name, adapter_version,
            installation_id, host, surface, session_id, turn_id, native_event_id, phase,
            source_context, source_epoch, suite_id, task_id, kind, occurred_at, received_at,
            classification_version, facts_json, evidence_json, status, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                stable_identity,
                digest,
                event["protocol"],
                event["adapter_name"],
                event["adapter_version"],
                identity["installation_id"],
                identity["host"],
                identity["surface"],
                identity.get("session_id"),
                identity.get("turn_id"),
                identity.get("native_event_id"),
                event["phase"],
                event["source_context"],
                event["source_epoch"],
                facts.get("suite_id"),
                facts.get("task_id"),
                event["kind"],
                event["occurred_at"],
                records.utc_now(),
                event["classification_version"],
                _encoded(facts),
                _encoded(event["evidence"]),
                status,
                reason,
            ),
        )
    return {"event_id": event_id, "status": status, "replayed": False, "reason": reason}


def _ingest_status(database: sqlite3.Connection, event: dict[str, Any]) -> tuple[str, str | None]:
    deleted_subject = _deleted_subject(database, event["facts"])
    if deleted_subject is not None:
        return "quarantined", f"deleted {deleted_subject} cannot be recreated"
    stable_identity = event["stable_identity"]
    if stable_identity is not None:
        tombstone = database.execute(
            "SELECT 1 FROM observation_tombstones WHERE stable_identity = ?", (stable_identity,)
        ).fetchone()
        if tombstone is not None:
            return "quarantined", "deleted stable identity cannot be replayed"
    invalid_source = database.execute(
        """SELECT invalid_before_epoch FROM observation_source_epochs
        WHERE source_context = ?""",
        (event["source_context"],),
    ).fetchone()
    if invalid_source is not None and event["source_epoch"] <= invalid_source["invalid_before_epoch"]:
        return "quarantined", "source context epoch was invalidated by deletion"
    suite_id = event["facts"].get("suite_id")
    if suite_id is not None and _range_matches(database, suite_id, event["occurred_at"]):
        return "quarantined", "event falls inside a deleted suite or time range"
    return "pending", None


def _deleted_subject(database: sqlite3.Connection, facts: dict[str, Any]) -> str | None:
    """Reject a new source that attempts to recreate a locally deleted subject.

    A fresh source context and epoch prove only that its transport is new.  They
    do not authorize recreating a task or call whose local record was deleted.
    The suppressions table is shared with minimal snapshot import handling, so
    it is deliberately checked without assuming the incoming source origin.
    """

    for subject_kind, fact_key in (("task", "task_id"), ("call", "call_id")):
        subject_id = facts.get(fact_key)
        if subject_id is None:
            continue
        row = database.execute(
            """SELECT 1 FROM suppressions
            WHERE subject_kind = ? AND subject_id = ? LIMIT 1""",
            (subject_kind, subject_id),
        ).fetchone()
        if row is not None:
            return subject_kind
    return None


def _reduce_one(database: sqlite3.Connection, row: sqlite3.Row) -> str:
    event = _row_event(row)
    with records.projection_transaction(database):
        suite_id = _resolve_suite(database, event)
        if suite_id is not None:
            database.execute(
                "UPDATE observation_events SET suite_id = ? WHERE event_id = ?",
                (suite_id, event["event_id"]),
            )
            event["suite_id"] = suite_id
        suppression = _replay_suppression(database, event)
        if suppression is not None:
            database.execute(
                "UPDATE observation_events SET status = 'quarantined', reason = ? WHERE event_id = ?",
                (suppression, event["event_id"]),
            )
            return "quarantined"
        try:
            _apply_projection(database, event)
        except SelcError as error:
            if error.code in {"MISSING_TASK", "MISSING_CALL", "INVALID_DELIVERY_LINK", "INVALID_FEEDBACK_LINK"}:
                database.execute(
                    "UPDATE observation_events SET reason = ? WHERE event_id = ?",
                    (f"pending dependency: {error.code}", event["event_id"]),
                )
                return "pending"
            raise
        database.execute(
            """UPDATE observation_events
            SET status = 'applied', reason = NULL, applied_at = ? WHERE event_id = ?""",
            (records.utc_now(), event["event_id"]),
        )
        task_id = event["facts"].get("task_id")
        if task_id is not None:
            _refresh_assessment(database, task_id)
    return "applied"


def _apply_projection(database: sqlite3.Connection, event: dict[str, Any]) -> None:
    facts = event["facts"]
    kind = event["kind"]
    evidence = f"observation:{event['event_id']}"
    if kind == "task_started":
        records._project_start_task(
            database,
            task_id=_fact(facts, "task_id"),
            suite_id=_fact(facts, "suite_id"),
            suite_version=_fact(facts, "suite_version"),
            harness=_fact(facts, "harness"),
            initiator=_fact(facts, "initiator"),
            evidence=evidence,
        )
    elif kind == "call_started":
        records._project_start_call(
            database,
            call_id=_fact(facts, "call_id"),
            task_id=_fact(facts, "task_id"),
            skill=_fact(facts, "skill"),
            executor=_fact(facts, "executor"),
            evidence=evidence,
            expected_version=_fact(facts, "expected_version"),
            expected_hash=facts.get("expected_hash"),
            trigger_kind=_fact(facts, "trigger_kind"),
        )
    elif kind == "input_ready":
        records._project_task_event(
            database, task_id=_fact(facts, "task_id"), kind="input_ready", evidence=evidence
        )
    elif kind == "permission_granted":
        records._project_task_event(
            database, task_id=_fact(facts, "task_id"), kind="permission_click", evidence=evidence
        )
    elif kind == "automatic_retry":
        records._project_task_event(
            database, task_id=_fact(facts, "task_id"), kind="automatic_retry", evidence=evidence
        )
    elif kind == "business_intervention":
        records._project_task_event(
            database, task_id=_fact(facts, "task_id"), kind="business_intervention", evidence=evidence
        )
    elif kind == "delivery_published":
        predecessor = facts.get("predecessor_ordinal")
        ordinal = _fact_int(facts, "publication_ordinal")
        if ordinal == 1 and predecessor is not None:
            raise SelcError(
                "INVALID_PUBLICATION_ORDER", "first publication cannot name a predecessor", "fix runtime order"
            )
        if ordinal > 1 and predecessor != ordinal - 1:
            raise SelcError(
                "INVALID_PUBLICATION_ORDER", "publication predecessor is not contiguous", "fix runtime order"
            )
        if predecessor is not None:
            prior = database.execute(
                "SELECT 1 FROM deliveries WHERE task_id = ? AND sequence = ?",
                (_fact(facts, "task_id"), predecessor),
            ).fetchone()
            if prior is None:
                raise SelcError("MISSING_TASK", "publication predecessor is pending", "replay after predecessor")
        records._project_delivery(
            database,
            delivery_id=_fact(facts, "delivery_id"),
            task_id=_fact(facts, "task_id"),
            call_id=facts.get("call_id"),
            artifact_hash=_fact(facts, "artifact_hash"),
            rule_version=_fact(facts, "rule_version"),
            checker_id=_fact(facts, "checker_id"),
            checker_result=_fact(facts, "checker_result"),
            checker_identity={
                key: _fact(facts, key)
                for key in (
                    "checker_input_hash",
                    "checker_artifact_hash",
                    "checker_resource_hash",
                    "checker_composite_hash",
                )
            },
            sequence=ordinal,
        )
    elif kind == "call_completed":
        records._project_complete_call(
            database, call_id=_fact(facts, "call_id"), result=_fact(facts, "result")
        )
    elif kind == "feedback":
        records._project_feedback(
            database,
            feedback_id=_fact(facts, "feedback_id"),
            task_id=_fact(facts, "task_id"),
            delivery_id=_fact(facts, "delivery_id"),
            revision=_fact_int(facts, "feedback_revision"),
            scope=_fact(facts, "feedback_scope"),
            result=_fact(facts, "feedback_result"),
            body_hash=facts.get("feedback_body_hash"),
        )
    elif kind == "task_interrupted":
        task_id = _fact(facts, "task_id")
        if facts.get("call_id"):
            records._project_complete_call(database, call_id=_fact(facts, "call_id"), result="unknown")
        records._project_task_event(database, task_id=task_id, kind="interrupted", evidence=evidence)
    elif kind == "task_closed":
        task_id = _fact(facts, "task_id")
        task = records._task(database, task_id)
        active = database.execute(
            "SELECT COUNT(*) FROM calls WHERE task_id = ? AND status = 'active'", (task_id,)
        ).fetchone()[0]
        if active:
            raise SelcError("MISSING_CALL", "task has active calls", "wait for call outcome")
        if task["status"] == "active":
            database.execute(
                "UPDATE tasks SET status = 'closed', closed_at = ? WHERE task_id = ?",
                (records.utc_now(), task_id),
            )
    else:
        raise SelcError("INVALID_OBSERVATION", "unhandled observation kind", "upgrade reducer")


def _refresh_assessment_for_event(database: sqlite3.Connection, event_id: str) -> None:
    """Refresh an existing task after durable unresolved evidence arrives."""

    row = database.execute(
        "SELECT task_id FROM observation_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None or row["task_id"] is None:
        return
    with records.projection_transaction(database):
        _refresh_assessment(database, str(row["task_id"]))


def _refresh_assessments_with_durable_gaps(database: sqlite3.Connection) -> None:
    """Restore assessments affected by pending, quarantined, or conflict evidence.

    Conflict rows have no reducer status and pending rows may never make progress,
    so a crash after raw persistence cannot rely on another applied event to
    revise the task's first-result evidence.  The query is intentionally task
    scoped; call assessment remains isolated by its own event selection.
    """

    rows = database.execute(
        """SELECT DISTINCT task_id FROM observation_events
        WHERE task_id IS NOT NULL AND status IN ('pending', 'quarantined')
        UNION
        SELECT DISTINCT events.task_id FROM observation_conflicts AS conflicts
        JOIN observation_events AS events ON events.event_id = conflicts.original_event_id
        WHERE events.task_id IS NOT NULL"""
    ).fetchall()
    if not rows:
        return
    with records.projection_transaction(database):
        for row in rows:
            _refresh_assessment(database, str(row["task_id"]))


def _refresh_assessment(database: sqlite3.Connection, task_id: str) -> None:
    task = database.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        return
    events = database.execute(
        """SELECT * FROM observation_events
        WHERE task_id = ? AND status = 'applied' ORDER BY received_at, event_id""",
        (task_id,),
    ).fetchall()
    pending_count = int(database.execute(
        """SELECT COUNT(*) FROM observation_events
        WHERE task_id = ? AND status IN ('pending', 'quarantined')""",
        (task_id,),
    ).fetchone()[0])
    conflicted_event_ids = {
        str(row["original_event_id"])
        for row in database.execute(
            """SELECT original_event_id FROM observation_conflicts
        WHERE original_event_id IN (
          SELECT event_id FROM observation_events WHERE task_id = ?
        )""",
        (task_id,),
        ).fetchall()
    }
    delivery_rows = database.execute(
        """SELECT * FROM deliveries WHERE task_id = ? ORDER BY sequence""", (task_id,)
    ).fetchall()
    delivery_events = {
        str(row["facts"]["delivery_id"]): row
        for row in (_row_event(event) for event in events)
        if row["kind"] == "delivery_published" and "delivery_id" in row["facts"]
    }
    parsed_events = [_row_event(event) for event in events]
    publication_gap = _has_publication_gap(delivery_rows, delivery_events)
    formal_start = any(
        event["kind"] == "task_started" and _formal_start(event["facts"])
        for event in parsed_events
    )
    task_result, assessment_evidence = _assessment_for_scope(
        database,
        task_id=task_id,
        deliveries=delivery_rows,
        delivery_events=delivery_events,
        events=parsed_events,
        formal_start=formal_start,
        input_ready=bool(task["input_ready"]),
        pending_count=pending_count,
        conflicted_event_ids=conflicted_event_ids,
        publication_gap=publication_gap,
        call_id=None,
    )
    database.execute(
        """INSERT INTO observation_assessments(
        task_id, first_result, final_result, assessment_version, evidence_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
          first_result = excluded.first_result,
          final_result = excluded.final_result,
          assessment_version = excluded.assessment_version,
          evidence_json = excluded.evidence_json,
          updated_at = excluded.updated_at""",
        (
            task_id,
            task_result["first_result"],
            task_result["final_result"],
            CLASSIFICATION_VERSION,
            _encoded(assessment_evidence),
            records.utc_now(),
        ),
    )
    database.execute(
        "UPDATE tasks SET first_result = ?, current_result = ? WHERE task_id = ?",
        (task_result["first_result"], task_result["final_result"], task_id),
    )
    call_rows = database.execute("SELECT * FROM calls WHERE task_id = ?", (task_id,)).fetchall()
    for call in call_rows:
        call_id = str(call["call_id"])
        call_deliveries = [row for row in delivery_rows if row["call_id"] == call_id]
        call_events = _events_for_call(parsed_events, call_id)
        call_formal_start = any(
            event["kind"] == "call_started"
            and event["facts"].get("call_id") == call_id
            and _formal_call_start(event["facts"])
            for event in call_events
        )
        call_result, call_evidence = _assessment_for_scope(
            database,
            task_id=task_id,
            deliveries=call_deliveries,
            delivery_events=delivery_events,
            events=call_events,
            formal_start=formal_start and call_formal_start,
            input_ready=bool(task["input_ready"]),
            pending_count=_pending_for_call(database, task_id, call_id),
            conflicted_event_ids=conflicted_event_ids,
            publication_gap=publication_gap,
            call_id=call_id,
        )
        database.execute(
            """INSERT INTO observation_call_assessments(
            call_id, task_id, first_result, final_result, assessment_version, evidence_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
              task_id = excluded.task_id,
              first_result = excluded.first_result,
              final_result = excluded.final_result,
              assessment_version = excluded.assessment_version,
              evidence_json = excluded.evidence_json,
              updated_at = excluded.updated_at""",
            (
                call_id,
                task_id,
                call_result["first_result"],
                call_result["final_result"],
                CLASSIFICATION_VERSION,
                _encoded(call_evidence),
                records.utc_now(),
            ),
        )
        database.execute(
            "UPDATE calls SET first_result = ?, result = ? WHERE call_id = ?",
            (call_result["first_result"], call_result["final_result"], call_id),
        )


def _assessment_for_scope(
    database: sqlite3.Connection,
    *,
    task_id: str,
    deliveries: list[sqlite3.Row],
    delivery_events: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    formal_start: bool,
    input_ready: bool,
    pending_count: int,
    conflicted_event_ids: set[str],
    publication_gap: bool,
    call_id: str | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    gap = publication_gap
    execution_events = [event for event in events if event["phase"] != "post_delivery"]
    execution_conflicts = [
        event for event in execution_events if event["event_id"] in conflicted_event_ids
    ]
    uncertainty = any(
        event["facts"].get("actor") == "unknown"
        or event["facts"].get("origin") == "unknown"
        or event["facts"].get("causation") == "unknown"
        for event in execution_events
    )
    interventions = [
        event
        for event in execution_events
        if (
            event["kind"] == "business_intervention"
            or event["facts"].get("causation") in {"business_correction", "audit"}
        )
        and (call_id is None or event["facts"].get("call_id") == call_id)
    ]
    reasons: list[str] = []
    if pending_count:
        reasons.append("pending_or_quarantined_events")
    if execution_conflicts:
        reasons.append("native_identity_conflict")
    if gap:
        reasons.append("publication_gap")
    if uncertainty:
        reasons.append("unknown_classification")
    if not formal_start:
        reasons.append("missing_verified_start")
    first_result = "unknown"
    final_result = "unknown"
    first_evidence_complete = not pending_count and not execution_conflicts and not gap
    if deliveries:
        first_delivery = deliveries[0]
        final_delivery = deliveries[-1]
        first_effective = records._delivery_result_with_feedback(database, task_id, first_delivery)
        final_effective = records._delivery_result_with_feedback(database, task_id, final_delivery)
        first_formal = _formal_delivery(delivery_events.get(first_delivery["delivery_id"]))
        final_formal = _formal_delivery(delivery_events.get(final_delivery["delivery_id"]))
        final_event = delivery_events.get(final_delivery["delivery_id"])
        final_delivery_conflicted = (
            final_event is not None and final_event["event_id"] in conflicted_event_ids
        )
        if interventions or first_effective == "fail":
            first_result = "fail"
        elif (
            first_evidence_complete
            and first_effective == "pass"
            and input_ready
            and formal_start
            and first_formal
            and not uncertainty
        ):
            first_result = "pass"
        if final_effective == "fail":
            final_result = "fail"
        elif (
            final_effective == "pass"
            and final_formal
            and not gap
            and not final_delivery_conflicted
        ):
            final_result = "pass"
    return (
        {"first_result": first_result, "final_result": final_result},
        {
            "pending_events": pending_count,
            "conflicts": len(execution_conflicts),
            "publication_gap": gap,
            "interventions": len(interventions),
            "final_delivery_conflicted": bool(
                deliveries
                and delivery_events.get(deliveries[-1]["delivery_id"], {}).get("event_id")
                in conflicted_event_ids
            ),
            "reasons": reasons,
        },
    )


def _events_for_call(events: list[dict[str, Any]], call_id: str) -> list[dict[str, Any]]:
    """Return evidence that belongs to one call or establishes its task context.

    Task start and prepared input are shared prerequisites.  A task-level
    correction, audit, or ambiguous event without a call link must not turn an
    unrelated call's independent denominator into fail or unknown.
    """

    shared_kinds = {"task_started", "input_ready", "permission_granted", "automatic_retry"}
    return [
        event
        for event in events
        if event["facts"].get("call_id") == call_id
        or (
            event["facts"].get("call_id") is None
            and event["kind"] in shared_kinds
        )
    ]


def _pending_for_call(database: sqlite3.Connection, task_id: str, call_id: str) -> int:
    """Count unresolved evidence that explicitly belongs to this call."""

    rows = database.execute(
        """SELECT facts_json FROM observation_events
        WHERE task_id = ? AND status IN ('pending', 'quarantined')""",
        (task_id,),
    ).fetchall()
    return sum(json.loads(row["facts_json"]).get("call_id") == call_id for row in rows)


def _formal_start(facts: dict[str, Any]) -> bool:
    return (
        facts.get("actor") == "human"
        and facts.get("origin") == "managed_semantic"
        and facts.get("causation") == "original_request"
    )


def _formal_call_start(facts: dict[str, Any]) -> bool:
    return (
        facts.get("actor") in {"agent", "host"}
        and facts.get("origin") == "managed_semantic"
        and facts.get("causation") == "original_request"
    )


def _formal_delivery(event: dict[str, Any] | None) -> bool:
    if event is None:
        return False
    facts = event["facts"]
    return (
        facts.get("delivery_role") == "user_visible"
        and facts.get("origin") == "managed_semantic"
        and facts.get("causation") == "original_request"
        and facts.get("checker_result") == "pass"
        and all(
            isinstance(facts.get(key), str) and facts[key]
            for key in (
                "checker_id",
                "checker_input_hash",
                "checker_artifact_hash",
                "checker_resource_hash",
                "checker_composite_hash",
            )
        )
    )


def _has_publication_gap(
    deliveries: list[sqlite3.Row], delivery_events: dict[str, dict[str, Any]]
) -> bool:
    if not deliveries:
        return False
    expected = 1
    for delivery in deliveries:
        if delivery["sequence"] != expected:
            return True
        event = delivery_events.get(delivery["delivery_id"])
        if event is None:
            return True
        predecessor = event["facts"].get("predecessor_ordinal")
        if (expected == 1 and predecessor is not None) or (expected > 1 and predecessor != expected - 1):
            return True
        expected += 1
    return False


def _resolve_suite(database: sqlite3.Connection, event: dict[str, Any]) -> str | None:
    if event["suite_id"]:
        return str(event["suite_id"])
    task_id = event["facts"].get("task_id")
    if task_id is None:
        return None
    task = database.execute("SELECT suite_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return str(task["suite_id"]) if task is not None else None


def _replay_suppression(database: sqlite3.Connection, event: dict[str, Any]) -> str | None:
    deleted_subject = _deleted_subject(database, event["facts"])
    if deleted_subject is not None:
        return f"deleted {deleted_subject} cannot be recreated"
    if event["stable_identity"]:
        tombstone = database.execute(
            "SELECT 1 FROM observation_tombstones WHERE stable_identity = ?",
            (event["stable_identity"],),
        ).fetchone()
        if tombstone is not None:
            return "deleted stable identity cannot be replayed"
    source = database.execute(
        """SELECT invalid_before_epoch FROM observation_source_epochs
        WHERE source_context = ?""",
        (event["source_context"],),
    ).fetchone()
    if source is not None and event["source_epoch"] <= source["invalid_before_epoch"]:
        return "source context epoch was invalidated by deletion"
    # Unknown-suite observations remain pending until a trusted task dependency
    # resolves their association.  A deleted source context is handled above and
    # is quarantined even while the suite remains unknown.
    if event["suite_id"] is None:
        return None
    if _range_matches(database, event["suite_id"], event["occurred_at"]):
        return "event falls inside a deleted suite or time range"
    return None


def _range_matches(database: sqlite3.Connection, suite_id: str, occurred_at: str) -> bool:
    rows = database.execute(
        """SELECT suite_id, before_time FROM observation_deletion_ranges
        WHERE suite_id = ? OR suite_id = '*'""",
        (suite_id,),
    ).fetchall()
    return any(row["before_time"] is None or occurred_at < row["before_time"] for row in rows)


def _row_event(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["facts"] = json.loads(value.pop("facts_json"))
    value["evidence"] = json.loads(value.pop("evidence_json"))
    return value


def _stable_identity(identity: dict[str, str | None], kind: str, phase: str) -> str | None:
    components = (
        identity.get("installation_id"),
        identity.get("host"),
        identity.get("surface"),
        identity.get("session_id"),
        identity.get("turn_id"),
        identity.get("native_event_id"),
        kind,
        phase,
    )
    if any(component is None for component in components):
        return None
    return json_hash({"stable_identity_v1": components})


def _digest_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"stable_identity"}
    }


def _required_keys(value: dict[str, Any], keys: set[str], name: str) -> None:
    missing = keys.difference(value)
    if missing:
        raise SelcError(
            "INVALID_OBSERVATION",
            f"{name} misses required fields: {', '.join(sorted(missing))}",
            "send complete adapter DTO v1",
        )


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SelcError("INVALID_OBSERVATION", f"{name} must be an object", "send adapter DTO v1")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SelcError("INVALID_OBSERVATION", f"{name} must be a non-empty string", "fix adapter DTO")
    return value


def _nullable_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _fact(facts: dict[str, Any], key: str) -> str:
    value = facts.get(key)
    if not isinstance(value, str) or not value:
        raise SelcError(
            "INVALID_OBSERVATION", f"{key} is required for this event", "fix adapter classification"
        )
    return value


def _fact_int(facts: dict[str, Any], key: str) -> int:
    value = facts.get(key)
    if type(value) is not int or value < 1:
        raise SelcError(
            "INVALID_OBSERVATION", f"{key} is required for this event", "fix adapter classification"
        )
    return value


def _encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
