"""Command-line interface (stage 1).

Commands are intentionally small vertical slices: diagnostics, model listing,
durable event log and a chat placeholder that already records tasks and events.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.table import Table

from kivi.cloud import (
    CloudBroker,
    CloudError,
    CloudRegistryError,
    CloudScout,
    QuotaExhausted,
    ScoutError,
    ScoutQuery,
    build_handoff,
    load_cloud_registry,
    load_handoff,
    load_scout_registry,
    save_handoff,
)
from kivi.config import ConfigError, KiviConfig, load_config
from kivi.doctor import ollama_models, run_checks, summary
from kivi.kernel.db import Database
from kivi.kernel.events import EventBus
from kivi.kernel.logs import JsonlLogger
from kivi.models import (
    BackendError,
    GatewayError,
    ModelGateway,
    OllamaBackend,
    RegistryError,
    canonical_model_id,
    load_registry,
    query_vram,
)
from kivi.paths import Paths, get_paths
from kivi.safety import PermissionEngine, ProtectedZones
from kivi.tools import ToolExecutor, build_registry

console = Console()


@contextmanager
def _state(paths: Paths) -> Iterator[tuple[Database, EventBus]]:
    db = Database(paths.db_file)
    try:
        yield db, EventBus(db, JsonlLogger(paths.logs))
    finally:
        db.close()


def _config(paths: Paths) -> KiviConfig:
    try:
        return load_config(paths.config_file)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise SystemExit(2) from exc


def _registry(paths: Paths):
    try:
        return load_registry(paths.root / "config" / "models.yaml")
    except RegistryError as exc:
        console.print(f"[red]model registry error:[/red] {exc}")
        raise SystemExit(2) from exc


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = get_paths()
    checks = run_checks(paths)

    table = Table(title="Kivi doctor", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for check in checks:
        table.add_row(check.name, f"[{colors[check.status]}]{check.status}[/]", check.detail)
    console.print(table)
    console.print(f"[bold]{summary(checks)}[/bold]")
    return 0 if not any(check.status == "fail" for check in checks) else 1


def cmd_models(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)

    table = Table(title="Model gateway")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("local first", str(config.model_gateway.prefer_local))
    table.add_row("one heavy model", str(config.model_gateway.one_heavy_model))
    table.add_row("VRAM budget", f"{config.model_gateway.vram_total_mb} MB")
    table.add_row("safety margin", f"{config.model_gateway.safety_margin_mb} MB")
    table.add_row("cloud mode", config.model_gateway.cloud.mode)
    table.add_row("cloud cost limit", f"${config.model_gateway.cloud.cost_limit_usd:.2f}")
    console.print(table)

    snapshot = query_vram()
    if snapshot is None:
        console.print("[yellow]VRAM: nvidia-smi did not return a GPU snapshot.[/yellow]")
    else:
        console.print(
            f"VRAM now: [bold]{snapshot.free_mb} MB free[/bold] of "
            f"{snapshot.total_mb} MB ({snapshot.used_ratio:.0%} used)"
        )
        if snapshot.overcommitted:
            console.print(
                "[yellow]Shared memory is active; a heavy load should be refused.[/yellow]"
            )

    registry = _registry(paths)
    names = ollama_models()
    installed_names = {canonical_model_id(name) for name in names}
    running: dict[str, int] = {}
    try:
        with OllamaBackend(timeout=3) as backend:
            running = backend.running()
    except BackendError:
        running = {}

    catalog = Table(title="Model registry")
    catalog.add_column("Model", overflow="fold")
    catalog.add_column("Tier", justify="right")
    catalog.add_column("Role")
    catalog.add_column("VRAM", justify="right")
    catalog.add_column("Installed")
    catalog.add_column("Loaded")
    for spec in sorted(registry.all(), key=lambda item: (item.tier, item.id)):
        catalog.add_row(
            spec.id,
            str(spec.tier),
            spec.role,
            f"{spec.vram_mb} MB",
            "yes" if canonical_model_id(spec.id) in installed_names else "-",
            f"{running[spec.id]} MB" if spec.id in running else "-",
        )
    console.print(catalog)

    extra = sorted(set(running) - {spec.id for spec in registry.all()})
    if extra:
        console.print("[yellow]Loaded outside the registry:[/yellow] " + ", ".join(extra))

    if not names:
        console.print("[yellow]Ollama is not running or has no models installed.[/yellow]")
        return 0

    import httpx

    payload = httpx.get("http://127.0.0.1:11434/api/tags", timeout=3).json()
    installed = Table(title="Ollama models")
    installed.add_column("Model")
    installed.add_column("Size", justify="right")
    installed.add_column("Parameters")
    for item in payload.get("models", []):
        size = item.get("size", 0) / 1024**3
        details = item.get("details", {})
        installed.add_row(
            item.get("name", "?"),
            f"{size:.1f} GB",
            details.get("parameter_size", "?"),
        )
    console.print(installed)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    prompt = args.prompt or ""
    if not prompt:
        console.print("Interactive chat arrives with the Web-UI (stage 6).")
        console.print('For a real local answer use: [bold]kivi ask "привет"[/bold]')
        return 0

    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    with _state(paths) as (db, bus):
        db.create_task(task_id, "chat", {"prompt": prompt})
        bus.emit("chat.request", task_id=task_id, source="cli", payload={"prompt": prompt})
        db.update_task(task_id, "done")

    console.print(f"[cyan]Kivi[/cyan] (gateway stub, task {task_id}):")
    console.print(f"Принято: {prompt}")
    console.print(
        f"[dim]voice={config.voice.provider}/{config.voice.speaker}, "
        f"autonomy={config.autonomy}[/dim]"
    )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = _registry(paths)
    try:
        spec = registry.require(args.model) if args.model else registry.default_for("router")
    except RegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    messages = [{"role": "user", "content": args.prompt}]
    options: dict[str, int] = {"num_predict": args.num_predict}
    if spec.context > 0:
        options["num_ctx"] = spec.context
    answer = ""

    with _state(paths) as (db, bus):
        db.create_task(task_id, "ask", {"prompt": args.prompt, "model": spec.id})
        bus.emit(
            "ask.request",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "priority": 0},
        )
        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    registry,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )
                if args.stream:
                    console.print(f"[cyan]Kivi[/cyan] · [dim]{spec.id}[/dim]")
                    chunks: list[str] = []
                    for chunk in gateway.chat_stream(
                        spec.id,
                        messages,
                        task_id=task_id,
                        priority=0,
                        keep_alive=args.keep_alive,
                        options=options,
                        wait_s=args.wait,
                    ):
                        chunks.append(chunk)
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    answer = "".join(chunks)
                else:
                    answer = gateway.chat(
                        spec.id,
                        messages,
                        task_id=task_id,
                        priority=0,
                        keep_alive=args.keep_alive,
                        options=options,
                        wait_s=args.wait,
                    )
                metrics = gateway.metrics()
        except (GatewayError, BackendError) as exc:
            db.update_task(task_id, "failed", {"error": str(exc)})
            bus.emit("ask.failed", task_id=task_id, source="cli", payload={"error": str(exc)})
            console.print(f"[red]model error:[/red] {exc}")
            return 1

        db.update_task(task_id, "done", {"model": spec.id, "answer": answer[:4000]})
        bus.emit(
            "ask.done",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "chars": len(answer)},
        )

    if not args.stream:
        console.print(f"[cyan]Kivi[/cyan] · [dim]{spec.id}[/dim]")
        console.print(answer or "(empty answer)")
    console.print(
        f"[dim]loads={metrics['loads']} evictions={metrics['evictions']} "
        f"last_load={metrics['last_load_ms']} ms[/dim]"
    )
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = _registry(paths)
    try:
        spec = registry.require(args.model) if args.model else registry.default_for("embedding")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 2

    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    with _state(paths) as (db, bus):
        db.create_task(task_id, "embed", {"text": args.text, "model": spec.id})
        bus.emit(
            "embed.request",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "chars": len(args.text)},
        )
        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    registry,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )
                vectors = gateway.embed(
                    spec.id,
                    [args.text],
                    task_id=task_id,
                    priority=0,
                    keep_alive=args.keep_alive,
                    wait_s=args.wait,
                )
                metrics = gateway.metrics()
        except (GatewayError, BackendError) as exc:
            db.update_task(task_id, "failed", {"error": str(exc)})
            bus.emit("embed.failed", task_id=task_id, source="cli", payload={"error": str(exc)})
            console.print(f"[red]embedding error:[/red] {exc}")
            return 1

        vector = vectors[0] if vectors else []
        db.update_task(task_id, "done", {"model": spec.id, "dim": len(vector)})
        bus.emit(
            "embed.done",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "dim": len(vector)},
        )

    preview = ", ".join(f"{value:.4f}" for value in vector[:8])
    console.print(f"[cyan]Kivi[/cyan] · [dim]{spec.id}[/dim] · dim={len(vector)}")
    console.print(f"[{preview}, …]")
    console.print(
        f"[dim]loads={metrics['loads']} evictions={metrics['evictions']} "
        f"last_load={metrics['last_load_ms']} ms[/dim]"
    )
    return 0


def cmd_cloud_status(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    try:
        registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
    except CloudRegistryError as exc:
        console.print(f"[red]cloud registry error:[/red] {exc}")
        return 2

    settings = config.model_gateway.cloud
    console.print(
        f"cloud mode [bold]{settings.mode}[/bold], cost limit ${settings.cost_limit_usd:.2f}"
    )
    with _state(paths) as (db, bus):
        broker = CloudBroker(db, settings, registry, events=bus)
        try:
            rows = broker.status()
        finally:
            broker.close()

    table = Table(title="Cloud providers")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Key")
    table.add_column("Data")
    table.add_column("Requests today", justify="right")
    table.add_column("Tokens today", justify="right")
    table.add_column("Free limit", overflow="fold")
    for row in rows:
        limit = (
            None if row.remaining_requests is None else row.requests_today + row.remaining_requests
        )
        free = "unlimited" if limit is None else f"{limit}/day"
        table.add_row(
            row.provider,
            row.kind,
            "yes" if row.key_configured else "-",
            row.data_class,
            f"{row.requests_today}/{limit}" if limit is not None else str(row.requests_today),
            str(row.tokens_today),
            free,
        )
    console.print(table)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        rows = db.recent_events(args.limit)
    if not rows:
        console.print("No events yet.")
        return 0

    table = Table(title=f"Last {len(rows)} events")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Type")
    table.add_column("Task")
    table.add_column("Payload", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]), row["ts"], row["type"], row["task_id"] or "-", row["payload"] or ""
        )
    console.print(table)
    return 0


def cmd_cloud_scout(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    candidates_path = paths.root / "config" / "cloud-candidates.yaml"
    registry_path = paths.root / "config" / "cloud.yaml"
    try:
        candidates = load_scout_registry(candidates_path)
        registry = load_cloud_registry(registry_path)
    except (CloudRegistryError, ScoutError) as exc:
        console.print(f"[red]cloud scout error:[/red] {exc}")
        return 1

    with _state(paths) as (db, events):
        scout = CloudScout(db, registry, candidates, events=events)
        if args.approve:
            try:
                provider = scout.approve(args.approve, registry_path)
            except ScoutError as exc:
                console.print(f"[red]cloud scout error:[/red] {exc}")
                return 1
            console.print(
                f"[green]approved[/green] {provider.id} in config/cloud.yaml "
                f"(key env: {provider.api_key_env or 'none'})"
            )
            console.print("Set the environment variable, then check `kivi cloud status`.")
            return 0

        try:
            query = ScoutQuery(
                capability=args.capability,
                data_class=args.data_class,
                allow_training_data=args.allow_training
                or config.model_gateway.cloud.allow_data_for_training,
                allow_watermark=args.allow_watermark,
                include_configured=args.include_configured,
                limit=args.limit,
            )
        except ScoutError as exc:
            console.print(f"[red]cloud scout error:[/red] {exc}")
            return 1

        if args.goal:
            outcome = scout.on_exhausted(
                query,
                goal=args.goal,
                reason=args.reason or "no free provider matched",
                next_step=args.next_step,
            )
            cards = outcome.cards
        else:
            outcome = None
            cards = scout.recommend(query)

    if not cards:
        console.print("No matching free provider in the Scout catalog.")
        if outcome is not None and outcome.handoff is not None:
            console.print(f"Local handoff saved as #{outcome.handoff_id}:")
            console.print(outcome.handoff.to_prompt())
        else:
            console.print("Pass --goal to save a local handoff, or run a local model directly.")
        return 0
    for index, card in enumerate(cards):
        if index:
            console.print("")
        console.print(card.to_text())
    if outcome is not None:
        console.print("\nUser confirmation is required: re-run with --approve <id>.")
    return 0


def _handoff_after_quota(
    paths: Paths,
    db: Database,
    events: EventBus,
    config: KiviConfig,
    args: argparse.Namespace,
    reason: str,
) -> int:
    """Checkpoint the task, then run Cloud Scout before giving up on the cloud."""
    goal = args.goal or args.prompt
    if config.model_gateway.cloud.scout_on_quota_exhausted:
        try:
            candidates = load_scout_registry(paths.root / "config" / "cloud-candidates.yaml")
            registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
        except (CloudRegistryError, ScoutError) as exc:
            console.print(f"[yellow]Cloud Scout unavailable:[/yellow] {exc}")
        else:
            scout = CloudScout(db, registry, candidates, events=events)
            outcome = scout.on_exhausted(
                ScoutQuery(
                    data_class=args.data_class,
                    allow_training_data=config.model_gateway.cloud.allow_data_for_training,
                    allow_watermark=args.allow_watermark,
                ),
                goal=goal,
                reason=reason,
                next_step=args.next_step,
                task_id=args.task_id,
            )
            console.print(f"[yellow]quota exhausted:[/yellow] {reason}")
            console.print(f"Checkpoint saved as handoff #{outcome.handoff_id}.")
            if outcome.found:
                console.print("Cloud Scout found replacement providers:")
                for index, card in enumerate(outcome.cards):
                    if index:
                        console.print("")
                    console.print(card.to_text())
                console.print("\nConfirm one with: kivi cloud scout --approve <id>")
            else:
                console.print("No free provider matched; continue locally from the handoff:")
                console.print(outcome.handoff.to_prompt() if outcome.handoff else "")
            return 1

    pack = build_handoff(goal, reason, next_step=args.next_step, task_id=args.task_id)
    handoff_id = save_handoff(db, pack)
    console.print(f"[yellow]quota exhausted:[/yellow] {reason}")
    console.print(f"Local handoff saved as #{handoff_id}:")
    console.print(pack.to_prompt())
    return 1


def cmd_cloud_ask(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    try:
        registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
    except CloudRegistryError as exc:
        console.print(f"[red]cloud error:[/red] {exc}")
        return 1

    with _state(paths) as (db, events):
        broker = CloudBroker(db, config.model_gateway.cloud, registry, events=events)
        try:
            provider_id = args.provider or next(
                (status.provider for status in broker.status() if status.key_configured),
                "",
            )
            if not provider_id:
                console.print("[yellow]No cloud provider has a key configured.[/yellow]")
                console.print("Set an api_key_env variable from config/cloud.yaml.")
                return 1
            result = broker.complete(
                provider_id,
                [{"role": "user", "content": args.prompt}],
                model=args.model,
                data_class=args.data_class,
                max_tokens=args.max_tokens,
                task_id=args.task_id,
            )
        except QuotaExhausted as exc:
            return _handoff_after_quota(paths, db, events, config, args, str(exc))
        except CloudError as exc:
            console.print(f"[red]cloud error:[/red] {exc}")
            return 1
        finally:
            broker.close()

    console.print(result.text)
    console.print(
        f"[dim]{result.provider}/{result.model}: {result.tokens} tokens, "
        f"${result.cost_usd:.4f}[/dim]"
    )
    return 0


def cmd_cloud_handoffs(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        if args.show is not None:
            pack = load_handoff(db, args.show)
            if pack is None:
                console.print(f"[red]unknown handoff:[/red] {args.show}")
                return 1
            console.print(pack.to_prompt())
            return 0
        rows = db.recent_handoffs(args.limit)
    if not rows:
        console.print("No saved handoffs.")
        return 0
    table = Table(title=f"Last {len(rows)} handoffs")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Task", overflow="fold")
    table.add_column("Goal", overflow="fold")
    table.add_column("Reason", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["created_at"],
            row["task_id"] or "-",
            row["goal"],
            row["reason"],
        )
    console.print(table)
    console.print("Show one with: kivi cloud handoff --show <id>")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    registry = build_registry()
    specs = registry.specs()
    if getattr(args, "json", False):
        payload = [
            {
                "name": spec.name,
                "description": spec.description,
                "risk": spec.risk,
                "capabilities": list(spec.capabilities),
                "schema": spec.schema,
            }
            for spec in specs
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    table = Table(title="Kivi tools")
    table.add_column("Name", style="bold")
    table.add_column("Risk")
    table.add_column("Capabilities")
    table.add_column("Description", overflow="fold")
    for spec in specs:
        table.add_row(spec.name, spec.risk, ", ".join(spec.capabilities), spec.description)
    console.print(table)
    console.print("Run one with: kivi tools run <name> --args '<json>' [--dry-run] [--approve]")
    return 0


def cmd_tool_run(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = build_registry()
    tool = registry.get(args.name)
    if tool is None:
        console.print(f"[red]unknown tool:[/red] {args.name}")
        console.print(f"Known tools: {', '.join(registry.names())}")
        return 1
    try:
        payload = json.loads(args.args)
    except json.JSONDecodeError as exc:
        console.print(f"[red]invalid --args JSON:[/red] {exc}")
        return 2
    if not isinstance(payload, dict):
        console.print("[red]--args must be a JSON object[/red]")
        return 2

    with _state(paths) as (db, events):
        zones = ProtectedZones(paths.root)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(paths.root),
            events=events,
        )
        task_id = args.task_id or f"cli-{uuid.uuid4().hex[:8]}"
        grant = permissions.issue(task_id, tool.capabilities)
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=paths.root,
            events=events,
        )
        result = executor.run(
            args.name,
            payload,
            grant=grant,
            approved=args.approve,
            dry_run=args.dry_run,
        )

    if result.ok:
        console.print(result.output)
        if result.truncated:
            console.print("[yellow]output truncated[/yellow]")
        return 0
    console.print(f"[red]tool failed:[/red] {result.error}")
    if "confirmation required" in result.error:
        console.print("Review the dry run, then re-run with --approve to confirm.")
    return 1


def cmd_audit(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        rows = db.recent_audit(args.limit)
    if not rows:
        console.print("Audit log is empty.")
        return 0

    table = Table(title=f"Last {len(rows)} audit entries")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Actor")
    table.add_column("Action")
    table.add_column("Target", overflow="fold")
    table.add_column("Decision")
    table.add_column("Reason", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["ts"],
            row["actor"],
            row["action"],
            row["target"] or "",
            row["decision"],
            row["reason"] or "",
        )
    console.print(table)
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    console.print("[green]config is valid[/green]")
    console.print(f"voice: {config.voice.provider} {config.voice.model}/{config.voice.speaker}")
    console.print(f"avatar: {config.avatar.mode}, outfit {config.avatar.outfit}")
    console.print(
        f"shell: mode {config.shell.mode}, explorer fallback {config.shell.explorer_fallback}"
    )
    console.print(f"mail: {config.mail.provider}, drafts only {config.mail.drafts_only}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kivi", description="Kivi local assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check hardware and local runtimes")
    doctor.set_defaults(func=cmd_doctor)

    models = sub.add_parser("models", help="show model gateway settings and installed models")
    models.set_defaults(func=cmd_models)

    chat = sub.add_parser("chat", help="chat placeholder (real answers: kivi ask)")
    chat.add_argument("prompt", nargs="?", default="", help="prompt text")
    chat.set_defaults(func=cmd_chat)

    ask = sub.add_parser("ask", help="ask a local model through the Model Gateway")
    ask.add_argument("prompt", help="prompt text")
    ask.add_argument("-m", "--model", default=None, help="model id from config/models.yaml")
    ask.add_argument("--keep-alive", default="5m", help="Ollama keep_alive after the answer")
    ask.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout in seconds")
    ask.add_argument("--stream", action="store_true", help="print answer chunks as they arrive")
    ask.add_argument("--num-predict", type=int, default=512, help="maximum generated tokens")
    ask.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="seconds to wait behind a pinned model instead of failing",
    )
    ask.set_defaults(func=cmd_ask)

    embed = sub.add_parser("embed", help="embed text with the embedding model")
    embed.add_argument("text", help="text to embed")
    embed.add_argument("-m", "--model", default=None, help="embedding model id")
    embed.add_argument("--keep-alive", default="5m", help="Ollama keep_alive after the request")
    embed.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout in seconds")
    embed.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="seconds to wait behind a pinned model instead of failing",
    )
    embed.set_defaults(func=cmd_embed)

    cloud = sub.add_parser("cloud", help="cloud providers, quotas and policy")
    cloud_sub = cloud.add_subparsers(dest="cloud_command")
    cloud_status = cloud_sub.add_parser("status", help="provider quota and key status")
    cloud_status.set_defaults(func=cmd_cloud_status)

    cloud_scout = cloud_sub.add_parser("scout", help="find and approve a free provider")
    cloud_scout.add_argument(
        "-c",
        "--capability",
        choices=["llm", "image", "video", "embedding"],
        default="llm",
    )
    cloud_scout.add_argument("--data-class", default="public")
    cloud_scout.add_argument(
        "--allow-training",
        action="store_true",
        help="permit candidates whose free tier may train on data",
    )
    cloud_scout.add_argument(
        "--allow-watermark",
        action="store_true",
        help="permit candidates that always watermark output",
    )
    cloud_scout.add_argument("--include-configured", action="store_true")
    cloud_scout.add_argument("-n", "--limit", type=int, default=3)
    cloud_scout.add_argument("--approve", metavar="ID", help="append this candidate to cloud.yaml")
    cloud_scout.add_argument("--goal", default="", help="save a handoff if nothing matches")
    cloud_scout.add_argument("--reason", default="", help="handoff reason")
    cloud_scout.add_argument("--next-step", default="", help="handoff next step")
    cloud_scout.set_defaults(func=cmd_cloud_scout)

    cloud_ask = cloud_sub.add_parser(
        "ask",
        help="call a free cloud provider; on quota exhaustion checkpoint and scout",
    )
    cloud_ask.add_argument("prompt")
    cloud_ask.add_argument("-p", "--provider", default="", help="provider id from cloud.yaml")
    cloud_ask.add_argument("-m", "--model", default="", help="model id; provider default if empty")
    cloud_ask.add_argument(
        "--data-class",
        default="private",
        choices=["public", "internal", "private", "secret"],
    )
    cloud_ask.add_argument("--max-tokens", type=int, default=512)
    cloud_ask.add_argument("--task-id", default="")
    cloud_ask.add_argument("--goal", default="", help="handoff goal; defaults to the prompt")
    cloud_ask.add_argument("--next-step", default="", help="handoff next step")
    cloud_ask.add_argument(
        "--allow-watermark",
        action="store_true",
        help="permit Scout candidates that watermark output",
    )
    cloud_ask.set_defaults(func=cmd_cloud_ask)

    cloud_handoff = cloud_sub.add_parser("handoff", help="list or show saved handoffs")
    cloud_handoff.add_argument("-n", "--limit", type=int, default=20)
    cloud_handoff.add_argument("--show", type=int, default=None, metavar="ID")
    cloud_handoff.set_defaults(func=cmd_cloud_handoffs)

    cloud.set_defaults(func=cmd_cloud_status)

    tools = sub.add_parser("tools", help="list and run tools")
    tools_sub = tools.add_subparsers(dest="tools_command")
    tools_list = tools_sub.add_parser("list", help="list registered tools")
    tools_list.add_argument("--json", action="store_true", help="print specs as JSON")
    tools_list.set_defaults(func=cmd_tools)
    tools_run = tools_sub.add_parser("run", help="run a tool through the permission engine")
    tools_run.add_argument("name", help="tool name, for example fs.read")
    tools_run.add_argument("--args", default="{}", help="tool arguments as a JSON object")
    tools_run.add_argument("--dry-run", action="store_true", help="show the preview only")
    tools_run.add_argument(
        "--approve",
        action="store_true",
        help="confirm an R2/R3 action after reviewing the preview",
    )
    tools_run.add_argument("--task-id", default="", help="task id for the grant and audit")
    tools_run.set_defaults(func=cmd_tool_run)
    tools.set_defaults(func=cmd_tools)

    events = sub.add_parser("events", help="show recent events")
    events.add_argument("-n", "--limit", type=int, default=20)
    events.set_defaults(func=cmd_events)

    audit = sub.add_parser("audit", help="show recent audit entries")
    audit.add_argument("-n", "--limit", type=int, default=20)
    audit.set_defaults(func=cmd_audit)

    config_cmd = sub.add_parser("config", help="config utilities")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="validate config/kivi.yaml")
    check.set_defaults(func=cmd_config_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        console.print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
