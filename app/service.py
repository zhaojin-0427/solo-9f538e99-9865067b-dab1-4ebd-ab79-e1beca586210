"""Transactional business logic for uploads and firmware registration."""

import json
from contextlib import contextmanager

from . import decoder, diagnosis


class ConflictError(Exception):
    """Request conflicts with stored, immutable state."""


class BatchRejected(Exception):
    """The whole upload is rejected (e.g. content mismatch on retransmit)."""

    def __init__(self, message, conflicts=None):
        super().__init__(message)
        self.conflicts = conflicts or []


@contextmanager
def write_transaction(db):
    """Single serialised writer; everything commits or nothing does."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _firmware_orders(db, versions):
    versions = list(versions)
    if not versions:
        return {}
    placeholders = ",".join("?" for _ in versions)
    rows = db.execute(
        "SELECT fw_version, word_order FROM firmware WHERE fw_version IN (%s)"
        % placeholders,
        versions,
    ).fetchall()
    return {r["fw_version"]: r["word_order"] for r in rows}


def ingest(db, rows):
    """Apply a validated batch atomically.

    Rules:
      * unknown firmware -> stored pending, no guessed reading;
      * registered firmware -> decoded exactly once, at insert time;
      * retransmission (same device+frame+content) -> reuse stored result;
      * same device+frame with different content -> reject the whole batch.
    """
    summary = {
        "received": len(rows),
        "inserted": 0,
        "reused": 0,
        "decoded": 0,
        "invalid": 0,
        "pending": 0,
        "inserted_keys": [],
    }

    with write_transaction(db):
        orders = _firmware_orders(db, {r["fw_version"] for r in rows})

        # Reject the whole batch before touching anything.
        existing = {}
        conflicts = []
        for row in rows:
            key = (row["device_id"], row["frame_no"])
            if key in existing:
                continue
            prev = db.execute(
                "SELECT raw_word, fw_version FROM frames WHERE device_id = ? AND frame_no = ?",
                key,
            ).fetchone()
            existing[key] = prev
            if prev is not None and (
                prev["raw_word"] != row["raw_word"]
                or prev["fw_version"] != row["fw_version"]
            ):
                conflicts.append(
                    {
                        "device_id": row["device_id"],
                        "frame_no": row["frame_no"],
                        "stored_raw_word": "%s (%s)"
                        % (prev["raw_word"], prev["fw_version"]),
                        "uploaded_raw_word": "%s (%s)"
                        % (row["raw_word"], row["fw_version"]),
                    }
                )
        if conflicts:
            raise BatchRejected(
                "上传被拒绝：%d 帧与该设备已存帧号内容不一致" % len(conflicts),
                conflicts,
            )

        inserted_in_batch = set()
        affected = set()  # (device_id, fw_version) with newly stored frames
        for row in rows:
            key = (row["device_id"], row["frame_no"])
            prev = existing.get(key)
            if prev is not None or key in inserted_in_batch:
                summary["reused"] += 1
                continue

            fw = row["fw_version"]
            order = orders.get(fw)
            if order is None:
                status, reading, bits = "pending", None, None
                summary["pending"] += 1
            else:
                status, reading, bits = decoder.decode(row["raw_word"], order)
                if status == "decoded":
                    summary["decoded"] += 1
                else:
                    summary["invalid"] += 1

            db.execute(
                """INSERT INTO frames
                   (device_id, frame_no, fw_version, raw_word,
                    status, reading, word_order, bit_pattern, decoded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                           CASE WHEN ? IS NULL THEN NULL
                                ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END)""",
                (
                    row["device_id"],
                    row["frame_no"],
                    fw,
                    row["raw_word"],
                    status,
                    reading,
                    order,
                    bits,
                    bits,
                ),
            )
            inserted_in_batch.add(key)
            affected.add((row["device_id"], fw))
            summary["inserted"] += 1
            summary["inserted_keys"].append(key)

        # Re-evaluate the diagnostic sheets touched by this batch in the same
        # transaction, so any new conclusion commits atomically with the frames
        # it is based on.  Batches that only retransmit (no inserts) change no
        # frame state and therefore produce no new conclusions.
        for device_id, fw_version in affected:
            _reevaluate_sheets(db, device_id=device_id, fw_version=fw_version)

    return summary


def register_firmware(db, fw_version, word_order):
    """Register an immutable word order and backfill its pending frames.

    Registration and backfill happen in one transaction, so no reader can
    observe the firmware as configured while its frames are still pending.
    """
    if word_order not in decoder.WORD_ORDERS:
        raise ConflictError("字序只能是 high_first 或 low_first")

    with write_transaction(db):
        row = db.execute(
            "SELECT word_order FROM firmware WHERE fw_version = ?", (fw_version,)
        ).fetchone()
        already = row is not None
        if already:
            if row["word_order"] != word_order:
                raise ConflictError(
                    "固件 %s 已登记为「%s」，字序不可更改（请求「%s」）"
                    % (
                        fw_version,
                        decoder.WORD_ORDERS[row["word_order"]],
                        decoder.WORD_ORDERS[word_order],
                    )
                )
        else:
            db.execute(
                "INSERT INTO firmware (fw_version, word_order) VALUES (?, ?)",
                (fw_version, word_order),
            )

        # Even for an already-registered firmware, sweep any row still marked
        # pending so the invariant "registered implies nothing pending" holds
        # no matter how the data got there.  The status guard makes this safe
        # to repeat.
        pending = db.execute(
            "SELECT id, raw_word FROM frames WHERE fw_version = ? AND status = 'pending'",
            (fw_version,),
        ).fetchall()

        decoded = invalid = 0
        for frame in pending:
            status, reading, bits = decoder.decode(frame["raw_word"], word_order)
            count = db.execute(
                """UPDATE frames
                   SET status = ?, reading = ?, word_order = ?, bit_pattern = ?,
                       decoded_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id = ? AND status = 'pending'""",
                (status, reading, word_order, bits, frame["id"]),
            ).rowcount
            # The row was selected inside this write transaction and the
            # UPDATE repeats the pending guard, so a frame cannot be decoded
            # twice.
            if count == 1:
                if status == "decoded":
                    decoded += 1
                else:
                    invalid += 1

        # Re-evaluate every sheet for this firmware in the same transaction as
        # the registration/backfill: a sheet must never read "registered
        # firmware" while its evidence still shows a pending firmware.
        _reevaluate_sheets(db, fw_version=fw_version)

    return {
        "fw_version": fw_version,
        "word_order": word_order,
        "already_registered": already,
        "backfilled": decoded + invalid,
        "decoded": decoded,
        "invalid": invalid,
    }


def list_devices(db):
    rows = db.execute(
        """SELECT device_id,
                  COUNT(*) AS frame_count,
                  SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                  MAX(received_at) AS last_seen
           FROM frames
           GROUP BY device_id
           ORDER BY device_id"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_firmwares(db):
    rows = db.execute(
        """SELECT f.fw_version, f.word_order, f.registered_at,
                  COUNT(fr.id) AS frame_count,
                  SUM(CASE WHEN fr.status = 'pending' THEN 1 ELSE 0 END) AS pending_count
           FROM firmware f
           LEFT JOIN frames fr ON fr.fw_version = f.fw_version
           GROUP BY f.fw_version
           ORDER BY f.fw_version"""
    ).fetchall()
    return [dict(r) for r in rows]


def device_frames(db, device_id):
    rows = db.execute(
        """SELECT frame_no, fw_version, raw_word, status, reading, word_order,
                  bit_pattern, received_at, decoded_at
           FROM frames WHERE device_id = ? ORDER BY frame_no""",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def batch_frames(db, rows):
    """Current stored state of the frames in a submitted batch.

    Reading this inside one snapshot after the write commits means the upload
    response reflects a firmware registration that ran concurrently and had
    already committed, never a stale "pending".
    """
    result = []
    seen = set()
    for row in rows:
        key = (row["device_id"], row["frame_no"])
        if key in seen:
            continue
        seen.add(key)
        stored = db.execute(
            """SELECT device_id, frame_no, fw_version, raw_word, status, reading,
                      bit_pattern
               FROM frames WHERE device_id = ? AND frame_no = ?""",
            key,
        ).fetchone()
        if stored is not None:
            result.append(dict(stored))
    return result


# --- Diagnostic sheets ----------------------------------------------------


def _registered_order(db, fw_version):
    row = db.execute(
        "SELECT word_order FROM firmware WHERE fw_version = ?", (fw_version,)
    ).fetchone()
    return row["word_order"] if row is not None else None


def _frame_lookup(db, device_id, frame_nos):
    """Stored raw frames for a sheet's frame numbers (one snapshot)."""
    if not frame_nos:
        return {}
    placeholders = ",".join("?" for _ in frame_nos)
    rows = db.execute(
        """SELECT frame_no, raw_word, fw_version FROM frames
           WHERE device_id = ? AND frame_no IN (%s)""" % placeholders,
        [device_id, *frame_nos],
    ).fetchall()
    return {
        r["frame_no"]: {"raw_word": r["raw_word"], "fw_version": r["fw_version"]}
        for r in rows
    }


def _store_conclusion(db, sheet_id, result):
    """Append a conclusion row only when the conclusion actually changed.

    Evidence/detail are stored on the same row as the code, so a reader can
    never observe a conclusion whose samples come from a different state.
    Returns ``(appended, conclusion_row)``.
    """
    latest = db.execute(
        """SELECT conclusion, detail_json, evidence_json
           FROM diagnostic_conclusions WHERE sheet_id = ?
           ORDER BY id DESC LIMIT 1""",
        (sheet_id,),
    ).fetchone()
    detail_text = (
        diagnosis.canonical_json(result["detail"])
        if result["detail"] is not None
        else None
    )
    evidence_text = diagnosis.canonical_json(result["evidence"])
    if (
        latest is not None
        and latest["conclusion"] == result["code"]
        and (latest["detail_json"] or None) == (detail_text or None)
        and latest["evidence_json"] == evidence_text
    ):
        return False, latest

    cur = db.execute(
        """INSERT INTO diagnostic_conclusions
               (sheet_id, conclusion, detail_json, evidence_json)
           VALUES (?, ?, ?, ?)""",
        (sheet_id, result["code"], detail_text, evidence_text),
    )
    return True, db.execute(
        """SELECT id, conclusion, detail_json, evidence_json, created_at
           FROM diagnostic_conclusions WHERE id = ?""",
        (cur.lastrowid,),
    ).fetchone()


def _evaluate_sheet(db, sheet):
    entries = json.loads(sheet["entries_json"])
    order = _registered_order(db, sheet["fw_version"])
    lookup = _frame_lookup(db, sheet["device_id"], [e["frame_no"] for e in entries])
    return diagnosis.evaluate(
        sheet["fw_version"], entries, lookup, order
    )


def _reevaluate_sheets(db, device_id=None, fw_version=None):
    """Re-evaluate sheets affected by a committed frame/firmware change.

    Must be called inside a ``write_transaction``.  Sheets whose current
    conclusion is already terminal (not ``pending``) cannot change -- frames
    and word order are immutable -- so they are skipped.  Irrelevant uploads
    never reach this function and therefore never create a conclusion.
    """
    sql = (
        "SELECT id, device_id, fw_version, entries_json FROM diagnostic_sheets"
    )
    clauses, params = [], []
    if fw_version is not None:
        clauses.append("fw_version = ?")
        params.append(fw_version)
    if device_id is not None:
        clauses.append("device_id = ?")
        params.append(device_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"

    reevaluated = 0
    appended = 0
    for sheet in db.execute(sql, params).fetchall():
        latest = db.execute(
            """SELECT conclusion FROM diagnostic_conclusions
               WHERE sheet_id = ? ORDER BY id DESC LIMIT 1""",
            (sheet["id"],),
        ).fetchone()
        if latest is not None and latest["conclusion"] != "pending":
            continue
        result = _evaluate_sheet(db, sheet)
        did_append, _row = _store_conclusion(db, sheet["id"], result)
        reevaluated += 1
        if did_append:
            appended += 1
    return {"reevaluated": reevaluated, "appended": appended}


def create_diagnostic_sheet(db, device_id, fw_version, entries):
    """Store a validated sheet and evaluate it once, atomically."""
    with write_transaction(db):
        cur = db.execute(
            """INSERT INTO diagnostic_sheets (device_id, fw_version, entries_json)
               VALUES (?, ?, ?)""",
            (
                device_id,
                fw_version,
                diagnosis.canonical_json(entries),
            ),
        )
        sheet_id = cur.lastrowid
        sheet = db.execute(
            "SELECT id, device_id, fw_version, entries_json, created_at"
            " FROM diagnostic_sheets WHERE id = ?",
            (sheet_id,),
        ).fetchone()
        result = _evaluate_sheet(db, sheet)
        _appended, row = _store_conclusion(db, sheet_id, result)

    return {
        "sheet": {
            "id": sheet["id"],
            "device_id": sheet["device_id"],
            "fw_version": sheet["fw_version"],
            "entries": json.loads(sheet["entries_json"]),
            "created_at": sheet["created_at"],
        },
        "conclusion": _conclusion_to_dict(row),
    }


def _conclusion_to_dict(row):
    return {
        "id": row["id"],
        "conclusion": row["conclusion"],
        "detail": json.loads(row["detail_json"]) if row["detail_json"] else None,
        "evidence": json.loads(row["evidence_json"]),
        "created_at": row["created_at"],
    }


def list_diagnostic_sheets(db):
    """Sheets with their latest conclusion, read in one snapshot."""
    rows = db.execute(
        """SELECT s.id, s.device_id, s.fw_version, s.entries_json, s.created_at,
                  c.id AS conclusion_id, c.conclusion, c.detail_json,
                  c.evidence_json, c.created_at AS concluded_at
           FROM diagnostic_sheets s
           LEFT JOIN diagnostic_conclusions c
             ON c.id = (SELECT MAX(id) FROM diagnostic_conclusions
                        WHERE sheet_id = s.id)
           ORDER BY s.id"""
    ).fetchall()
    result = []
    for r in rows:
        item = {
            "id": r["id"],
            "device_id": r["device_id"],
            "fw_version": r["fw_version"],
            "entries": json.loads(r["entries_json"]),
            "created_at": r["created_at"],
        }
        if r["conclusion_id"] is not None:
            item["latest"] = {
                "id": r["conclusion_id"],
                "conclusion": r["conclusion"],
                "detail": json.loads(r["detail_json"])
                if r["detail_json"]
                else None,
                "evidence": json.loads(r["evidence_json"]),
                "created_at": r["concluded_at"],
            }
        else:
            item["latest"] = None
        result.append(item)
    return result


def get_diagnostic_sheet(db, sheet_id):
    sheet = db.execute(
        "SELECT id, device_id, fw_version, entries_json, created_at"
        " FROM diagnostic_sheets WHERE id = ?",
        (sheet_id,),
    ).fetchone()
    if sheet is None:
        return None
    rows = db.execute(
        """SELECT id, conclusion, detail_json, evidence_json, created_at
           FROM diagnostic_conclusions WHERE sheet_id = ? ORDER BY id""",
        (sheet_id,),
    ).fetchall()
    return {
        "sheet": {
            "id": sheet["id"],
            "device_id": sheet["device_id"],
            "fw_version": sheet["fw_version"],
            "entries": json.loads(sheet["entries_json"]),
            "created_at": sheet["created_at"],
        },
        "history": [_conclusion_to_dict(r) for r in rows],
    }
