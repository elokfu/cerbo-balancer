import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .analysis import analyze_session
from .config import load_config
from .exporter import export_session
from .service import CaptureManager
from .storage import list_sessions, safe_session_id
from .telemetry import import_dyness_csv
from .webserver import serve


def _print(value):
    print(json.dumps(value, indent=2, default=str))


def _api(config, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (config.port, path), data=data,
                                     method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("service returned HTTP %d: %s" % (exc.code, message))
    except urllib.error.URLError as exc:
        raise RuntimeError("cannot reach local spy service: %s" % exc.reason)


def _session_path(config, value):
    candidate = Path(value)
    if candidate.exists():
        return candidate
    return config.sessions_dir / (safe_session_id(value) + ".sqlite")


def build_parser():
    parser = argparse.ArgumentParser(prog="dyness-can-spy", description="Receive-only Dyness CAN spy")
    parser.add_argument("--config", help="configuration file path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("serve")
    capture = sub.add_parser("capture")
    capture_sub = capture.add_subparsers(dest="capture_command", required=True)
    start = capture_sub.add_parser("start")
    start.add_argument("--notes", default="")
    capture_sub.add_parser("stop")
    capture_sub.add_parser("status")
    sessions = sub.add_parser("sessions")
    sessions.add_subparsers(dest="sessions_command", required=True).add_parser("list")
    importer = sub.add_parser("import-rs485")
    importer.add_argument("csv")
    importer.add_argument("--session-id")
    analyzer = sub.add_parser("analyze")
    analyzer.add_argument("session")
    exporter = sub.add_parser("export")
    exporter.add_argument("session")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    try:
        if args.command == "preflight":
            result = CaptureManager(config).preflight()
            _print(result)
            return 0 if result.get("ok") else 1
        if args.command == "serve":
            serve(CaptureManager(config))
            return 0
        if args.command == "capture":
            if args.capture_command == "start":
                result = _api(config, "POST", "/api/capture/start", {"notes": args.notes})
            elif args.capture_command == "stop":
                result = _api(config, "POST", "/api/capture/stop", {})
            else:
                result = _api(config, "GET", "/api/status")
            _print(result)
            return 0
        if args.command == "sessions":
            _print(list_sessions(config.sessions_dir))
            return 0
        if args.command == "import-rs485":
            _print(import_dyness_csv(Path(args.csv), config.sessions_dir, args.session_id))
            return 0
        if args.command == "analyze":
            path = _session_path(config, args.session)
            if not path.exists():
                raise FileNotFoundError(path)
            _print(analyze_session(path, config))
            return 0
        if args.command == "export":
            path = _session_path(config, args.session)
            if not path.exists():
                raise FileNotFoundError(path)
            _print(export_session(path, config.exports_dir))
            return 0
    except (RuntimeError, ValueError, FileNotFoundError, FileExistsError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

