"""User-facing commands. Run immediately, or start the optional dashboard."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .config import data_directory


def parser():
    cli = argparse.ArgumentParser(description="Read-only portfolio research with an optional personal dashboard")
    cli.add_argument("--data-dir", type=Path, help="Application configuration and history directory")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("serve", "setup"):
        serve = commands.add_parser(name, help="Start the local setup and research dashboard")
        serve.add_argument("--host", default="127.0.0.1")
        serve.add_argument("--port", type=int, default=8000)
        serve.add_argument("--server", action="store_true", help="Require owner authentication and HTTPS public origin")
    run = commands.add_parser("analyze", aliases=["run"], help="Run research now; JSON on stdout by default")
    run.add_argument("--request", type=Path, help="AnalysisRequest JSON file, or - for stdin")
    run.add_argument("--demo", action="store_true", help="Use a labeled offline demonstration portfolio")
    run.add_argument("--offline", action="store_true", help="Deterministic metrics, without network calls")
    run.add_argument("--send", action="store_true", help="Also email the report to the configured verified owner")
    run.add_argument("--format", choices=["json", "text", "html"], default="json")
    diagnostics = commands.add_parser("diagnostics", help="Inspect configuration without showing credentials")
    diagnostics.add_argument("--live", action="store_true", help="Explicitly check configured external connections (may incur provider usage)")
    schema = commands.add_parser("schema", help="Print a public JSON schema")
    schema.add_argument("name", choices=["request", "result", "submission"])
    return cli


async def run_analysis(args, directory):
    from .delivery import render_report
    from .engine import EngineDependencies, analyze_portfolio
    from .models import AnalysisRequest
    from .service import ApplicationService, build_dependencies
    service = ApplicationService(directory, server=os.environ.get("PORTFOLIO_SERVER_MODE") == "1")
    if args.demo:
        from .demo import demo_snapshot
        request = service.request_for(demo_snapshot())
    elif args.request:
        raw = sys.stdin.read() if str(args.request) == "-" else args.request.read_text(encoding="utf-8")
        provided = AnalysisRequest.model_validate_json(raw)
        request = service.request_for(snapshot=provided.snapshot, **provided.model_dump(exclude={"snapshot"}))
    else:
        snapshot = service.store.snapshot()
        if not snapshot:
            raise ValueError("Supply --request, --demo, or import holdings in the dashboard")
        request = service.request_for(snapshot)
    initialization_warning = None
    try:
        dependencies = EngineDependencies() if args.offline or args.demo else build_dependencies(service.settings, service.secrets)
    except Exception:
        dependencies = build_dependencies(service.settings.model_copy(update={"model_enabled": False}), service.secrets)
        initialization_warning = "Model connection could not be initialized; metrics and available research are shown. Review model settings."
    from uuid import uuid4
    run_id = str(uuid4())
    if not service.store.create_run(run_id, email_requested=args.send):
        raise ValueError("Another analysis is running; use the dashboard to inspect or cancel it")
    try:
        service.store.update_run(run_id, status="running")
        result = await analyze_portfolio(request, dependencies)
        result.run_id = run_id
        if initialization_warning:
            result.warnings.append(initialization_warning)
            result.status = "partial" if result.status != "failed" else "failed"
        service.store.finish(result)
        delivery_failed = False
        if args.send:
            outcome = await service.deliver(run_id)
            print(outcome.message, file=sys.stderr)
            if outcome.status != "sent":
                delivery_failed = True
        if args.format == "json":
            print(result.model_dump_json(indent=2))
        else:
            html, text = render_report(result)
            print(html if args.format == "html" else text)
        return 2 if delivery_failed else (1 if result.status == "failed" else 0)
    except BaseException:
        row = service.store.run(run_id)
        if row and row["status"] in {"queued", "running"}:
            service.store.update_run(run_id, status="failed", error="Command interrupted or failed. Run again manually.")
        raise


async def diagnose(args, directory):
    from .service import ApplicationService, build_dependencies, read_secret
    service = ApplicationService(directory, server=os.environ.get("PORTFOLIO_SERVER_MODE") == "1")
    settings = service.settings
    checks = {"snapshot_present": service.store.snapshot() is not None,
              "model_enabled": settings.model_enabled, "model_credential_present": bool(read_secret(service.secrets, "model_api_key")),
              "search_provider": settings.search_provider, "schedule_enabled": settings.schedule.enabled,
              "email_configured": settings.smtp is not None, "email_verified": bool(settings.smtp and service.store.get("verified_recipient") == settings.smtp.recipient)}
    if args.live:
        checks["live"] = {}
        deps = build_dependencies(settings, service.secrets)
        for name, operation in (
            ("search", lambda: deps.research.search("Federal Reserve latest monetary policy", max_results=1) if deps.research else None),
            ("model", lambda: deps.model.ainvoke("Respond with the word connected.") if deps.model else None),
        ):
            try:
                pending = operation()
                if pending is None:
                    checks["live"][name] = "not configured"
                else:
                    async with asyncio.timeout(45):
                        await pending
                    checks["live"][name] = "connected"
            except Exception:
                checks["live"][name] = "failed; check configuration, quota, and network"
    print(json.dumps(checks, indent=2))
    return 0


def main():
    args = parser().parse_args()
    directory = args.data_dir or data_directory()
    if args.data_dir:
        os.environ["PORTFOLIO_DATA_DIR"] = str(directory.resolve())
    try:
        if args.command in {"serve", "setup"}:
            server = args.server or os.environ.get("PORTFOLIO_SERVER_MODE") == "1"
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not server:
                raise ValueError("Non-loopback hosting requires --server, an owner password, and an HTTPS public origin")
            from .web import create_app
            import uvicorn
            print(f"Open {'your configured HTTPS origin' if server else f'http://{args.host}:{args.port}'} to set up your research desk.")
            uvicorn.run(create_app(directory, server=server), host=args.host, port=args.port, workers=1, access_log=False)
            return 0
        if args.command in {"analyze", "run"}:
            return asyncio.run(run_analysis(args, directory))
        if args.command == "diagnostics":
            return asyncio.run(diagnose(args, directory))
        if args.command == "schema":
            from .models import AnalysisRequest, AnalysisResult
            from .web import RunSubmission
            print(json.dumps({"request": AnalysisRequest, "result": AnalysisResult, "submission": RunSubmission}[args.name].model_json_schema(), indent=2))
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Never echo provider exceptions or credentials into logs/stdout.
        from pydantic import ValidationError
        from .secrets import SecretStorageError
        if isinstance(exc, ValidationError):
            message = "Invalid input: " + "; ".join(".".join(map(str, error["loc"])) + " " + error["msg"] for error in exc.errors())
        elif isinstance(exc, (ValueError, SecretStorageError)):
            message = str(exc)
        else:
            message = "Unable to complete the command. Check the inputs, connections, and dashboard status."
        print(message[:500], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
