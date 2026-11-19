"""HTTP routes: HTML pages for the data clerk plus a small JSON API."""

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, decoder, diagnosis, service

bp = Blueprint("main", __name__)

STATUS_LABELS = {
    "decoded": "有效",
    "invalid": "无效",
    "pending": "待解",
    "reused": "沿用",
}


@bp.app_template_filter("status_label")
def status_label(status):
    return STATUS_LABELS.get(status, status)


@bp.app_template_filter("conclusion_label")
def conclusion_label(code):
    return diagnosis.CONCLUSION_LABELS.get(code, code)


@bp.app_template_filter("pending_reason_label")
def pending_reason_label(code):
    return diagnosis.PENDING_REASON_LABELS.get(code, code)


@bp.app_template_filter("reading_fmt")
def reading_fmt(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return format(value, ".9g")


def _csv_text_from_request():
    """Pull CSV text out of a file upload, a form field, or a raw body."""
    for _field, storage in request.files.items():
        if storage is not None:
            try:
                return storage.read().decode("utf-8")
            except UnicodeDecodeError:
                raise decoder.ValidationError("CSV 必须是 UTF-8 编码")

    if request.form:
        for key in ("csv", "data", "content"):
            if key in request.form and request.form[key].strip():
                return request.form[key]

    raw = request.get_data(cache=True)
    if raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise decoder.ValidationError("CSV 必须是 UTF-8 编码")

    raise decoder.ValidationError("请选择 CSV 文件")


def _run_upload(text):
    """Parse and ingest; return (summary, stored frames, inserted keys).

    Raises decoder.ValidationError or service.BatchRejected.
    """
    rows = decoder.parse_csv(text)
    conn = db.get_db()
    summary = service.ingest(conn, rows)

    # Report the committed state rather than values computed during insert: a
    # concurrent firmware registration may already have decoded these frames.
    with db.read_snapshot(conn):
        stored = service.batch_frames(conn, rows)

    inserted = set(summary["inserted_keys"])
    for status, label in (
        ("decoded", "decoded"),
        ("invalid", "invalid"),
        ("pending", "pending"),
    ):
        summary[label] = sum(
            1
            for f in stored
            if (f["device_id"], f["frame_no"]) in inserted and f["status"] == status
        )
    return summary, stored, inserted


@bp.get("/")
def index():
    conn = db.get_db()
    with db.read_snapshot(conn):
        devices = service.list_devices(conn)
        firmwares = service.list_firmwares(conn)
    return render_template(
        "index.html",
        devices=devices,
        firmwares=firmwares,
        word_orders=decoder.WORD_ORDERS,
        max_batch=decoder.MAX_BATCH,
    )


@bp.post("/upload")
def upload():
    try:
        summary, stored, inserted = _run_upload(_csv_text_from_request())
    except decoder.ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    except service.BatchRejected as exc:
        flash(str(exc), "error")
        return render_template(
            "upload_rejected.html", message=str(exc), conflicts=exc.conflicts
        )

    flash(
        "上传完成：共 %d 帧，新增 %d（有效 %d，无效 %d，待解 %d），沿用 %d。"
        % (
            summary["received"],
            summary["inserted"],
            summary["decoded"],
            summary["invalid"],
            summary["pending"],
            summary["reused"],
        ),
        "success",
    )
    return render_template(
        "upload_result.html",
        summary=summary,
        frames=stored,
        inserted=inserted,
    )


@bp.post("/firmware")
def register_firmware():
    fw_version = (request.form.get("fw_version") or "").strip()
    word_order = (request.form.get("word_order") or "").strip()
    if not fw_version:
        flash("固件号不能为空", "error")
        return redirect(url_for("main.index"))

    try:
        result = service.register_firmware(db.get_db(), fw_version, word_order)
    except service.ConflictError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))

    if result["already_registered"]:
        if result["backfilled"]:
            flash(
                "固件 %s 已登记为「%s」，本次补解遗留待解帧 %d 帧。"
                % (
                    fw_version,
                    decoder.WORD_ORDERS[result["word_order"]],
                    result["backfilled"],
                ),
                "success",
            )
        else:
            flash("固件 %s 已登记，字序未变。" % fw_version, "info")
    else:
        flash(
            "固件 %s 已登记为「%s」，补解 %d 帧（有效 %d，无效 %d）。"
            % (
                fw_version,
                decoder.WORD_ORDERS[result["word_order"]],
                result["backfilled"],
                result["decoded"],
                result["invalid"],
            ),
            "success",
        )
    return redirect(url_for("main.index"))


@bp.get("/devices/<path:device_id>")
def device(device_id):
    conn = db.get_db()
    with db.read_snapshot(conn):
        frames = service.device_frames(conn, device_id)
    if not frames:
        abort(404)
    return render_template(
        "device.html",
        device_id=device_id,
        frames=frames,
        word_orders=decoder.WORD_ORDERS,
    )


# --- Diagnostic sheets ----------------------------------------------------


def _sheet_params_from(payload):
    """Pull (device_id, fw_version, entries text) from form or JSON payload."""
    if not payload:
        raise diagnosis.SheetValidationError("请求内容为空")
    device_id = (payload.get("device_id") or "").strip()
    fw_version = (payload.get("fw_version") or "").strip()
    entries_text = payload.get("entries_text")
    if entries_text is None:
        entries = payload.get("entries")
        if isinstance(entries, list):
            entries_text = "\n".join(
                "%s,%s"
                % (item.get("frame_no", ""), item.get("expected_pattern", ""))
                for item in entries
            )
        else:
            entries_text = ""
    if hasattr(entries_text, "read"):
        entries_text = entries_text.read()
    if isinstance(entries_text, bytes):
        entries_text = entries_text.decode("utf-8", errors="replace")
    elif not isinstance(entries_text, str):
        raise diagnosis.SheetValidationError("entries_text 必须是文本")
    return device_id, fw_version, entries_text.splitlines()


def _create_sheet(payload):
    device_id, fw_version, lines = _sheet_params_from(payload)
    device_id, fw_version, entries = diagnosis.parse_sheet_input(
        device_id, fw_version, lines
    )
    return service.create_diagnostic_sheet(db.get_db(), device_id, fw_version, entries)


@bp.get("/diagnostics")
def diagnostics():
    conn = db.get_db()
    with db.read_snapshot(conn):
        sheets = service.list_diagnostic_sheets(conn)
    return render_template(
        "diagnostics.html",
        sheets=sheets,
        min_entries=diagnosis.MIN_ENTRIES,
        max_entries=diagnosis.MAX_ENTRIES,
        min_distinct=diagnosis.MIN_DISTINCT_PATTERNS,
    )


@bp.post("/diagnostics")
def create_diagnostic():
    try:
        result = _create_sheet(request.form)
    except diagnosis.SheetValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.diagnostics"))
    sheet = result["sheet"]
    flash(
        "诊断单 #%d 已建立，当前结论：%s。"
        % (sheet["id"], diagnosis.CONCLUSION_LABELS[result["conclusion"]["conclusion"]]),
        "success",
    )
    return redirect(url_for("main.diagnostic_detail", sheet_id=sheet["id"]))


@bp.get("/diagnostics/<int:sheet_id>")
def diagnostic_detail(sheet_id):
    conn = db.get_db()
    with db.read_snapshot(conn):
        data = service.get_diagnostic_sheet(conn, sheet_id)
    if data is None:
        abort(404)
    return render_template("diagnostic_detail.html", data=data)


# --- JSON API -------------------------------------------------------------


@bp.get("/healthz")
def healthz():
    conn = db.get_db()
    conn.execute("SELECT 1 FROM firmware LIMIT 1").fetchall()
    return jsonify({"status": "ok"})


@bp.get("/api/devices")
def api_devices():
    conn = db.get_db()
    with db.read_snapshot(conn):
        return jsonify({"devices": service.list_devices(conn)})


@bp.get("/api/devices/<path:device_id>")
def api_device(device_id):
    conn = db.get_db()
    with db.read_snapshot(conn):
        frames = service.device_frames(conn, device_id)
    if not frames:
        abort(404)
    return jsonify({"device_id": device_id, "frames": frames})


@bp.get("/api/firmwares")
def api_firmwares():
    conn = db.get_db()
    with db.read_snapshot(conn):
        return jsonify({"firmwares": service.list_firmwares(conn)})


@bp.post("/api/firmware")
def api_register_firmware():
    payload = request.get_json(silent=True) or request.form
    fw_version = (payload.get("fw_version") or "").strip()
    word_order = (payload.get("word_order") or "").strip()
    if not fw_version:
        return jsonify({"error": "固件号不能为空"}), 400
    try:
        result = service.register_firmware(db.get_db(), fw_version, word_order)
    except service.ConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    status = 200 if result["already_registered"] else 201
    return jsonify(result), status


@bp.post("/api/upload")
def api_upload():
    try:
        summary, stored, inserted = _run_upload(_csv_text_from_request())
    except decoder.ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except service.BatchRejected as exc:
        return (
            jsonify(
                {
                    "error": str(exc),
                    "rejected": True,
                    "conflicts": exc.conflicts,
                }
            ),
            409,
        )
    return jsonify(
        {
            "summary": {k: v for k, v in summary.items() if k != "inserted_keys"},
            "inserted": [
                {"device_id": d, "frame_no": n} for d, n in sorted(inserted)
            ],
            "frames": stored,
        }
    )


@bp.get("/api/diagnostics")
def api_diagnostics():
    conn = db.get_db()
    with db.read_snapshot(conn):
        return jsonify({"sheets": service.list_diagnostic_sheets(conn)})


@bp.post("/api/diagnostics")
def api_create_diagnostic():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form
    try:
        result = _create_sheet(payload)
    except diagnosis.SheetValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 201


@bp.get("/api/diagnostics/<int:sheet_id>")
def api_diagnostic_detail(sheet_id):
    conn = db.get_db()
    with db.read_snapshot(conn):
        data = service.get_diagnostic_sheet(conn, sheet_id)
    if data is None:
        abort(404)
    return jsonify(data)
