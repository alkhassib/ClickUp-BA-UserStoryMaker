"""Command line entry point: `ba-flow <command>` (or `python -m ba_flow <command>`)."""
from __future__ import annotations

import argparse
import sys

from ba_flow.app import build_app
from ba_flow.clickup.client import ClickUpError
from ba_flow.clickup.resolver import Check, WorkspaceConfigError, local_checks
from ba_flow.core.config import ConfigError, load_settings

LABELS = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_checks(checks: list[Check]) -> None:
    section = None
    for c in checks:
        if c.section != section:
            section = c.section
            print(f"\n{section}")
        print(f"  {LABELS[c.level]} {c.message}")


def cmd_check_config(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        print(f"[FAIL] {e}")
        return 2
    print(f"settings.yaml valid (hash {settings.config_hash}) — user story language: "
          f"{settings.output.user_story_language}")

    checks = local_checks(settings)
    if not settings.secrets.clickup_token:
        _print_checks(checks)
        return 1

    app = build_app(args.config, log_level="ERROR")
    try:
        app.resolver.refresh()  # always re-inspect ClickUp and refresh the cache
    except WorkspaceConfigError:
        pass
    except ClickUpError as e:
        checks.append(Check("fail", "clickup", str(e)))
    finally:
        app.close()
    checks += app.resolver.last_checks

    _print_checks(checks)
    fails = sum(c.level == "fail" for c in checks)
    warns = sum(c.level == "warn" for c in checks)
    print(f"\n{'FAILED' if fails else 'PASSED'}: {fails} failure(s), {warns} warning(s)")
    if not fails:
        print("Resolved IDs cached; the app will reuse them until settings.yaml changes or the cache expires.")
    return 1 if fails else 0


def cmd_refresh_cache(args: argparse.Namespace) -> int:
    app = build_app(args.config)
    try:
        app.resolver.invalidate()
        ws = app.resolver.refresh()
        print(f"Cache refreshed for space '{ws.space_name}'.")
        return 0
    except WorkspaceConfigError as e:
        print(e)
        return 1
    finally:
        app.close()


def cmd_run(args: argparse.Namespace) -> int:
    from ba_flow.runtime import build_dispatcher
    from ba_flow.triggers.poller import Poller

    app = build_app(args.config, dry_run=args.dry_run)
    try:
        app.startup_check()  # automatic check-config; refuses to start on mismatch
        poller = Poller(app, build_dispatcher(app))
        if args.once:
            n = poller.poll_once(resume=True)
            print(f"Dispatched {n} event(s).")
        else:
            poller.run_forever(app.settings.trigger.interval_seconds)
        return 0
    except WorkspaceConfigError as e:
        print(e)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        app.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    from ba_flow.flow.dispatcher import INTAKE_TO_ANALYZE, Event
    from ba_flow.runtime import build_dispatcher

    app = build_app(args.config, dry_run=args.dry_run)
    try:
        app.startup_check()
        result = build_dispatcher(app).dispatch(Event(INTAKE_TO_ANALYZE, args.task_id, "manual"), force=True)
        print(result)
        return 0 if result and result.get("final_result") == "analyzed" else 1
    except WorkspaceConfigError as e:
        print(e)
        return 1
    finally:
        app.close()


def cmd_validate(args: argparse.Namespace) -> int:
    from ba_flow.flow.dispatcher import DT_SUBMITTED, Event
    from ba_flow.runtime import build_dispatcher

    app = build_app(args.config, dry_run=args.dry_run)
    try:
        app.startup_check()
        result = build_dispatcher(app).dispatch(Event(DT_SUBMITTED, args.dt_id, "manual"), force=True)
        print(result)
        return 0 if result and result.get("final_result") in ("compiled", "returned") else 1
    except WorkspaceConfigError as e:
        print(e)
        return 1
    finally:
        app.close()


def cmd_serve(args: argparse.Namespace) -> int:
    import threading

    import uvicorn

    from ba_flow.core.observability import get_logger, log
    from ba_flow.runtime import build_dispatcher
    from ba_flow.triggers.webhook import create_http_app
    from ba_flow.triggers.worker import EventWorker, PollTick

    app = build_app(args.config, dry_run=args.dry_run)
    t = app.settings.trigger
    if t.mode == "polling":
        print("trigger.mode is 'polling': use `ba-flow run`, or set trigger.mode to 'webhook' or 'hybrid'.")
        app.close()
        return 2
    if not app.settings.secrets.webhook_secret:
        print("CLICKUP_WEBHOOK_SECRET is not set. Run `ba-flow webhook register --url <public url> --save` first.")
        app.close()
        return 2
    stop = threading.Event()
    worker = None
    try:
        ws = app.startup_check()  # automatic check-config
        worker = EventWorker(app, build_dispatcher(app), debounce_seconds=t.webhook.debounce_seconds)
        worker.start()
        worker.submit(PollTick(resume=True))  # catch up on anything that happened while the service was down
        if t.mode == "hybrid":
            def safety_poll() -> None:
                while not stop.wait(t.safety_poll_seconds):
                    worker.submit(PollTick())
            threading.Thread(target=safety_poll, name="ba-flow-safety-poll", daemon=True).start()
        api = create_http_app(worker, secret=app.settings.secrets.webhook_secret, path=t.webhook.path,
                              bot_user_id=ws.bot_user_id)
        host, port = args.host or t.webhook.host, args.port or t.webhook.port
        log(get_logger("serve"), 20, "serving", mode=t.mode, host=host, port=port, path=t.webhook.path,
            dry_run=app.writer.dry_run)
        uvicorn.run(api, host=host, port=port, log_level="warning")
        return 0
    except WorkspaceConfigError as e:
        print(e)
        return 1
    finally:
        stop.set()
        if worker:
            worker.stop()
        app.close()


def _set_env_values(path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in values.items():
        line = f"{key}={value}"
        idx = next((i for i, l in enumerate(lines) if l.split("=", 1)[0].strip() == key), None)
        if idx is None:
            lines.append(line)
        else:
            lines[idx] = line
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_webhook(args: argparse.Namespace) -> int:
    app = build_app(args.config, log_level="ERROR")
    client, team = app.client, app.settings.clickup.team_id
    try:
        if args.webhook_cmd == "register":
            ws = app.startup_check()
            resp = client.create_webhook(team, args.url, ["taskStatusUpdated"], ws.space_id)
            hook = resp.get("webhook") or resp
            wid, secret = resp.get("id") or hook.get("id"), hook.get("secret")
            print(f"Webhook {wid} registered for space '{ws.space_name}' -> {args.url}")
            if args.save:
                _set_env_values(app.settings.root / ".env",
                                {"CLICKUP_WEBHOOK_ID": str(wid), "CLICKUP_WEBHOOK_SECRET": str(secret)})
                print("Saved CLICKUP_WEBHOOK_ID and CLICKUP_WEBHOOK_SECRET to .env")
            else:
                print(f"Put these in .env:\nCLICKUP_WEBHOOK_ID={wid}\nCLICKUP_WEBHOOK_SECRET={secret}")
        elif args.webhook_cmd == "list":
            hooks = client.list_webhooks(team)
            if not hooks:
                print("No webhooks.")
            for h in hooks:
                health = h.get("health") or {}
                print(f"{h.get('id')}  {h.get('endpoint')}  events={h.get('events')}  "
                      f"status={health.get('status')}  fail_count={health.get('fail_count')}")
        elif args.webhook_cmd == "delete":
            client.delete_webhook(args.webhook_id)
            print(f"Webhook {args.webhook_id} deleted.")
        elif args.webhook_cmd == "reactivate":
            client.update_webhook(args.webhook_id, {"status": "active"})
            print(f"Webhook {args.webhook_id} set to active.")
        return 0
    except WorkspaceConfigError as e:
        print(e)
        return 1
    except ClickUpError as e:
        print(f"[FAIL] {e}")
        return 1
    finally:
        app.close()


RUN_FIELDS = ["run_id", "started_at", "finished_at", "trigger", "event_type", "task_id", "dt_id", "model",
              "prompt_version", "input_tokens", "output_tokens", "cache_read_tokens", "cache_hit",
              "validation_result", "final_result", "error", "dry_run", "doc_sha", "pack_id"]


def cmd_runs_list(args: argparse.Namespace) -> int:
    app = build_app(args.config, log_level="ERROR")
    try:
        runs = app.store.list_runs(task_id=args.task, limit=args.limit)
    finally:
        app.close()
    if not runs:
        print("No runs recorded.")
        return 0
    print(f"{'run':<9} {'started':<26} {'event':<14} {'task':<14} {'result':<12} {'tokens in/out':<14}")
    for r in runs:
        tokens = f"{r['input_tokens']}/{r['output_tokens']}"
        print(f"{r['run_id'][:8]:<9} {r['started_at']:<26} {str(r['event_type']):<14} "
              f"{str(r['task_id']):<14} {str(r['final_result']):<12} {tokens:<14}")
    return 0


def cmd_runs_show(args: argparse.Namespace) -> int:
    app = build_app(args.config, log_level="ERROR")
    try:
        run = app.store.get_run(args.run_id)
    finally:
        app.close()
    if not run:
        print(f"Run '{args.run_id}' not found.")
        return 1
    for k in RUN_FIELDS:
        print(f"{k:<18} {run.get(k)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ba-flow", description="BA gap resolution automation for ClickUp")
    p.add_argument("--config", help="path to settings.yaml (default: config/settings.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="validate settings.yaml against ClickUp and refresh the ID cache") \
        .set_defaults(func=cmd_check_config)
    sub.add_parser("refresh-cache", help="drop cached ClickUp IDs and resolve them again") \
        .set_defaults(func=cmd_refresh_cache)

    run = sub.add_parser("run", help="start the poller (runs check-config automatically first)")
    run.add_argument("--once", action="store_true", help="single poll cycle, then exit")
    run.add_argument("--dry-run", action="store_true", help="read and analyze, but write nothing to ClickUp")
    run.set_defaults(func=cmd_run)

    an = sub.add_parser("analyze", help="run W1 analysis for one US Intake task now (ignores its status)")
    an.add_argument("task_id")
    an.add_argument("--dry-run", action="store_true")
    an.set_defaults(func=cmd_analyze)

    va = sub.add_parser("validate", help="run W2 validation for one submitted Draft Ticket now")
    va.add_argument("dt_id")
    va.add_argument("--dry-run", action="store_true")
    va.set_defaults(func=cmd_validate)

    sv = sub.add_parser("serve", help="webhook/hybrid mode: HTTP endpoint for ClickUp webhooks (+ safety poll)")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--dry-run", action="store_true")
    sv.set_defaults(func=cmd_serve)

    wh = sub.add_parser("webhook", help="manage the ClickUp webhook").add_subparsers(dest="webhook_cmd", required=True)
    reg = wh.add_parser("register", help="create a webhook for the BA Flow space (taskStatusUpdated)")
    reg.add_argument("--url", required=True, help="public https URL ending with trigger.webhook.path")
    reg.add_argument("--save", action="store_true", help="write id and secret to .env")
    reg.set_defaults(func=cmd_webhook)
    wh.add_parser("list", help="webhooks of the team, with health").set_defaults(func=cmd_webhook)
    for name, help_ in (("delete", "delete a webhook"), ("reactivate", "set a suspended webhook back to active")):
        p_ = wh.add_parser(name, help=help_)
        p_.add_argument("webhook_id")
        p_.set_defaults(func=cmd_webhook)

    runs = sub.add_parser("runs", help="inspect processing runs").add_subparsers(dest="runs_cmd", required=True)
    rl = runs.add_parser("list", help="recent runs")
    rl.add_argument("--task", help="filter by task or DT id")
    rl.add_argument("--limit", type=int, default=20)
    rl.set_defaults(func=cmd_runs_list)
    rs = runs.add_parser("show", help="details of one run (full id or prefix)")
    rs.add_argument("run_id")
    rs.set_defaults(func=cmd_runs_show)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"[FAIL] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
