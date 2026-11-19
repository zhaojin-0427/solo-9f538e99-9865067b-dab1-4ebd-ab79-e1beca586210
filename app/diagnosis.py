"""Pure diagnostic logic for word-order vs. stuck-bit triage.

A diagnostic sheet binds one device + one firmware version to 2..20 frame
numbers, each with the 8-hex-digit IEEE-754 single-precision bit pattern the
bench expects.  Evaluation never mutates frames or the firmware registry: it
only reassembles the stored raw word with the registered word order and
compares.

Conclusion codes:

* ``normal``             every reassembled pattern already equals expected;
* ``word_order_suspect`` every sample matches only with the opposite order;
* ``stuck_bit``          one data line forced to 0/1 explains every sample;
* ``inconclusive``       both explanations hold at once;
* ``unexplainable``      neither explanation holds;
* ``pending``            a frame is missing or the firmware is not registered
                         (or the stored frame carries another firmware).
"""

import json

from . import decoder

MIN_ENTRIES = 2
MAX_ENTRIES = 20
MIN_DISTINCT_PATTERNS = 2
ALL_BITS = 0xFFFFFFFF

# Conclusion code -> Chinese label used by the UI.
CONCLUSION_LABELS = {
    "normal": "正常",
    "word_order_suspect": "字序嫌疑",
    "stuck_bit": "卡位",
    "inconclusive": "无法区分",
    "unexplainable": "无法解释",
    "pending": "待补",
}

PENDING_REASON_LABELS = {
    "missing_frame": "缺帧",
    "firmware_mismatch": "帧固件号与诊断单不符",
    "firmware_unregistered": "固件待登记",
}


class SheetValidationError(ValueError):
    """The diagnostic sheet itself is malformed; nothing is stored."""


def parse_sheet_input(device_id, fw_version, lines):
    """Validate raw sheet input.

    ``lines`` is a sequence of raw text lines, each holding
    ``帧号, 预期位型`` (comma / whitespace / colon separated).  Returns
    ``(device_id, fw_version, [{"frame_no": int, "expected_pattern": str}])``
    or raises :class:`SheetValidationError`.
    """
    device_id = (device_id or "").strip()
    fw_version = (fw_version or "").strip()
    if not device_id:
        raise SheetValidationError("设备号不能为空")
    if len(device_id) > 128:
        raise SheetValidationError("设备号过长")
    if not fw_version:
        raise SheetValidationError("固件号不能为空")
    if len(fw_version) > 128:
        raise SheetValidationError("固件号过长")

    entries = []
    seen_frames = set()
    for line_no, raw_line in enumerate(lines, start=1):
        line = (raw_line or "").strip()
        if not line:
            continue
        for sep in ("\t", ";", " ", ":"):
            line = line.replace(sep, ",")
        parts = [p.strip() for p in line.split(",") if p.strip() != ""]
        if len(parts) != 2:
            raise SheetValidationError(
                "第 %d 行：应为「帧号, 预期位型」两列，实际 %d 列"
                % (line_no, len(parts))
            )
        frame_no_text, pattern = parts
        if not decoder.FRAME_NO_RE.match(frame_no_text):
            raise SheetValidationError(
                "第 %d 行：帧号必须是非负整数" % line_no
            )
        frame_no = int(frame_no_text)
        if frame_no >= decoder.MAX_FRAME_NO:
            raise SheetValidationError("第 %d 行：帧号超出范围" % line_no)
        if not decoder.HEX8_RE.match(pattern):
            raise SheetValidationError(
                "第 %d 行：预期位型必须是 8 位十六进制（如 3F800000）" % line_no
            )
        if frame_no in seen_frames:
            raise SheetValidationError(
                "第 %d 行：帧号 %d 在诊断单中重复" % (line_no, frame_no)
            )
        seen_frames.add(frame_no)
        entries.append(
            {"frame_no": frame_no, "expected_pattern": pattern.upper()}
        )

    if not MIN_ENTRIES <= len(entries) <= MAX_ENTRIES:
        raise SheetValidationError(
            "诊断单须包含 %d～%d 个帧号，实际 %d 个"
            % (MIN_ENTRIES, MAX_ENTRIES, len(entries))
        )
    distinct = {e["expected_pattern"] for e in entries}
    if len(distinct) < MIN_DISTINCT_PATTERNS:
        raise SheetValidationError(
            "预期位型须至少 %d 种，实际只有 %d 种"
            % (MIN_DISTINCT_PATTERNS, len(distinct))
        )
    return device_id, fw_version, entries


def find_stuck_bit(pairs):
    """Find a single bit forced to 0 or 1 that explains every sample.

    ``pairs`` is a list of ``(actual_pattern, expected_pattern)`` 32-bit
    ints (already known to differ somewhere).  Returns
    ``(bit_position, stuck_value, mask)`` -- bit 31 is the MSB -- or
    ``None``.  The fault must actually manifest: at least one expected
    sample carries the opposite value at that bit.
    """
    for position in range(31, -1, -1):
        mask = 1 << position
        rest = ALL_BITS ^ mask
        for stuck_value in (0, 1):
            forced_bit = stuck_value << position
            manifests = False
            for actual, expected in pairs:
                # Every other bit must already agree, and the suspect bit is
                # present in the actual pattern exactly as the stuck line.
                if (actual & rest) != (expected & rest):
                    break
                if (actual & mask) != forced_bit:
                    break
                if (expected & mask) != forced_bit:
                    manifests = True
            else:
                if manifests:
                    return position, stuck_value, mask
    return None


def _opposite(order):
    return "low_first" if order == "high_first" else "high_first"


def _pending_evidence(entry, reason, raw_word, stored_fw, registered_order):
    return {
        "frame_no": entry["frame_no"],
        "expected_pattern": entry["expected_pattern"],
        "status": reason,
        "raw_word": raw_word,
        "stored_fw_version": stored_fw,
        "registered_order": registered_order,
        "assembled_pattern": None,
        "swapped_pattern": None,
    }


def evaluate(fw_version, entries, frame_lookup, registered_order):
    """Evaluate one sheet against current committed frame state.

    ``entries``: ``[{"frame_no": int, "expected_pattern": "XXXXXXXX"}]``.
    ``frame_lookup``: ``{frame_no: {"raw_word": str, "fw_version": str}}``
    for stored frames; missing keys are absent frames.
    ``registered_order``: the firmware registry's immutable word order,
    or ``None`` when the firmware is not registered yet.

    Returns ``{"code", "detail", "evidence"}`` where evidence is a list of
    per-entry snapshots supporting the conclusion.  Pure: no I/O and no
    mutation of any frame or registry row.
    """
    evidence = []
    pending_reasons = []
    comparable = []  # (frame_no, raw, actual, expected, swapped)

    for entry in entries:
        frame_no = entry["frame_no"]
        expected_text = entry["expected_pattern"]
        expected = int(expected_text, 16)
        stored = frame_lookup.get(frame_no)

        if stored is None:
            reason = "missing_frame"
            pending_reasons.append({"frame_no": frame_no, "reason": reason})
            evidence.append(
                _pending_evidence(entry, reason, None, None, registered_order)
            )
            continue

        raw_word = stored["raw_word"]
        stored_fw = stored["fw_version"]
        if stored_fw != fw_version:
            reason = "firmware_mismatch"
            pending_reasons.append({"frame_no": frame_no, "reason": reason})
            evidence.append(
                _pending_evidence(
                    entry, reason, raw_word, stored_fw, registered_order
                )
            )
            continue

        if registered_order is None:
            reason = "firmware_unregistered"
            pending_reasons.append({"frame_no": frame_no, "reason": reason})
            evidence.append(
                _pending_evidence(
                    entry, reason, raw_word, stored_fw, None
                )
            )
            continue

        actual = decoder.assemble(raw_word, registered_order)
        swapped = decoder.assemble(raw_word, _opposite(registered_order))
        comparable.append((frame_no, raw_word, actual, expected, swapped))
        evidence.append(
            {
                "frame_no": frame_no,
                "expected_pattern": expected_text,
                "status": "comparable",
                "raw_word": raw_word,
                "stored_fw_version": stored_fw,
                "registered_order": registered_order,
                "assembled_pattern": "%08X" % actual,
                "swapped_pattern": "%08X" % swapped,
            }
        )

    if pending_reasons:
        return {
            "code": "pending",
            "detail": {"pending_reasons": pending_reasons},
            "evidence": evidence,
        }

    all_match = all(
        actual == expected for _, _, actual, expected, _ in comparable
    )
    if all_match:
        return {"code": "normal", "detail": None, "evidence": evidence}

    swap_match = all(
        swapped == expected for _, _, _, expected, swapped in comparable
    )
    stuck = find_stuck_bit(
        [(actual, expected) for _, _, actual, expected, _ in comparable]
    )

    if swap_match and stuck is not None:
        position, stuck_value, mask = stuck
        return {
            "code": "inconclusive",
            "detail": {
                "word_order": {
                    "registered_order": registered_order,
                    "opposite_order": _opposite(registered_order),
                },
                "stuck_bit": {
                    "bit_position": position,
                    "stuck_value": stuck_value,
                    "bit_mask": "%08X" % mask,
                },
            },
            "evidence": evidence,
        }
    if swap_match:
        return {
            "code": "word_order_suspect",
            "detail": {
                "registered_order": registered_order,
                "opposite_order": _opposite(registered_order),
            },
            "evidence": evidence,
        }
    if stuck is not None:
        position, stuck_value, mask = stuck
        return {
            "code": "stuck_bit",
            "detail": {
                "bit_position": position,
                "stuck_value": stuck_value,
                "bit_mask": "%08X" % mask,
            },
            "evidence": evidence,
        }
    return {"code": "unexplainable", "detail": None, "evidence": evidence}


def canonical_json(obj):
    """Stable JSON text so identical conclusions compare byte-for-byte."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
