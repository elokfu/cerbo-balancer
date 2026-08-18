import csv
import html
import json
import zipfile
from pathlib import Path
from typing import Dict

from .storage import SessionStore, safe_session_id


def export_session(session_path: Path, exports_dir: Path) -> Dict[str, str]:
    store = SessionStore(session_path)
    info = store.info()
    session_id = safe_session_id(info["id"])
    out = Path(exports_dir) / session_id
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "candump": out / "frames.log",
        "frames_csv": out / "frames.csv",
        "cells_csv": out / "cell_samples.csv",
        "candidates_csv": out / "candidates.csv",
        "candidates_json": out / "candidates.json",
        "report_html": out / "report.html",
    }
    with paths["candump"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in store.db.execute("SELECT * FROM frames ORDER BY seq"):
            width = 8 if row["is_extended"] else 3
            ident = ("%0*X" % (width, row["can_id"]))
            suffix = "R" if row["is_remote"] else bytes(row["data"]).hex().upper()
            handle.write("(%.6f) %s %s#%s\n" % (row["wall_ts"], row["interface"], ident, suffix))
    _write_query_csv(store, paths["frames_csv"], "SELECT seq,wall_ts,mono_ts,can_id,is_extended,is_remote,is_error,is_local,CASE is_local WHEN 1 THEN 'CERBO_TX' ELSE 'BUS_RX' END AS direction,dlc,hex(data) AS data_hex,interface FROM frames ORDER BY seq")
    _write_query_csv(store, paths["cells_csv"], "SELECT * FROM cell_samples ORDER BY sample_id,cell_index")
    _write_query_csv(store, paths["candidates_csv"], "SELECT * FROM candidates ORDER BY cell_index,rank")
    candidates = [dict(row) for row in store.db.execute("SELECT * FROM candidates ORDER BY cell_index,rank")]
    paths["candidates_json"].write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    paths["report_html"].write_text(_report(info, candidates), encoding="utf-8")
    archive = out / (session_id + "-evidence.zip")
    with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, path in paths.items():
            bundle.write(str(path), path.name)
    paths["archive"] = archive
    store.close()
    return {key: str(path) for key, path in paths.items()}


def _write_query_csv(store: SessionStore, path: Path, query: str) -> None:
    cursor = store.db.execute(query)
    columns = [item[0] for item in cursor.description]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(cursor)


def _report(info, candidates) -> str:
    rows = []
    for row in candidates:
        can_id = "—" if row["can_id"] is None else "0x%X" % row["can_id"]
        direction = "—" if row["is_local"] is None else ("CERBO_TX" if row["is_local"] else "BUS_RX")
        rows.append("<tr%s><td>%d</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            " class=\"tracer\"" if row["cell_index"] == 7 else "",
            row["cell_index"], row["rank"], can_id,
            direction, html.escape(row["verdict"]), _num(row["correlation"]), _num(row["rmse_mv"]),
            _num(row["lag_s"]), _num(row["score"]), html.escape(row["detail"])))
    return """<!doctype html><html><head><meta charset=utf-8><title>Dyness CAN analysis</title>
<style>body{font:14px system-ui;margin:2rem;color:#17202a}table{border-collapse:collapse;width:100%%}th,td{padding:.45rem;border:1px solid #ccd}th{background:#eef}.tracer{background:#fff3bf}.meta{white-space:pre-wrap;background:#f5f5f5;padding:1rem}</style></head>
<body><h1>Passive Dyness CAN analysis</h1><p>Session <strong>%s</strong>. Cell 7 is highlighted as an experimental tracer. Cell 16 provenance is retained in the cell sample export.</p>
<div class=meta>%s</div><h2>Ranked candidates</h2><table><thead><tr><th>Cell</th><th>Rank</th><th>CAN ID</th><th>Direction</th><th>Verdict</th><th>r</th><th>RMSE mV</th><th>Lag s</th><th>Score</th><th>Detail</th></tr></thead><tbody>%s</tbody></table></body></html>""" % (
        html.escape(info.get("id", "")), html.escape(json.dumps(info, indent=2, default=str)), "".join(rows))


def _num(value):
    return "—" if value is None else "%.4g" % value
