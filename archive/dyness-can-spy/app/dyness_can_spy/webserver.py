import json
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .analysis import analyze_session, candidate_series
from .exporter import export_session
from .storage import SessionStore, list_sessions, safe_session_id
from .telemetry import import_dyness_csv


class SpyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager):
        super().__init__(address, SpyHandler)
        self.manager = manager
        self.config = manager.config


class SpyHandler(BaseHTTPRequestHandler):
    server_version = "DynessCANSpy/0.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/status":
                return self._json(self.server.manager.status())
            if path == "/api/preflight":
                return self._json(self.server.manager.preflight())
            if path == "/api/sessions":
                return self._json(list_sessions(self.server.config.sessions_dir))
            if path == "/api/events":
                return self._events()
            if path.startswith("/api/sessions/"):
                return self._session_get(path, parse_qs(parsed.query))
            return self._static(path)
        except (ValueError, FileNotFoundError) as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._body_json()
            if parsed.path == "/api/capture/start":
                return self._json(self.server.manager.start(str(body.get("notes", ""))))
            if parsed.path == "/api/capture/stop":
                return self._json(self.server.manager.stop("user"))
            if parsed.path == "/api/import-rs485":
                result = import_dyness_csv(Path(body["path"]), self.server.config.sessions_dir,
                                           body.get("session_id"))
                return self._json(result, 201)
            if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/analyze"):
                session_id = safe_session_id(parsed.path.split("/")[3])
                path = self.server.config.sessions_dir / (session_id + ".sqlite")
                if not path.exists():
                    raise FileNotFoundError(session_id)
                if self.server.manager.status()["active"] and self.server.manager.status()["session_id"] == session_id:
                    raise RuntimeError("stop capture before analysis")
                return self._json(analyze_session(path, self.server.config))
            self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 409)

    def _session_get(self, path, query):
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            return self._json({"error": "not found"}, 404)
        session_id = safe_session_id(parts[2])
        db_path = self.server.config.sessions_dir / (session_id + ".sqlite")
        if not db_path.exists():
            raise FileNotFoundError(session_id)
        if len(parts) == 4 and parts[3] == "candidates":
            store = SessionStore(db_path)
            rows = [dict(row) for row in store.db.execute("SELECT * FROM candidates ORDER BY cell_index,rank")]
            store.close()
            return self._json(rows)
        if len(parts) == 4 and parts[3] == "series":
            return self._json(candidate_series(db_path, int(query.get("cell", ["7"])[0]),
                                               int(query.get("rank", ["1"])[0])))
        if len(parts) == 4 and parts[3] == "download":
            state = self.server.manager.status()
            if state.get("active") and state.get("session_id") == session_id:
                raise RuntimeError("stop capture before export")
            files = export_session(db_path, self.server.config.exports_dir)
            return self._file(Path(files["archive"]), "application/zip")
        store = SessionStore(db_path)
        info = store.info()
        store.close()
        return self._json(info)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for _ in range(30):
            payload = json.dumps(self.server.manager.status(), default=str)
            try:
                self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(1.0)

    def _static(self, path):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        if "/" in name or "\\" in name or name.startswith("."):
            return self._json({"error": "not found"}, 404)
        file_path = Path(__file__).with_name("web") / name
        if not file_path.exists():
            return self._json({"error": "not found"}, 404)
        return self._file(file_path, mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")

    def _file(self, path, content_type):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "attachment; filename=\"%s\"" % path.name if content_type == "application/zip" else "inline")
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _json(self, value, status=200):
        data = json.dumps(value, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def serve(manager):
    server = SpyHTTPServer((manager.config.host, manager.config.port), manager)
    try:
        server.serve_forever()
    finally:
        manager.stop("service_shutdown")
        server.server_close()
