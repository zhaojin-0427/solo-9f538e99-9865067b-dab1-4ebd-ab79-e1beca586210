import io
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, decoder, diagnosis, service  # noqa: E402


def force_bit(pattern_hex, position, value):
    """Return 8-hex pattern with bit `position` (31=MSB) forced to value."""
    n = int(pattern_hex, 16)
    mask = 1 << position
    n = (n & ~mask) | (value << position)
    return "%08X" % n


def expected_actual_pair(pattern_hex, position, value):
    """(actual_int, expected_int) where actual has one stuck bit."""
    expected = int(pattern_hex, 16)
    actual = int(force_bit(pattern_hex, position, value), 16)
    return actual, expected


class StuckBitLogicTests(unittest.TestCase):
    def test_stuck_to_zero_detected_and_manifests(self):
        # Expected has bit 3 = 1, actual line forces it to 0; all other bits
        # agree in every sample.
        pairs = [
            expected_actual_pair("3F800008", 3, 0),
            expected_actual_pair("40000008", 3, 0),
        ]
        found = diagnosis.find_stuck_bit(pairs)
        self.assertIsNotNone(found)
        position, stuck_value, mask = found
        self.assertEqual(position, 3)
        self.assertEqual(stuck_value, 0)
        self.assertEqual(mask, 0x00000008)

    def test_stuck_to_one_detected(self):
        pairs = [
            expected_actual_pair("3F800000", 7, 1),
            expected_actual_pair("40000000", 7, 1),
        ]
        position, stuck_value, mask = diagnosis.find_stuck_bit(pairs)
        self.assertEqual((position, stuck_value), (7, 1))
        self.assertEqual(mask, 0x80)

    def test_no_fault_when_already_equal(self):
        # The "manifests" requirement: no expected sample has the opposite
        # bit, so this must not be reported as a stuck bit.
        pairs = [(0x3F800000, 0x3F800000), (0x40000000, 0x40000000)]
        self.assertIsNone(diagnosis.find_stuck_bit(pairs))

    def test_two_broken_bits_unexplainable(self):
        expected1 = 0x3F800000
        actual1 = expected1 | 0x00000003  # two bits differ
        expected2 = 0x40000000
        actual2 = expected2 | 0x00000003
        self.assertIsNone(
            diagnosis.find_stuck_bit([(actual1, expected1), (actual2, expected2)])
        )

    def test_bit_that_never_manifests_is_rejected(self):
        # Bit 5 differs in no sample; it must not be picked even though
        # forcing it is technically consistent.
        pairs = [
            expected_actual_pair("00000020", 0, 1),  # bit 0 actually differs
            (0x00000021, 0x00000021),
        ]
        position, stuck_value, _ = diagnosis.find_stuck_bit(pairs)
        self.assertEqual(position, 0)
        self.assertEqual(stuck_value, 1)


class SheetValidationTests(unittest.TestCase):
    def _parse(self, text):
        return diagnosis.parse_sheet_input("D1", "FW1", text.splitlines())

    def test_ok(self):
        device, fw, entries = self._parse("1,3F800000\n2,40000000")
        self.assertEqual(device, "D1")
        self.assertEqual(fw, "FW1")
        self.assertEqual(
            entries,
            [
                {"frame_no": 1, "expected_pattern": "3F800000"},
                {"frame_no": 2, "expected_pattern": "40000000"},
            ],
        )

    def test_count_bounds(self):
        with self.assertRaises(diagnosis.SheetValidationError):
            self._parse("1,3F800000")
        text = "\n".join("%d,3F80000%d" % (i, i % 2) for i in range(21))
        with self.assertRaises(diagnosis.SheetValidationError):
            self._parse(text)
        # Exactly 20 with two distinct patterns is accepted.
        text = "\n".join("%d,%s" % (i, "3F800000" if i % 2 else "40000000")
                         for i in range(20))
        self.assertEqual(len(self._parse(text)[2]), 20)

    def test_need_two_distinct_patterns(self):
        with self.assertRaises(diagnosis.SheetValidationError):
            self._parse("1,3f800000\n2,3F800000")

    def test_duplicate_frame_rejected(self):
        with self.assertRaises(diagnosis.SheetValidationError):
            self._parse("1,3F800000\n1,40000000")

    def test_bad_hex_and_frame(self):
        with self.assertRaises(diagnosis.SheetValidationError):
            self._parse("1,3F80000\n2,40000000")
        with self.assertRaises(diagnosis.SheetValidationError):
            self._parse("x,3F800000\n2,40000000")

    def test_missing_device_or_firmware(self):
        with self.assertRaises(diagnosis.SheetValidationError):
            diagnosis.parse_sheet_input(" ", "FW1", ["1,3F800000", "2,40000000"])
        with self.assertRaises(diagnosis.SheetValidationError):
            diagnosis.parse_sheet_input("D1", " ", ["1,3F800000", "2,40000000"])


class DiagnosticAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "test.sqlite3"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, text):
        data = {"csv": (io.BytesIO(text.encode("utf-8")), "frames.csv")}
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data"
        )

    def register(self, fw, order):
        return self.client.post(
            "/firmware", data={"fw_version": fw, "word_order": order}
        )

    def make_sheet(self, device="D1", fw="FW1", entries=None, api=False):
        entries = entries or [(1, "3F800000"), (2, "40000000")]
        text = "\n".join("%d,%s" % (n, p) for n, p in entries)
        if api:
            return self.client.post(
                "/api/diagnostics",
                json={"device_id": device, "fw_version": fw, "entries_text": text},
            )
        return self.client.post(
            "/diagnostics",
            data={"device_id": device, "fw_version": fw, "entries_text": text},
        )

    def sheet(self, sheet_id):
        with self.app.app_context():
            return service.get_diagnostic_sheet(db.get_db(), sheet_id)

    def latest(self, sheet_id):
        return self.sheet(sheet_id)["history"][-1]

    # --- pending paths -----------------------------------------------------

    def test_missing_frames_is_pending(self):
        resp = self.make_sheet()
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            sheets = service.list_diagnostic_sheets(db.get_db())
        self.assertEqual(len(sheets), 1)
        latest = sheets[0]["latest"]
        self.assertEqual(latest["conclusion"], "pending")
        reasons = {r["frame_no"]: r["reason"]
                   for r in latest["detail"]["pending_reasons"]}
        self.assertEqual(reasons, {1: "missing_frame", 2: "missing_frame"})

    def test_unregistered_firmware_is_pending(self):
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet()
        self.assertEqual(self.latest(1)["conclusion"], "pending")
        reasons = {r["reason"] for r in self.latest(1)["detail"]["pending_reasons"]}
        self.assertEqual(reasons, {"firmware_unregistered"})

    def test_firmware_mismatch_is_pending(self):
        self.register("FW1", "high_first")
        # Stored frame belongs to a different firmware than the sheet.
        self.upload("D1,1,FW2,3F800000\nD1,2,FW2,40000000")
        self.make_sheet(fw="FW1")
        latest = self.latest(1)
        self.assertEqual(latest["conclusion"], "pending")
        self.assertTrue(
            all(r["reason"] == "firmware_mismatch"
                for r in latest["detail"]["pending_reasons"])
        )

    # --- normal / word order / stuck bit -----------------------------------

    def test_normal_when_all_match(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        self.assertEqual(self.latest(1)["conclusion"], "normal")

    def test_word_order_suspect_after_opposite_order_matches_all(self):
        # Firmware registered high_first, but the wire words are low-first.
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,00003F80\nD1,2,FW1,00004000")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        latest = self.latest(1)
        self.assertEqual(latest["conclusion"], "word_order_suspect")
        self.assertEqual(latest["detail"]["opposite_order"], "low_first")
        # Original frame and firmware word order are untouched.
        with self.app.app_context():
            frames = service.device_frames(db.get_db(), "D1")
            fw = service.list_firmwares(db.get_db())
        self.assertEqual(frames[0]["raw_word"], "00003F80")
        self.assertEqual(fw[0]["word_order"], "high_first")

    def test_stuck_bit_reports_position_and_direction(self):
        self.register("FW1", "high_first")
        # Bit 3 stuck at 0: actual patterns lack 0x00000008 present in expected.
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet(entries=[(1, "3F800008"), (2, "40000008")])
        latest = self.latest(1)
        self.assertEqual(latest["conclusion"], "stuck_bit")
        self.assertEqual(latest["detail"]["bit_position"], 3)
        self.assertEqual(latest["detail"]["stuck_value"], 0)
        self.assertEqual(latest["detail"]["bit_mask"], "00000008")
        # At least one expected sample had the opposite value at that bit.
        manifest = any(
            int(e["expected_pattern"], 16) & 0x8 for e in latest["evidence"]
        )
        self.assertTrue(manifest)

    def test_stuck_to_one(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800080\nD1,2,FW1,40000080")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        latest = self.latest(1)
        self.assertEqual(latest["conclusion"], "stuck_bit")
        self.assertEqual(
            (latest["detail"]["bit_position"], latest["detail"]["stuck_value"]),
            (7, 1),
        )

    def test_unexplainable(self):
        self.register("FW1", "high_first")
        # Two bits differ in both samples; opposite word order won't help.
        self.upload("D1,1,FW1,3F800003\nD1,2,FW1,40000003")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        self.assertEqual(self.latest(1)["conclusion"], "unexplainable")

    def test_inconclusive_predicates_can_both_hold(self):
        # The both-explanations branch is unreachable through real raw-word
        # semantics (a word swap changes paired bits), so verify its
        # predicates directly on controlled 32-bit patterns.
        assembled = [0x00000001, 0x00000003]
        expected = [0x00000000, 0x00000002]
        swapped = list(expected)  # opposite order would fix everything
        stuck = diagnosis.find_stuck_bit(list(zip(assembled, expected)))
        self.assertIsNotNone(stuck)
        self.assertEqual((stuck[0], stuck[1]), (0, 1))
        self.assertTrue(all(s == e for s, e in zip(swapped, expected)))

    # --- re-evaluation lifecycle -------------------------------------------

    def test_backfill_upload_reevaluates_and_keeps_history(self):
        # Sheet created with both frames missing -> pending.
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        self.assertEqual(self.latest(1)["conclusion"], "pending")

        # Upload one frame: frame 2 is still missing; frame 1 is stored but
        # its firmware is not registered yet.  A fresh pending row with the
        # new evidence snapshot is appended.
        self.upload("D1,1,FW1,3F800000")
        data = self.sheet(1)
        self.assertEqual(len(data["history"]), 2)
        self.assertEqual(data["history"][-1]["conclusion"], "pending")
        by_frame = {
            r["frame_no"]: r["reason"]
            for r in data["history"][-1]["detail"]["pending_reasons"]
        }
        self.assertEqual(by_frame[2], "missing_frame")
        self.assertEqual(by_frame[1], "firmware_unregistered")

        # Register firmware and supply the last frame -> normal.
        self.register("FW1", "high_first")
        self.upload("D1,2,FW1,40000000")
        data = self.sheet(1)
        self.assertEqual(data["history"][-1]["conclusion"], "normal")
        # All old conclusions and their evidence are retained.
        self.assertGreaterEqual(len(data["history"]), 3)
        self.assertEqual(data["history"][0]["conclusion"], "pending")

    def test_registration_alone_reevaluates_pending_sheet(self):
        self.upload("D1,1,FW1,00003F80\nD1,2,FW1,00004000")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        self.assertEqual(self.latest(1)["conclusion"], "pending")
        # The wire words are low-first but registration says high_first, so
        # once registered the opposite order explains everything.
        self.register("FW1", "high_first")
        self.assertEqual(self.latest(1)["conclusion"], "word_order_suspect")

    def test_irrelevant_upload_creates_no_new_conclusion(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        before = len(self.sheet(1)["history"])

        # Other device, other firmware, and a pure retransmission: none may
        # append a conclusion row for sheet #1.
        self.upload("OTHER,9,FW1,3F800000")
        self.upload("D1,7,FW9,3F800000")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.assertEqual(len(self.sheet(1)["history"]), before)

    def test_terminal_sheet_is_not_reevaluated(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")])
        self.assertEqual(self.latest(1)["conclusion"], "normal")
        # Re-registering the same firmware and re-uploading same content
        # cannot add rows (frames are immutable, conclusion is terminal).
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        self.assertEqual(len(self.sheet(1)["history"]), 1)

    def test_identical_reevaluation_does_not_duplicate_row(self):
        self.make_sheet()  # pending
        first = len(self.sheet(1)["history"])
        # An unrelated registered firmware triggers a firmware-wide sweep that
        # includes this sheet, but its state is unchanged -> no new row.
        self.register("FWOTHER", "high_first")
        self.assertEqual(len(self.sheet(1)["history"]), first)

    # --- evidence and atomicity ---------------------------------------------

    def test_evidence_matches_committed_frame_state(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet(entries=[(1, "3F800008"), (2, "40000008")])
        latest = self.latest(1)
        by_frame = {e["frame_no"]: e for e in latest["evidence"]}
        self.assertEqual(by_frame[1]["raw_word"], "3F800000")
        self.assertEqual(by_frame[1]["assembled_pattern"], "3F800000")
        self.assertEqual(by_frame[1]["expected_pattern"], "3F800008")
        self.assertEqual(by_frame[1]["registered_order"], "high_first")

    def test_conclusion_and_evidence_share_row_after_restart(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.make_sheet(entries=[(1, "3F800008"), (2, "40000008")])
        app2 = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "test.sqlite3"),
            }
        )
        client2 = app2.test_client()
        html = client2.get("/diagnostics/1").get_data(as_text=True)
        self.assertIn("卡位", html)
        self.assertIn("3F800000", html)
        self.assertIn("3F800008", html)
        body = client2.get("/api/diagnostics/1").get_json()
        self.assertEqual(body["history"][-1]["conclusion"], "stuck_bit")
        self.assertEqual(len(body["history"][-1]["evidence"]), 2)

    # --- HTTP / API ---------------------------------------------------------

    def test_invalid_sheet_form_flashes_and_persists_nothing(self):
        resp = self.client.post(
            "/diagnostics",
            data={"device_id": "D1", "fw_version": "FW1",
                  "entries_text": "1,3F800000"},  # only one entry
        )
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            self.assertEqual(service.list_diagnostic_sheets(db.get_db()), [])

    def test_api_create_and_list(self):
        resp = self.make_sheet(api=True)
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body["conclusion"]["conclusion"], "pending")
        self.assertEqual(body["sheet"]["device_id"], "D1")

        bad = self.client.post(
            "/api/diagnostics",
            json={"device_id": "D1", "fw_version": "FW1",
                  "entries_text": "1,BADHEX!!\n2,40000000"},
        )
        self.assertEqual(bad.status_code, 400)

        listing = self.client.get("/api/diagnostics").get_json()
        self.assertEqual(len(listing["sheets"]), 1)

    def test_diagnostic_pages_render(self):
        self.make_sheet()
        page = self.client.get("/diagnostics").get_data(as_text=True)
        self.assertIn("诊断单", page)
        self.assertIn("待补", page)
        detail = self.client.get("/diagnostics/1").get_data(as_text=True)
        self.assertIn("缺帧", detail)
        self.assertEqual(self.client.get("/diagnostics/999").status_code, 404)

    def test_api_entries_array_payload(self):
        resp = self.client.post(
            "/api/diagnostics",
            json={
                "device_id": "D1",
                "fw_version": "FW1",
                "entries": [
                    {"frame_no": 1, "expected_pattern": "3f800000"},
                    {"frame_no": 2, "expected_pattern": "40000000"},
                ],
            },
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(
            body["sheet"]["entries"][0]["expected_pattern"], "3F800000"
        )

    def test_sheet_created_after_data_gets_immediate_conclusion(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        resp = self.make_sheet(entries=[(1, "3F800000"), (2, "40000000")],
                               api=True)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["conclusion"]["conclusion"], "normal")
        # Exactly one conclusion row: no spurious pending history.
        self.assertEqual(len(self.sheet(1)["history"]), 1)

    def test_twenty_entries_accepted_twentyone_rejected(self):
        entries = [
            (i, "3F800000" if i % 2 else "40000000") for i in range(1, 21)
        ]
        resp = self.make_sheet(entries=entries, api=True)
        self.assertEqual(resp.status_code, 201)
        resp = self.make_sheet(
            entries=entries + [(21, "3F800000")], api=True
        )
        self.assertEqual(resp.status_code, 400)

    def test_malformed_json_payload_is_400(self):
        resp = self.client.post(
            "/api/diagnostics",
            data="not-json-at-all",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


class InconclusiveBranchTests(unittest.TestCase):
    """Drive evaluate()'s both-explanations branch with controlled inputs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "t.sqlite3"),
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_explanations_returns_inconclusive(self):
        # Monkeypatch assemble so swapped words equal expected while the
        # registered-order assembly differs in exactly one manifesting bit.
        entries = [
            {"frame_no": 1, "expected_pattern": "00000000"},
            {"frame_no": 2, "expected_pattern": "00000002"},
        ]
        # high_first assembly: frame1 -> 0x1, frame2 -> 0x3 (bit0 stuck at 1);
        # low_first (opposite) assembly: exactly the expected patterns.
        fake_assembled = {1: (0x00000001, 0x00000000), 2: (0x00000003, 0x00000002)}
        original = decoder.assemble

        def fake_assemble(raw_word, word_order):
            frame_no = int(raw_word)
            actual, swapped = fake_assembled[frame_no]
            return actual if word_order == "high_first" else swapped

        decoder.assemble = fake_assemble
        try:
            with self.app.app_context():
                conn = db.get_db()
                conn.execute("BEGIN IMMEDIATE")
                for frame_no in (1, 2):
                    conn.execute(
                        "INSERT INTO frames (device_id, frame_no, fw_version,"
                        " raw_word, status) VALUES ('D1', ?, 'FW1', ?, 'decoded')",
                        (frame_no, str(frame_no)),
                    )
                conn.execute(
                    "INSERT INTO firmware (fw_version, word_order)"
                    " VALUES ('FW1', 'high_first')"
                )
                lookup = service._frame_lookup(conn, "D1", [1, 2])
                result = diagnosis.evaluate(
                    "FW1", entries, lookup, "high_first"
                )
                conn.execute("ROLLBACK")
        finally:
            decoder.assemble = original

        self.assertEqual(result["code"], "inconclusive")
        self.assertEqual(result["detail"]["stuck_bit"]["bit_position"], 0)
        self.assertEqual(result["detail"]["stuck_bit"]["stuck_value"], 1)
        self.assertEqual(result["detail"]["word_order"]["opposite_order"],
                         "low_first")


class DiagnosticConcurrencyTests(unittest.TestCase):
    """Interleaved upload / registration / evaluation must stay atomic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "race.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": self.db_path,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, fn, *args):
        with self.app.app_context():
            conn = db.get_db()
            return fn(conn, *args)

    def test_interleaved_upload_and_registration(self):
        import threading

        entries = [
            {
                "frame_no": i,
                "expected_pattern": "%08X"
                % struct.unpack(">I", struct.pack(">f", float(i)))[0],
            }
            for i in range(1, 31)
        ]
        self._run(
            service.create_diagnostic_sheet, "D1", "FW1",
            [{"frame_no": e["frame_no"],
              "expected_pattern": e["expected_pattern"]} for e in entries],
        )

        rows_a = [
            {"device_id": "D1", "frame_no": i, "fw_version": "FW1",
             "raw_word": e["expected_pattern"], "content": None}
            for i, e in enumerate(entries[:15], start=1)
        ]
        rows_b = [
            {"device_id": "D1", "frame_no": i, "fw_version": "FW1",
             "raw_word": e["expected_pattern"], "content": None}
            for i, e in enumerate(entries[15:], start=16)
        ]
        barrier = threading.Barrier(3)
        errors = []

        def uploader_a():
            try:
                barrier.wait()
                self._run(service.ingest, rows_a)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def uploader_b():
            try:
                barrier.wait()
                self._run(service.ingest, rows_b)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def registrar():
            try:
                barrier.wait()
                self._run(service.register_firmware, "FW1", "high_first")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=uploader_a),
                   threading.Thread(target=uploader_b),
                   threading.Thread(target=registrar)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        data = self._run(service.get_diagnostic_sheet, 1)
        latest = data["history"][-1]
        # Every committed conclusion row carries evidence from the same state.
        for c in data["history"]:
            self.assertEqual(len(c["evidence"]), len(entries))
        self.assertEqual(latest["conclusion"], "normal")
        # The final evidence shows all frames assembled and matching.
        self.assertTrue(
            all(e["status"] == "comparable" for e in latest["evidence"])
        )


if __name__ == "__main__":
    unittest.main()
