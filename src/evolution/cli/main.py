"""CLI entry point for the evo command."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from evolution.adapters._paths import tasks_root
from evolution.core.models import SkillFrontmatter
from evolution.core.parser import parse_skill_md, write_skill_md
from evolution.core.store import SkillStore
from evolution.core.validator import validate_skill
from evolution.eval.task import Task, discover_tasks
from evolution.evolution.reflect import (
    collect_reflections,
    reflect_failing_traces,
    rewrite_skill_with_audit,
)
from evolution.evolution.rewrite_outcome import (
    CONTROLLED_STATUSES,
    PROMOTED_ELIGIBLE,
    REWRITE_ERROR_INTERNAL,
)
from evolution.llm.backend import get_backend
from evolution.splits import LeakageError, SplitManifest

app = typer.Typer(
    name="evo",
    help="Evolution: trace-driven skill lifecycle for LLM agents.",
    no_args_is_help=True,
)
console = Console()

from evolution.cli.bench import bench as _bench_app

task_app = typer.Typer(help="Create, list, prepare, and evaluate task corpora.")
skill_app = typer.Typer(help="Create, inspect, validate, and version skills.")
run_app = typer.Typer(help="Run task attempts and benchmark loops.")
trace_app = typer.Typer(help="List and inspect recorded task traces.")
evolve_app = typer.Typer(help="Reflect on traces and rewrite candidate skills.")
paper_app = typer.Typer(help="Optional paper-reproduction utilities.")

app.add_typer(task_app, name="task")
app.add_typer(skill_app, name="skill")
app.add_typer(run_app, name="run")
app.add_typer(trace_app, name="trace")
app.add_typer(evolve_app, name="evolve")
app.add_typer(paper_app, name="paper")
app.add_typer(_bench_app, name="bench")


def _get_store(project_dir: Optional[Path] = None, experiment: Optional[str] = None) -> SkillStore:
    if experiment:
        from evolution.core.experiments import get_experiment_store

        project = project_dir or Path.cwd()
        return get_experiment_store(project, experiment)
    project = project_dir or Path.cwd()
    return SkillStore(project_dir=project, user_dir=Path.home())


def _get_container_store(project_dir: Optional[Path] = None):
    from evolution.core.containers import ContainerStore

    project = project_dir or Path.cwd()
    return ContainerStore(project_dir=project, user_dir=Path.home())


def _resolve_skills(name: str, store: SkillStore, snapshot: Optional[str] = None) -> list:
    """Resolve a name to a list of Skills. Checks containers first, then individual skills."""

    cstore = _get_container_store()
    if cstore.has(name):
        return cstore.resolve(name, store, snapshot_label=snapshot)
    if store.has(name):
        return [store.get(name)]
    return []


# ------------------------------------------------------------------
# init
# ------------------------------------------------------------------


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Project directory"),
) -> None:
    """Initialize evolution tracking for skills in this directory."""
    store = SkillStore(project_dir=path)
    entries = store.list()
    console.print(f"Discovered {len(entries)} skill(s) in {path.resolve()}")
    for entry in entries:
        evo_dir = entry.path / ".evolution"
        if not evo_dir.exists():
            console.print(f"  Initializing .evolution/ for {entry.name}...")
            evo_dir.mkdir(parents=True, exist_ok=True)
        else:
            console.print(f"  {entry.name}: already initialized")


# ------------------------------------------------------------------
# create
# ------------------------------------------------------------------


@app.command()
def create(
    name: str = typer.Argument(help="Skill name (lowercase, hyphens ok)"),
    description: str = typer.Option(..., "--description", "-d", help="What the skill does"),
    license: Optional[str] = typer.Option(None, "--license", "-l"),
    compatibility: Optional[str] = typer.Option(None, "--compatibility"),
    scope: str = typer.Option("project", help="'project' or 'user'"),
) -> None:
    """Create a new skill scaffold."""
    store = _get_store()
    try:
        skill = store.create(
            name=name,
            description=description,
            license=license,
            compatibility=compatibility,
            scope=scope,
        )
        console.print(f"[green]Created skill:[/green] {skill.name}")
        console.print(f"  Path: {skill.path}")
        console.print(f"  Edit: {skill.skill_md_path}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# ------------------------------------------------------------------
# list
# ------------------------------------------------------------------


@app.command(name="list")
def list_skills(
    status: Optional[str] = typer.Option(None, help="Filter: active, retired, archived"),
) -> None:
    """List all discovered skills."""
    store = _get_store()
    entries = store.list(status=status)

    if not entries:
        console.print("No skills found.")
        return

    table = Table(title="Skills")
    table.add_column("NAME", style="cyan")
    table.add_column("FITNESS", justify="right")
    table.add_column("GEN", justify="right")
    table.add_column("STATUS")
    table.add_column("DESCRIPTION", max_width=50)

    for e in entries:
        fitness = f"{e.fitness:.2f}" if e.fitness is not None else "-"
        gen = str(e.generation) if e.generation is not None else "-"
        status_style = {
            "active": "green",
            "retired": "yellow",
            "archived": "dim",
            "candidate": "blue",
        }.get(e.status, "dim")
        desc = e.description[:50] + ("..." if len(e.description) > 50 else "")
        table.add_row(e.name, fitness, gen, f"[{status_style}]{e.status}[/]", desc)

    console.print(table)


# ------------------------------------------------------------------
# show
# ------------------------------------------------------------------


@app.command()
def show(
    name: str = typer.Argument(help="Skill name"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific version"),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment",
    ),
) -> None:
    """Show full skill content."""
    store = _get_store(experiment=experiment)

    if version is not None:
        try:
            skill = store.get(name)
            version_path = skill.path / ".evolution" / "versions" / version / "SKILL.md"
            if version_path.exists():
                content = version_path.read_text()
                console.print(Panel(Syntax(content, "markdown"), title=f"{name} ({version})"))
            else:
                console.print(f"[red]Version {version!r} not found.[/red]")
        except KeyError:
            console.print(f"[red]Skill {name!r} not found.[/red]")
        return

    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    # Header
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Name", skill.name)
    table.add_row("Description", skill.description)
    if skill.frontmatter.license:
        table.add_row("License", skill.frontmatter.license)
    if skill.frontmatter.compatibility:
        table.add_row("Compatibility", skill.frontmatter.compatibility)
    if skill.fitness is not None:
        table.add_row("Fitness", f"{skill.fitness:.2f}")
    if skill.generation is not None:
        table.add_row("Generation", str(skill.generation))
    if skill.scripts:
        table.add_row("Scripts", ", ".join(skill.scripts))
    if skill.references:
        table.add_row("References", ", ".join(skill.references))
    table.add_row("Path", str(skill.path))
    console.print(table)
    console.print()

    # Body
    console.print(Syntax(skill.body, "markdown"))


# ------------------------------------------------------------------
# validate
# ------------------------------------------------------------------


@app.command()
def validate(
    name: str = typer.Argument(help="Skill name or path"),
) -> None:
    """Validate a skill against the Agent Skills specification."""
    store = _get_store()

    # Resolve to path
    if store.has(name):
        path = store._index[name].path
    else:
        path = Path(name)

    result = validate_skill(path)

    if result.errors:
        console.print("[red bold]ERRORS:[/red bold]")
        for err in result.errors:
            console.print(f"  [red]x[/red] {err}")

    if result.warnings:
        console.print("[yellow bold]WARNINGS:[/yellow bold]")
        for warn in result.warnings:
            console.print(f"  [yellow]![/yellow] {warn}")

    if result.valid:
        console.print(f"[green]v {name} is valid.[/green]")
    else:
        console.print(f"[red]x {name} has validation errors.[/red]")
        raise typer.Exit(1)


# ------------------------------------------------------------------
# search
# ------------------------------------------------------------------


@app.command()
def search(
    query: str = typer.Argument(help="Search query"),
    limit: int = typer.Option(5, "--limit", "-n"),
) -> None:
    """Search skills by keyword."""
    store = _get_store()
    results = store.search(query, limit=limit)

    if not results:
        console.print("No matching skills found.")
        return

    table = Table(title=f"Search: {query!r}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("NAME", style="cyan")
    table.add_column("STATUS")
    table.add_column("DESCRIPTION", max_width=60)

    for i, entry in enumerate(results, 1):
        desc = entry.description[:60] + ("..." if len(entry.description) > 60 else "")
        table.add_row(str(i), entry.name, entry.status, desc)

    console.print(table)


# ------------------------------------------------------------------
# status
# ------------------------------------------------------------------


@app.command()
def status() -> None:
    """Population overview."""
    store = _get_store()
    pop = store.population_status()

    if pop.total == 0:
        console.print("No skills found.")
        return

    table = Table(title="Population Status", show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Total skills", str(pop.total))
    table.add_row("Active", f"[green]{pop.active}[/green]")
    table.add_row("Retired", f"[yellow]{pop.retired}[/yellow]")
    table.add_row("Archived", f"[dim]{pop.archived}[/dim]")
    if pop.mean_fitness is not None:
        table.add_row("Mean fitness", f"{pop.mean_fitness:.2f}")
    if pop.best:
        table.add_row("Best", f"{pop.best[0]} ({pop.best[1]:.2f})")
    if pop.worst:
        table.add_row("Worst", f"{pop.worst[0]} ({pop.worst[1]:.2f})")
    table.add_row("Total generations", str(pop.total_generations))
    console.print(table)


# ------------------------------------------------------------------
# lineage
# ------------------------------------------------------------------


@app.command()
def lineage(
    name: str = typer.Argument(help="Skill name"),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment",
    ),
) -> None:
    """Show ancestry tree for a skill."""
    from evolution.lineage.tracker import LineageTracker

    store = _get_store(experiment=experiment)
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    tracker = LineageTracker(skill)
    entries = tracker.lineage

    if not entries:
        console.print("No lineage recorded.")
        return

    tree = Tree(f"[bold]{name}[/bold] lineage")
    current_node = tree
    for entry in entries:
        status_style = {
            "active": "green",
            "retired": "yellow",
            "archived": "dim",
            "candidate": "blue",
        }.get(entry.status, "dim")

        fitness_str = f"fitness={entry.fitness:.2f}" if entry.fitness is not None else "fitness=-"
        marker = " << current" if entry.status == "active" else ""
        label = (
            f"[bold]{entry.version}[/bold] -- {entry.origin} -- "
            f"{fitness_str}  [{status_style}]{entry.status}[/]{marker}"
        )
        current_node = current_node.add(label)

    console.print(tree)


# ------------------------------------------------------------------
# diff
# ------------------------------------------------------------------


@app.command(name="diff")
def diff_cmd(
    name: str = typer.Argument(help="Skill name"),
    v1: Optional[str] = typer.Argument(
        None, help="First version (omit both to diff working copy vs active)"
    ),
    v2: Optional[str] = typer.Argument(None, help="Second version"),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment",
    ),
) -> None:
    """Diff two versions, or show uncommitted changes.

    Examples:

      evo diff docx            # working copy vs last committed version
      evo diff docx v1 v2      # compare two versions
      evo diff pdf -e reflect-ablation
    """
    from evolution.lineage.diff import skill_diff
    from evolution.lineage.tracker import LineageTracker
    from evolution.lineage.version import VersionStore

    store = _get_store(experiment=experiment)
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    vs = VersionStore(skill)

    if v1 is None and v2 is None:
        # Diff working copy vs active version
        tracker = LineageTracker(skill)
        active_ver = tracker.current_version
        if not active_ver or not vs.exists(active_ver):
            console.print("[yellow]No committed version to compare against.[/yellow]")
            raise typer.Exit(1)
        old = vs.load(active_ver)
        new = skill  # working copy
        console.print(f"Comparing [cyan]{active_ver}[/cyan] (committed) vs working copy\n")
    elif v1 is not None and v2 is not None:
        try:
            old = vs.load(v1)
        except KeyError:
            console.print(f"[red]Version {v1!r} not found.[/red]")
            raise typer.Exit(1)
        try:
            new = vs.load(v2)
        except KeyError:
            console.print(f"[red]Version {v2!r} not found.[/red]")
            raise typer.Exit(1)
    else:
        console.print(
            "[red]Provide both versions (evo diff name v1 v2) or none (evo diff name).[/red]"
        )
        raise typer.Exit(1)

    d = skill_diff(old, new)

    if not d.text_diff and not d.frontmatter_changes:
        console.print("[dim]No changes.[/dim]")
        return

    console.print(f"[bold]Summary:[/bold] {d.summary}\n")

    if d.text_diff:
        console.print(Syntax(d.text_diff, "diff"))
    else:
        console.print("[dim]No text changes.[/dim]")


# ------------------------------------------------------------------
# history
# ------------------------------------------------------------------


@app.command()
def history(
    name: str = typer.Argument(help="Skill name"),
) -> None:
    """Show fitness trajectory over versions."""
    from evolution.lineage.traces import TraceStore
    from evolution.lineage.tracker import LineageTracker

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    tracker = LineageTracker(skill)
    entries = tracker.lineage
    ts = TraceStore(skill)

    table = Table(title=f"{name} -- version history")
    table.add_column("VERSION", style="cyan")
    table.add_column("FITNESS", justify="right")
    table.add_column("DELTA", justify="right")
    table.add_column("TRACES", justify="right")
    table.add_column("ORIGIN")
    table.add_column("STATUS")
    table.add_column("DATE")

    prev_fitness: Optional[float] = None
    for entry in entries:
        # Prefer real fitness from traces; fall back to lineage metadata
        trace_fitness = ts.fitness(entry.version)
        trace_count = ts.count(skill_version=entry.version)
        fitness = trace_fitness if trace_fitness is not None else entry.fitness

        fitness_str = f"{fitness:.1%}" if fitness is not None else "-"
        if fitness is not None and prev_fitness is not None:
            delta = fitness - prev_fitness
            delta_str = f"[green]+{delta:.1%}[/]" if delta > 0 else f"[red]{delta:.1%}[/]"
        else:
            delta_str = "-"

        status_style = {
            "active": "green",
            "retired": "yellow",
            "archived": "dim",
        }.get(entry.status, "dim")
        date_str = entry.timestamp.strftime("%Y-%m-%d") if entry.timestamp else "-"

        table.add_row(
            entry.version,
            fitness_str,
            delta_str,
            str(trace_count) if trace_count > 0 else "-",
            entry.origin,
            f"[{status_style}]{entry.status}[/]",
            date_str,
        )
        if fitness is not None:
            prev_fitness = fitness

    console.print(table)


# ------------------------------------------------------------------
# promote
# ------------------------------------------------------------------


@app.command()
def promote(
    name: str = typer.Argument(help="Skill name"),
    version: str = typer.Argument(help="Version to promote (e.g. v1, v2)"),
) -> None:
    """Switch a skill to a specific version."""
    from evolution.lineage.tracker import LineageTracker

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    tracker = LineageTracker(skill)

    try:
        tracker.promote(version)
    except KeyError:
        console.print(f"[red]Version {version!r} not found for {name}.[/red]")
        raise typer.Exit(1)

    console.print(f"[green]{name}[/green] switched to [cyan]{version}[/cyan]")


# ------------------------------------------------------------------
# commit
# ------------------------------------------------------------------


@app.command()
def commit(
    name: str = typer.Argument(help="Skill name"),
    message: Optional[str] = typer.Option(
        None,
        "--message",
        "-m",
        help="Description of changes",
    ),
    no_promote: bool = typer.Option(
        False,
        "--no-promote",
        help="Save version but don't make it active",
    ),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment",
    ),
) -> None:
    """Snapshot the current skill state as a new version.

    Examples:

      evo commit docx -m "fixed conditional handling"
      evo commit docx --no-promote
      evo commit pdf -m "r1 rewrite" --experiment reflect-ablation
    """
    from datetime import datetime

    from evolution.core.models import LineageEntry
    from evolution.core.validator import validate_skill
    from evolution.lineage.diff import skill_diff
    from evolution.lineage.tracker import LineageTracker
    from evolution.lineage.version import VersionStore

    store = _get_store(experiment=experiment)
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    # Validate first
    vr = validate_skill(skill.path)
    if not vr.valid:
        for err in vr.errors:
            console.print(f"  [red]x[/red] {err}")
        console.print("[red]Fix validation errors before committing.[/red]")
        raise typer.Exit(1)

    vs = VersionStore(skill)
    tracker = LineageTracker(skill)

    # Check for changes vs active version
    active_ver = tracker.current_version
    if active_ver and vs.exists(active_ver):
        old = vs.load(active_ver)
        d = skill_diff(old, skill)
        if not d.text_diff and not d.frontmatter_changes:
            console.print("[yellow]No changes to commit.[/yellow]")
            raise typer.Exit(0)

    # Auto-increment version
    new_ver = tracker.next_version_label()

    # Snapshot
    vs.save(new_ver)

    # Record lineage
    entry = LineageEntry(
        version=new_ver,
        parent=active_ver or None,
        timestamp=datetime.now(),
        origin="manual",
        mutation_type="manual-edit",
        status="candidate",
    )
    tracker.record(entry)

    if not no_promote:
        tracker.promote(new_ver)
        console.print(f"[green]{name}[/green] committed as [cyan]{new_ver}[/cyan] (active)")
    else:
        console.print(f"[green]{name}[/green] committed as [cyan]{new_ver}[/cyan] (candidate)")

    if message:
        console.print(f"  {message}")


# ------------------------------------------------------------------
# discard
# ------------------------------------------------------------------


@app.command()
def discard(
    name: str = typer.Argument(help="Skill name"),
) -> None:
    """Discard uncommitted changes, restoring the last committed version.

    Examples:

      evo discard docx
    """
    from evolution.lineage.diff import skill_diff
    from evolution.lineage.tracker import LineageTracker
    from evolution.lineage.version import VersionStore

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    vs = VersionStore(skill)
    tracker = LineageTracker(skill)
    active_ver = tracker.current_version

    if not active_ver or not vs.exists(active_ver):
        console.print("[yellow]No committed version to restore.[/yellow]")
        raise typer.Exit(1)

    # Check if there are changes
    old = vs.load(active_ver)
    d = skill_diff(old, skill)
    if not d.text_diff and not d.frontmatter_changes:
        console.print("[dim]No uncommitted changes.[/dim]")
        return

    # Restore by re-promoting current active (copies SKILL.md back)
    tracker.promote(active_ver)
    console.print(f"[green]{name}[/green] restored to [cyan]{active_ver}[/cyan]")


# ------------------------------------------------------------------
# repair
# ------------------------------------------------------------------


@app.command(name="repair")
def repair_cmd(
    name: str = typer.Argument(help="Skill name"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the rebuilt lineage.json without writing it",
    ),
) -> None:
    """Rebuild a skill's .evolution/lineage.json from on-disk version snapshots.

    Use when `evo lineage|history|commit|promote` fails because lineage.json is
    malformed. Recovery is provenance-light: entries are marked
    origin=import / mutation_type=recovered-from-disk.

    Examples:

      evo repair docx
      evo repair docx --dry-run
    """
    from evolution.lineage.tracker import rebuild_lineage

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    lineage, notes = rebuild_lineage(skill)
    for note in notes:
        console.print(f"[yellow]note:[/yellow] {note}")

    payload = json.dumps(lineage.model_dump(mode="json"), indent=2, default=str)

    if dry_run:
        console.print(payload)
        console.print("[dim](dry run — lineage.json not written)[/dim]")
        return

    path = skill.path / ".evolution" / "lineage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)
    console.print(
        f"[green]{name}[/green] lineage repaired: {len(lineage.lineage)} version(s), "
        f"active [cyan]{lineage.current_version}[/cyan]."
    )


# ------------------------------------------------------------------
# delete-version
# ------------------------------------------------------------------


@app.command(name="delete-version")
def delete_version_cmd(
    name: str = typer.Argument(help="Skill name"),
    version: str = typer.Argument(help="Version to delete (e.g. v4)"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Delete even if version has children (reparents them)",
    ),
) -> None:
    """Delete a skill version from lineage and disk.

    Cannot delete the active version — promote another first.
    Cannot delete versions with children unless --force is used.

    Examples:

      evo delete-version docx v4
      evo delete-version docx v2 --force
    """
    from evolution.lineage.tracker import LineageTracker

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    tracker = LineageTracker(skill)

    try:
        tracker.delete_version(version, force=force)
    except KeyError:
        console.print(f"[red]Version {version!r} not found for {name}.[/red]")
        raise typer.Exit(1)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]{name}[/green] version [cyan]{version}[/cyan] deleted")


# ------------------------------------------------------------------
# import
# ------------------------------------------------------------------


@app.command(name="import")
def import_cmd(
    source: Path = typer.Argument(help="Path to skill directory or SKILL.md"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Override skill name"),
) -> None:
    """Import an existing skill into the store."""
    store = _get_store()
    try:
        skill = store.import_skill(source, name=name)
        console.print(f"[green]Imported:[/green] {skill.name}")
        console.print(f"  Path: {skill.path}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command(name="import-skillsbench")
def import_skillsbench(
    repo: Path = typer.Argument(help="Path to SkillsBench repository"),
    skills: Optional[str] = typer.Option(
        None,
        "--skills",
        "-s",
        help="Comma-separated skill names to import (default: all)",
    ),
    theme: str = typer.Option("", "--theme", "-t", help="Theme prefix for metadata"),
) -> None:
    """Bulk import skills from a SkillsBench repository."""
    from evolution.importers.skillsbench import discover_skills, import_skills

    store = _get_store()

    names = [n.strip() for n in skills.split(",")] if skills else None

    # Show what we'll import
    entries = discover_skills(repo)
    if names:
        entries = [e for e in entries if e.name in set(names)]

    console.print(f"Found {len(entries)} skill(s) to import from {repo}")

    imported = import_skills(store, repo, names=names, theme_prefix=theme)

    if imported:
        table = Table(title=f"Imported {len(imported)} skill(s)")
        table.add_column("NAME", style="cyan")
        table.add_column("STATUS", style="green")
        for n in imported:
            table.add_row(n, "imported")
        console.print(table)
    else:
        console.print("[yellow]No new skills imported.[/yellow]")


@app.command(name="import-sb-bench")
def import_sb_bench(
    repo: Path = typer.Argument(help="Path to sb-bench repository"),
    role: Optional[str] = typer.Option(
        None,
        "--role",
        "-r",
        help="Filter by role: ds, de, infra",
    ),
    difficulty: Optional[str] = typer.Option(
        None,
        "--difficulty",
        help="Filter by difficulty: easy, medium, hard",
    ),
    tasks_filter: Optional[str] = typer.Option(
        None,
        "--tasks",
        "-t",
        help="Comma-separated task IDs (e.g. de/E1,infra/E2)",
    ),
    with_skills: bool = typer.Option(
        False,
        "--with-skills",
        help="Also import skills into the skill store",
    ),
    with_containers: bool = typer.Option(
        False,
        "--with-containers",
        help="Also create skill containers for each task",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing tasks",
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Destination tasks dir (default: ./tasks)",
    ),
) -> None:
    """Import tasks (and optionally skills) from sb-bench repository."""
    from evolution.importers.sb_bench import (
        discover_skills as sb_discover_skills,
    )
    from evolution.importers.sb_bench import (
        discover_tasks as sb_discover,
    )
    from evolution.importers.sb_bench import (
        import_tasks as sb_import_tasks,
    )

    dest = tasks_dir or Path.cwd() / "tasks"
    task_ids = [t.strip() for t in tasks_filter.split(",")] if tasks_filter else None

    # Show what we'll import
    found = sb_discover(repo, role=role, difficulty=difficulty)
    if task_ids:
        id_set = set(task_ids)
        found = [
            t for t in found if t.task_id in id_set or any(t.task_id.startswith(p) for p in id_set)
        ]

    if not found:
        console.print("[yellow]No matching tasks found.[/yellow]")
        raise typer.Exit(0)

    console.print(f"Found [bold]{len(found)}[/bold] task(s) to import from {repo}\n")
    for t in found:
        console.print(f"  {t.task_id:<30} {t.difficulty:<8} skills: {', '.join(t.skill_names)}")
    console.print()

    # Import tasks
    imported = sb_import_tasks(
        repo,
        dest,
        role=role,
        difficulty=difficulty,
        task_ids=task_ids,
        force=force,
    )

    if imported:
        table = Table(title=f"Imported {len(imported)} task(s)")
        table.add_column("TASK", style="cyan")
        table.add_column("STATUS", style="green")
        for name in imported:
            table.add_row(name, "imported")
        console.print(table)
    else:
        console.print("[yellow]No new tasks imported.[/yellow]")

    # Import skills if requested
    if with_skills and imported:
        store = _get_store()
        all_skills = sb_discover_skills(repo)

        # Filter to skills used by imported tasks
        imported_set = set(imported)
        task_skill_map: dict[str, list[str]] = {}
        for t in found:
            evo_name = f"sb-{t.role}-{t.task_id.split('/', 1)[-1]}"
            m = re.match(r"^[A-Z]\d+-(.+)$", t.task_id.split("/", 1)[-1])
            short = m.group(1) if m else t.task_id.split("/", 1)[-1]
            evo_name = f"sb-{t.role}-{short}"
            if evo_name in imported_set:
                task_skill_map[t.task_id] = t.skill_names

        skill_names_needed = set()
        for names in task_skill_map.values():
            skill_names_needed.update(names)

        relevant = [s for s in all_skills if s["name"] in skill_names_needed]
        skill_count = 0
        for s in relevant:
            if store.has(s["name"]):
                continue
            try:
                store.import_skill(
                    source=s["skill_dir"],
                    metadata={
                        "source": "sb-bench",
                        "source-task": s["task_id"],
                        "role": s["role"],
                    },
                )
                skill_count += 1
                console.print(f"  Imported skill: [green]{s['name']}[/green]")
            except Exception as exc:
                console.print(f"  [red]Failed: {s['name']}: {exc}[/red]")

        console.print(f"\n[green]{skill_count} skill(s) imported.[/green]")

    # Create containers if requested
    if with_containers and imported:
        cstore = _get_container_store()

        for t in found:
            m = re.match(r"^[A-Z]\d+-(.+)$", t.task_id.split("/", 1)[-1])
            short = m.group(1) if m else t.task_id.split("/", 1)[-1]
            evo_name = f"sb-{t.role}-{short}"

            if evo_name not in set(imported):
                continue
            if not t.skill_names:
                continue

            container_name = evo_name
            if cstore.has(container_name):
                console.print(
                    f"  Container [yellow]{container_name}[/yellow] already exists, skipping"
                )
                continue

            try:
                cstore.create(
                    name=container_name,
                    description=f"Skills for {t.name} ({t.task_id})",
                    skills=t.skill_names,
                    metadata={
                        "source": "sb-bench",
                        "source-task": t.task_id,
                        "role": t.role,
                    },
                )
                console.print(
                    f"  Created container: [green]{container_name}[/green] ({', '.join(t.skill_names)})"
                )
            except Exception as exc:
                console.print(f"  [red]Failed container {container_name}: {exc}[/red]")


# ------------------------------------------------------------------
# containers
# ------------------------------------------------------------------


@app.command(name="containers")
def list_containers() -> None:
    """List all skill containers."""
    cstore = _get_container_store()
    containers = cstore.list()

    if not containers:
        console.print("[yellow]No containers found.[/yellow]")
        return

    table = Table(title=f"Skill Containers ({len(containers)})")
    table.add_column("NAME", style="cyan")
    table.add_column("SKILLS", style="green")
    table.add_column("DESCRIPTION")

    for c in containers:
        table.add_row(c.name, ", ".join(c.skill_names), c.description[:60])
    console.print(table)


@app.command(name="show-container")
def show_container(
    name: str = typer.Argument(help="Container name"),
) -> None:
    """Show details of a skill container."""
    cstore = _get_container_store()
    try:
        c = cstore.get(name)
    except KeyError:
        console.print(f"[red]Container {name!r} not found.[/red]")
        raise typer.Exit(1)

    console.print(Panel(f"[bold]{c.name}[/bold]\n{c.description}", title="Container"))
    console.print(f"\n[bold]Skills ({len(c.skills)}):[/bold]")

    store = _get_store()
    for i, ref in enumerate(c.skills, 1):
        sname = ref.name
        marker = "[bold green]primary[/bold green]" if i == 1 else ""
        found = "[green]✓[/green]" if store.has(sname) else "[red]✗[/red]"
        console.print(f"  {i}. {found} {sname} {marker}")

    if c.metadata:
        console.print("\n[bold]Metadata:[/bold]")
        for k, v in c.metadata.items():
            console.print(f"  {k}: {v}")


@app.command(name="create-container")
def create_container(
    name: str = typer.Argument(help="Container name"),
    description: str = typer.Option(
        ...,
        "--description",
        "-d",
        help="Description of the container",
    ),
    skills: str = typer.Option(
        ...,
        "--skills",
        help="Comma-separated skill names",
    ),
) -> None:
    """Create a new skill container."""
    cstore = _get_container_store()
    skill_list = [s.strip() for s in skills.split(",")]

    try:
        c = cstore.create(name=name, description=description, skills=skill_list)
        console.print(f"[green]Created container:[/green] {c.name}")
        console.print(f"  Skills: {', '.join(c.skill_names)}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command(name="delete-container")
def delete_container(
    name: str = typer.Argument(help="Container name"),
) -> None:
    """Delete a skill container."""
    cstore = _get_container_store()
    try:
        cstore.delete(name)
        console.print(f"[green]Deleted container:[/green] {name}")
    except KeyError:
        console.print(f"[red]Container {name!r} not found.[/red]")
        raise typer.Exit(1)


@app.command(name="snapshot-container")
def snapshot_container_cmd(
    name: str = typer.Argument(help="Container name"),
    message: str = typer.Option(
        ...,
        "--message",
        "-m",
        help="Snapshot message",
    ),
) -> None:
    """Record which skill versions this container currently uses."""
    cstore = _get_container_store()
    store = _get_store()
    try:
        label = cstore.snapshot(name, message, skill_store=store)
        entries = cstore.snapshots(name)
        last = entries[-1]
        console.print(f"[green]Snapshot {label}:[/green] {name}")
        for skill, ver in last["skills"].items():
            console.print(f"  {skill} @ {ver}")
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command(name="container-snapshots")
def container_snapshots_cmd(
    name: str = typer.Argument(help="Container name"),
) -> None:
    """Show snapshot history for a container."""
    cstore = _get_container_store()
    try:
        entries = cstore.snapshots(name)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not entries:
        console.print(f"[yellow]No snapshots for {name!r}.[/yellow]")
        return

    table = Table(title=f"Container Snapshots: {name}")
    table.add_column("LABEL", style="cyan")
    table.add_column("SKILLS")
    table.add_column("MESSAGE")
    table.add_column("TIMESTAMP")

    for e in entries:
        skills_str = ", ".join(f"{k}@{v}" for k, v in e.get("skills", {}).items())
        table.add_row(
            e["label"],
            skills_str,
            e.get("message", ""),
            e.get("timestamp", "")[:19],
        )
    console.print(table)


# ------------------------------------------------------------------
# reflect & rewrite
# ------------------------------------------------------------------


def _task_instruction_for_cli_task(task: str, tasks_dir: Optional[Path]) -> str:
    """Resolve a task filter to non-empty task instructions or fail loudly."""

    tasks_path = Path(tasks_dir or tasks_root()).resolve()
    for t in discover_tasks(tasks_path):
        if t.name == task:
            if not str(t.instruction or "").strip():
                console.print(f"[red]Task {task!r} in {tasks_path} has an empty instruction.[/red]")
                raise typer.Exit(1)
            return t.instruction
    console.print(f"[red]Task {task!r} not found in tasks directory {tasks_path}.[/red]")
    raise typer.Exit(1)


def _task_obj_for_cli_task(task: str, tasks_dir: Optional[Path]) -> Task:
    """Resolve a task filter to its full Task object (agentic-session workspace)."""
    tasks_path = Path(tasks_dir or tasks_root()).resolve()
    for t in discover_tasks(tasks_path):
        if t.name == task:
            return t
    console.print(f"[red]Task {task!r} not found in tasks directory {tasks_path}.[/red]")
    raise typer.Exit(1)


@app.command(name="reflect")
def reflect_cmd(
    skill_name: str = typer.Argument(help="Skill name"),
    model: str = typer.Option(
        "openrouter/anthropic/claude-sonnet-4",
        "--model",
        "-m",
        help="Model for analysis",
    ),
    task: Optional[str] = typer.Option(
        None,
        "--task",
        "-t",
        help="Filter traces by task name",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        help="Max traces to analyze (default: no cap — reflect ALL eligible failing traces)",
    ),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment",
    ),
    trace_model: Optional[str] = typer.Option(
        None,
        "--trace-model",
        help="Filter traces by model (substring match, e.g. 'sonnet', 'llama')",
    ),
    api_base: Optional[str] = typer.Option(
        None,
        "--api-base",
        help="API base URL",
    ),
    manifest: Optional[Path] = typer.Option(
        None,
        "--manifest",
        help=(
            "Split manifest JSON. When set, only traces from the skill-"
            "evolution scope (train) are analyzed."
        ),
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--tasks-dir",
        help=(
            "Tasks directory for task-instruction lookup (default: $EVO_TASKS_ROOT or <repo>/tasks)."
        ),
    ),
) -> None:
    """Analyze failing traces with an LLM and save reflections.

    When `--manifest` is set, reflections are restricted to train-scope
    traces; a `LeakageError` fails the run if a val/test trace is about to
    influence skill content.
    """
    store = _get_store(experiment=experiment)
    if not store.has(skill_name):
        console.print(f"[red]Skill {skill_name!r} not found.[/red]")
        raise typer.Exit(1)
    skill = store.get(skill_name)

    split_manifest = None
    if manifest is not None:
        split_manifest = SplitManifest.load(manifest)
        console.print(
            f"  manifest: {split_manifest.name} ({split_manifest.sha256[:12]}) "
            "scope=skill_evolution (train-only)"
        )

    from evolution.adapters.evolution_adapter import (
        OracleOnPaperError,
        _publication_preflight,
    )

    try:
        _publication_preflight(split_manifest.name if split_manifest else None)
    except OracleOnPaperError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    # Get task instruction if task filter is set
    task_instruction = ""
    if task:
        task_instruction = _task_instruction_for_cli_task(task, tasks_dir)

    backend_kwargs = {}
    if api_base:
        backend_kwargs["api_base"] = api_base
    backend = get_backend(model, **backend_kwargs)

    console.print(f"Analyzing failing traces for [cyan]{skill_name}[/cyan]...")
    if task:
        console.print(f"  Task filter: {task}")
    if trace_model:
        console.print(f"  Trace model: {trace_model}")
    if experiment:
        console.print(f"  Experiment: {experiment}")

    _require_v3 = bool(split_manifest is not None or os.environ.get("EVO_REQUIRE_V3_EVIDENCE"))
    try:
        reflections = reflect_failing_traces(
            backend,
            skill,
            task_instruction,
            task_filter=task,
            trace_model=trace_model,
            limit=limit,
            manifest=split_manifest,
            scope="skill_evolution",
            strict=True,
            require_v3_evidence=_require_v3,
        )
    except Exception as exc:
        console.print(f"[red]Reflect failed:[/red] {exc}")
        raise typer.Exit(1)

    if not reflections:
        console.print("[yellow]No failing traces without reflections found.[/yellow]")
        return

    for r in reflections:
        if "error" in r:
            console.print(f"  [red]✗[/red] {r['trace_id']}: {r['error']}")
        else:
            severity = r.get("severity", "?")
            pattern = r.get("pattern", "?")
            colors = {"critical": "red", "major": "yellow", "minor": "dim"}
            color = colors.get(severity, "white")
            console.print(
                f"  [{color}]●[/{color}] {r.get('model', '?')} — "
                f"{severity}/{pattern}: {r.get('root_cause', '?')[:80]}"
            )

    console.print(f"\n[green]{len(reflections)} reflections saved.[/green]")


@app.command(name="rewrite")
def rewrite_cmd(
    skill_name: str = typer.Argument(help="Skill name"),
    model: str = typer.Option(
        "openrouter/anthropic/claude-sonnet-4",
        "--model",
        "-m",
        help="Model for rewriting",
    ),
    task: Optional[str] = typer.Option(
        None,
        "--task",
        "-t",
        help="Filter reflections by task",
    ),
    trace_model: Optional[str] = typer.Option(
        None,
        "--trace-model",
        help="Filter reflections by model (substring match)",
    ),
    skill_version: Optional[str] = typer.Option(
        None,
        "--skill-version",
        help="Filter reflections by skill version (e.g. v4)",
    ),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment",
    ),
    api_base: Optional[str] = typer.Option(
        None,
        "--api-base",
        help="API base URL",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print rewritten skill without saving",
    ),
    manifest: Optional[Path] = typer.Option(
        None,
        "--manifest",
        help=(
            "Split manifest JSON. When set, only reflections from train-"
            "scope tasks are used; passing a val/test task via --task fails."
        ),
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--tasks-dir",
        help=(
            "Tasks directory for task-instruction lookup (default: $EVO_TASKS_ROOT or <repo>/tasks)."
        ),
    ),
    include_best_trace_code: bool = typer.Option(
        False,
        "--include-best-trace-code",
        help="Force raw best trace code into the rewrite prompt.",
    ),
    no_best_trace_code: bool = typer.Option(
        False,
        "--no-best-trace-code",
        help=(
            "Force raw best trace code out of the rewrite prompt. Default: yes "
            "without --manifest, no with --manifest."
        ),
    ),
    rewrite_audit_out: Optional[Path] = typer.Option(
        None,
        "--rewrite-audit-out",
        help=(
            "Write structured rewrite audit JSON to this path. Default: "
            "<skill_dir>/.evolution/last_rewrite_audit.json."
        ),
    ),
    rewrite_mode: str = typer.Option(
        "text-return",
        "--rewrite-mode",
        help="text-return (default) | file-edit | agentic-session (needs --task).",
    ),
    rewrite_row_id: str = typer.Option(
        "",
        "--rewrite-row-id",
        help=(
            "Row identifier used for the candidate-debug archive path when "
            "EVO_DEBUG_C1=1. Adapter sets this from the row it is producing."
        ),
    ),
) -> None:
    """Rewrite a skill based on collected reflections.

    When `--manifest` is provided, only reflections from train-scope tasks
    are fed into the rewrite prompt. Passing a val/test task via `--task`
    fails with `LeakageError`.

    Always writes a ``rewrite-audit-v1`` JSON sidecar to
    ``--rewrite-audit-out`` (or the default path) so the adapter can read
    the structured candidate status without parsing stdout.
    """
    store = _get_store(experiment=experiment)
    if not store.has(skill_name):
        console.print(f"[red]Skill {skill_name!r} not found.[/red]")
        raise typer.Exit(1)
    skill = store.get(skill_name)

    split_manifest = None
    if manifest is not None:
        split_manifest = SplitManifest.load(manifest)
        console.print(
            f"  manifest: {split_manifest.name} ({split_manifest.sha256[:12]}) "
            "scope=skill_evolution (train-only)"
        )
        if task:
            split_manifest.assert_no_leakage(
                [task],
                scope="skill_evolution",
                context="rewrite --task",
            )

    reflections = collect_reflections(
        skill,
        task_filter=task,
        trace_model=trace_model,
        skill_version=skill_version,
        manifest=split_manifest,
        scope="skill_evolution",
    )
    if not reflections and rewrite_mode != "agentic-session":
        console.print("[yellow]No reflections found. Run 'evo reflect' first.[/yellow]")
        raise typer.Exit(1)

    # Get task instruction
    task_instruction = ""
    if task:
        task_instruction = _task_instruction_for_cli_task(task, tasks_dir)

    console.print(
        f"Rewriting [cyan]{skill_name}[/cyan] based on "
        f"[green]{len(reflections)}[/green] reflections (mode={rewrite_mode})..."
    )
    if task:
        console.print(f"  Task: {task}")
    if experiment:
        console.print(f"  Experiment: {experiment}")

    backend_kwargs = {}
    if api_base:
        backend_kwargs["api_base"] = api_base
    backend = get_backend(model, **backend_kwargs)
    if include_best_trace_code and no_best_trace_code:
        console.print(
            "[red]Use only one of --include-best-trace-code or --no-best-trace-code.[/red]"
        )
        raise typer.Exit(1)
    best_trace_enabled = (
        True if include_best_trace_code else False if no_best_trace_code else split_manifest is None
    )

    audit_path = rewrite_audit_out or (
        skill.skill_md_path.parent / ".evolution" / "last_rewrite_audit.json"
    )
    audit: dict = {
        "schema": "rewrite-audit-v1",
        "rewrite_mode": rewrite_mode,
        "model_id": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    exit_nonzero = False
    new_body = skill.body  # falls back to parent on rejection or error

    session_mode = rewrite_mode == "agentic-session"
    invalid_reason: str | None = None
    if rewrite_mode not in ("text-return", "file-edit", "agentic-session"):
        invalid_reason = f"invalid --rewrite-mode: {rewrite_mode!r}"
    elif session_mode and not task:
        invalid_reason = "agentic-session requires --task to build the session workspace"

    try:
        if invalid_reason is not None:
            audit.update(
                {
                    "candidate_status": REWRITE_ERROR_INTERNAL,
                    "gate_reason": REWRITE_ERROR_INTERNAL,
                    "reason": invalid_reason,
                    "parent_preservation": None,
                    "codex_usage": None,
                    "codex_diagnostics": None,
                    "tokens_used": None,
                    "candidate_debug_dir": None,
                    "parent_skill_path": None,
                    "parent_body_sha256": None,
                    "candidate_body_sha256": None,
                    "computed_churn": None,
                    "new_headings": None,
                    "edit_budget_frac": None,
                }
            )
            exit_nonzero = True
        else:
            session_task = _task_obj_for_cli_task(task, tasks_dir) if session_mode else None
            outcome = asyncio.run(
                rewrite_skill_with_audit(
                    backend,
                    skill,
                    reflections,
                    task_instruction=task_instruction,
                    task_filter=task,
                    manifest=split_manifest,
                    include_best_trace_code=best_trace_enabled,
                    file_edit=(rewrite_mode == "file-edit"),
                    session=session_mode,
                    task=session_task,
                    audit_root=audit_path.parent,
                    row_id=rewrite_row_id,
                    debug_archive=os.environ.get("EVO_DEBUG_C1") == "1",
                )
            )
            new_body = outcome.body
            audit.update(
                {
                    "candidate_status": outcome.candidate_status,
                    "gate_reason": outcome.candidate_status,
                    "reason": outcome.reason,
                    "parent_preservation": outcome.preservation,
                    "codex_usage": outcome.codex_usage,
                    "codex_diagnostics": outcome.codex_diagnostics,
                    "tokens_used": outcome.tokens_used,
                    "candidate_debug_dir": outcome.candidate_debug_dir,
                    "parent_skill_path": outcome.parent_skill_path,
                    "parent_body_sha256": outcome.parent_body_sha256,
                    "candidate_body_sha256": outcome.candidate_body_sha256,
                    "computed_churn": outcome.computed_churn,
                    "new_headings": outcome.new_headings,
                    "edit_budget_frac": outcome.edit_budget_frac,
                }
            )
            if outcome.candidate_status not in CONTROLLED_STATUSES:
                # Contract violation: callee returned an unknown status.
                audit["candidate_status"] = REWRITE_ERROR_INTERNAL
                audit["gate_reason"] = REWRITE_ERROR_INTERNAL
                exit_nonzero = True
    except Exception as exc:
        audit.update(
            {
                "candidate_status": REWRITE_ERROR_INTERNAL,
                "gate_reason": REWRITE_ERROR_INTERNAL,
                "reason": f"{type(exc).__name__}: {exc!s}"[:500],
                "parent_preservation": None,
                "codex_usage": None,
                "codex_diagnostics": None,
                "tokens_used": None,
                "candidate_debug_dir": None,
                "parent_skill_path": None,
                "parent_body_sha256": None,
                "candidate_body_sha256": None,
                "computed_churn": None,
                "new_headings": None,
                "edit_budget_frac": None,
            }
        )
        exit_nonzero = True
    finally:
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            # If the audit JSON cannot be written, that IS the contract failure.
            exit_nonzero = True
        # Reviewer-facing sidecars next to the audit JSON (see docs/codex_usage.md,
        # docs/codex_usage.md). Best-effort: a sidecar write failure must not
        # promote-or-fail the rewrite outcome itself.
        sidecar_stem = audit_path.parent / audit_path.stem
        last_message_text = (audit.get("codex_diagnostics") or {}).get("last_message") or ""
        if last_message_text:
            with contextlib.suppress(OSError):
                sidecar_stem.with_name(sidecar_stem.name + ".last_message.txt").write_text(
                    last_message_text, encoding="utf-8"
                )
        diff_text = "".join(
            difflib.unified_diff(
                skill.body.splitlines(keepends=True),
                new_body.splitlines(keepends=True),
                fromfile="parent/SKILL.md",
                tofile="candidate/SKILL.md",
            )
        )
        if diff_text:
            with contextlib.suppress(OSError):
                sidecar_stem.with_name(sidecar_stem.name + ".diff.patch").write_text(
                    diff_text, encoding="utf-8"
                )

    if dry_run:
        console.print("\n[bold]Rewritten skill body:[/bold]\n")
        console.print(new_body)
        if exit_nonzero:
            raise typer.Exit(1)
        return

    if audit.get("candidate_status") == PROMOTED_ELIGIBLE:
        raw_fm, _ = parse_skill_md(skill.skill_md_path)
        fm = SkillFrontmatter.model_validate(raw_fm)
        write_skill_md(skill.skill_md_path, fm, new_body)
        console.print(f"\n[green]Skill {skill_name!r} rewritten.[/green]")
        console.print(f"Review with: [cyan]evo diff {skill_name}[/cyan]")
    else:
        console.print(
            f"\n[yellow]No new candidate committed "
            f"(status={audit.get('candidate_status')}): {audit.get('reason')}[/yellow]"
        )
    console.print(f"  audit: {audit_path}")
    if experiment:
        console.print(f"  (in experiment {experiment})")

    if exit_nonzero:
        raise typer.Exit(1)


# ------------------------------------------------------------------
# experiments
# ------------------------------------------------------------------


@app.command(name="experiments")
def list_experiments_cmd() -> None:
    """List all experiments."""
    from evolution.core.experiments import list_experiments

    exps = list_experiments(Path.cwd())
    if not exps:
        console.print("[yellow]No experiments found.[/yellow]")
        return

    table = Table(title=f"Experiments ({len(exps)})")
    table.add_column("NAME", style="cyan")
    table.add_column("SKILLS")
    table.add_column("FORKED FROM")
    table.add_column("DESCRIPTION")
    table.add_column("CREATED")

    for e in exps:
        forked = e.get("forked_from", {})
        forked_str = ", ".join(f"{k}@{v}" for k, v in forked.items())
        table.add_row(
            e["name"],
            str(e.get("skill_count", 0)),
            forked_str,
            e.get("description", ""),
            e.get("created_at", "")[:19],
        )
    console.print(table)


@app.command(name="create-experiment")
def create_experiment_cmd(
    name: str = typer.Argument(help="Experiment name"),
    skills: str = typer.Option(
        ...,
        "--skills",
        help="Skills to fork: skill1@v1,skill2@v2 (version required)",
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Experiment description",
    ),
) -> None:
    """Create an experiment by forking skills at specific versions."""
    from evolution.core.experiments import create_experiment

    # Parse skill@version pairs
    skill_specs = []
    for item in skills.split(","):
        item = item.strip()
        if "@" in item:
            sname, ver = item.rsplit("@", 1)
            skill_specs.append({"name": sname, "version": ver})
        else:
            console.print(f"[red]Missing version for {item!r}. Use skill@version format.[/red]")
            raise typer.Exit(1)

    try:
        exp_dir = create_experiment(Path.cwd(), name, skill_specs, description)
        console.print(f"[green]Created experiment:[/green] {name}")
        for spec in skill_specs:
            console.print(f"  {spec['name']} @ {spec['version']}")
        console.print(f"  Path: {exp_dir}")
    except (KeyError, FileExistsError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command(name="delete-experiment")
def delete_experiment_cmd(
    name: str = typer.Argument(help="Experiment name"),
) -> None:
    """Delete an experiment."""
    from evolution.core.experiments import delete_experiment

    try:
        delete_experiment(Path.cwd(), name)
        console.print(f"[green]Deleted experiment:[/green] {name}")
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


# ------------------------------------------------------------------
# tasks
# ------------------------------------------------------------------


@app.command(name="tasks")
def list_tasks(
    tasks_dir: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Tasks directory (default: ./tasks)",
    ),
) -> None:
    """List available evaluation tasks."""
    from evolution.eval.task import discover_tasks

    tasks_path = tasks_dir or Path.cwd() / "tasks"
    tasks = discover_tasks(tasks_path)

    if not tasks:
        console.print("No tasks found.")
        return

    table = Table(title=f"Evaluation Tasks ({len(tasks)})")
    table.add_column("NAME", style="cyan")
    table.add_column("DIFFICULTY")
    table.add_column("SKILLS", style="green")
    table.add_column("SOURCE")
    table.add_column("CATEGORY")

    for t in tasks:
        diff_style = {
            "easy": "green",
            "medium": "yellow",
            "hard": "red",
        }.get(t.difficulty, "dim")
        table.add_row(
            escape(t.name),
            f"[{diff_style}]{escape(t.difficulty)}[/]",
            escape(", ".join(t.skills_required)),
            escape(t.source),
            escape(t.category),
        )

    console.print(table)


@app.command(name="setup-task")
def setup_task_cmd(
    name: str = typer.Argument(help="Task name"),
    workspace: Path = typer.Option(
        ...,
        "--workspace",
        "-w",
        help="Workspace directory to prepare",
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Tasks directory (default: ./tasks)",
    ),
) -> None:
    """Set up a workspace with task input files."""
    from evolution.eval.task import discover_tasks
    from evolution.eval.workspace import setup_workspace

    tasks_path = tasks_dir or Path.cwd() / "tasks"
    tasks = discover_tasks(tasks_path)
    by_name = {t.name: t for t in tasks}

    if name not in by_name:
        console.print(f"[red]Task {name!r} not found.[/red]")
        raise typer.Exit(1)

    task = by_name[name]
    ws = setup_workspace(task, workspace)

    console.print(f"[green]Workspace ready:[/green] {ws}")
    console.print(f"  Task: {task.name}")
    console.print(f"  Skills: {', '.join(task.skills_required)}")
    console.print("\n[bold]Instruction:[/bold]")
    console.print(Panel(task.instruction, title=task.name))
    console.print("\nWhen done, evaluate with:")
    console.print(f"  [cyan]evo run-task {task.name} -w {ws}[/cyan]")


# ------------------------------------------------------------------
# split-check — verify no leakage of test tasks into evolution/selection
# ------------------------------------------------------------------


@app.command(name="split-check")
def split_check_cmd(
    manifest: Path = typer.Argument(
        help="Path to split manifest JSON (e.g. evolution/splits/skillsbench/manifests/domain-stratified-v1.json)",
    ),
    evolution_tasks: Optional[str] = typer.Option(
        None,
        "--evolution-tasks",
        help="Comma-separated task ids that will be fed to the skill-evolution loop",
    ),
    dev_tasks: Optional[str] = typer.Option(
        None,
        "--dev-tasks",
        help="Comma-separated task ids used for selection/stopping (train+val allowed)",
    ),
    reporting_tasks: Optional[str] = typer.Option(
        None,
        "--reporting-tasks",
        help="Comma-separated task ids used for final reporting (test only)",
    ),
) -> None:
    """Preflight: fail if any task id falls outside its allowed scope.

    Run this once at the start of every experiment to guarantee that the
    evolution loop, selection loop, and reporting pipeline each see only the
    slice the manifest permits.

    Examples:

      evo split-check \
        evolution/splits/skillsbench/manifests/domain-stratified-v1.json \
        --evolution-tasks xlsx-recover-data,offer-letter-generator \
        --dev-tasks xlsx-recover-data,offer-letter-generator,pdf-excel-diff
    """
    try:
        m = SplitManifest.load(manifest)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(f"Manifest [cyan]{m.name}[/cyan] ({m.sha256[:12]})")
    counts = m.counts
    console.print(
        f"  train={counts.get('train', '?')}  "
        f"val={counts.get('val', '?')}  "
        f"test={counts.get('test', '?')}  "
        f"(eligible={counts.get('eligible', '?')}, all_repo={counts.get('all_repo', '?')})"
    )

    def _split_ids(csv: Optional[str]) -> list[str]:
        if not csv:
            return []
        return [x.strip() for x in csv.split(",") if x.strip()]

    checks = [
        ("skill_evolution", _split_ids(evolution_tasks), "evolution loop"),
        ("dev", _split_ids(dev_tasks), "selection/stopping"),
        ("reporting", _split_ids(reporting_tasks), "reporting"),
    ]

    had_error = False
    for scope, ids_, context in checks:
        if not ids_:
            continue
        try:
            m.assert_no_leakage(ids_, scope=scope, context=context)
            console.print(f"  [green]OK[/green] scope={scope} ({len(ids_)} tasks) -- no leakage")
        except LeakageError as exc:
            had_error = True
            console.print(f"  [red]LEAK[/red] {exc}")

    if had_error:
        raise typer.Exit(1)

    if not any(ids_ for _, ids_, _ in checks):
        console.print(
            "[yellow]No task lists supplied; manifest loaded but nothing to check.[/yellow]"
        )


@app.command(name="split-list")
def split_list_cmd(
    manifest: Path = typer.Argument(help="Path to split manifest JSON"),
    scope: str = typer.Option(
        "all",
        "--scope",
        "-s",
        help="One of: skill_evolution, dev, reporting, all",
    ),
) -> None:
    """Print the task ids in a given scope of a manifest.

    Examples:

      evo split-list evolution/splits/skillsbench/manifests/domain-stratified-v1.json -s skill_evolution
      evo split-list evolution/splits/skillsbench/manifests/domain-stratified-v1.json -s reporting
    """
    try:
        m = SplitManifest.load(manifest)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    try:
        ids = m.tasks(scope=scope)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    for tid in ids:
        typer.echo(tid)


@app.command(name="run-task")
def run_task_cmd(
    name: str = typer.Argument(help="Task name"),
    workspace: Path = typer.Option(
        ...,
        "--workspace",
        "-w",
        help="Workspace directory with agent output",
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Tasks directory (default: ./tasks)",
    ),
) -> None:
    """Run a task's evaluation tests against a workspace."""
    from evolution.eval.runner import run_task
    from evolution.eval.task import discover_tasks

    tasks_path = tasks_dir or Path.cwd() / "tasks"
    tasks = discover_tasks(tasks_path)
    by_name = {t.name: t for t in tasks}

    if name not in by_name:
        console.print(f"[red]Task {name!r} not found.[/red]")
        available = ", ".join(sorted(by_name.keys()))
        console.print(f"Available: {available}")
        raise typer.Exit(1)

    task = by_name[name]
    console.print(f"Running [cyan]{task.name}[/cyan] against {workspace}...")

    result = run_task(task, workspace)

    # Display result
    if result.success:
        console.print(f"\n[green]PASS[/green] {result.passed}/{result.total_tests} tests passed")
    else:
        console.print(
            f"\n[red]FAIL[/red] {result.passed}/{result.total_tests} passed, {result.failed} failed"
        )
        for err in result.errors:
            console.print(f"  [red]x[/red] {err}")
        raise typer.Exit(1)


@app.command(name="run-tasks")
def run_tasks_cmd(
    workspace: Path = typer.Option(
        ...,
        "--workspace",
        "-w",
        help="Workspace directory with agent output",
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Tasks directory (default: ./tasks)",
    ),
    skill: Optional[str] = typer.Option(
        None,
        "--skill",
        "-s",
        help="Only run tasks requiring this skill",
    ),
) -> None:
    """Run all task evaluations against a workspace."""
    from evolution.eval.runner import run_task
    from evolution.eval.task import discover_tasks

    tasks_path = tasks_dir or Path.cwd() / "tasks"
    tasks = discover_tasks(tasks_path)

    if skill:
        tasks = [t for t in tasks if skill in t.skills_required]

    if not tasks:
        console.print("No matching tasks found.")
        return

    console.print(f"Running {len(tasks)} task(s) against {workspace}...\n")

    results = []
    for task in tasks:
        result = run_task(task, workspace)
        results.append(result)

        status = "[green]PASS[/green]" if result.success else "[red]FAIL[/red]"
        console.print(f"  {status} {task.name}: {result.passed}/{result.total_tests}")

    # Summary
    total_pass = sum(1 for r in results if r.success)
    console.print(f"\n{total_pass}/{len(results)} tasks passed")

    if total_pass < len(results):
        raise typer.Exit(1)


# ------------------------------------------------------------------
# traces
# ------------------------------------------------------------------


@app.command(name="traces")
def traces_cmd(
    name: str = typer.Argument(help="Skill name"),
    version: Optional[str] = typer.Option(
        None,
        "--version",
        "-v",
        help="Filter by skill version",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Filter by model name",
    ),
    task: Optional[str] = typer.Option(
        None,
        "--task",
        "-t",
        help="Filter by task name",
    ),
) -> None:
    """List traces for a skill with optional filters.

    Examples:

      evo traces docx
      evo traces docx --version v3
      evo traces docx --model claude-sonnet-4
      evo traces docx --task offer-letter
    """
    from evolution.lineage.traces import TraceStore

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    ts = TraceStore(skill)
    traces = ts.list(skill_version=version, model=model, task=task)

    if not traces:
        console.print("No traces found.")
        return

    table = Table(title=f"{name} -- traces ({len(traces)})")
    table.add_column("#", style="dim", justify="right")
    table.add_column("VERSION", style="cyan")
    table.add_column("MODEL")
    table.add_column("TASK")
    table.add_column("RESULT", justify="right")
    table.add_column("IN/OUT", justify="right")
    table.add_column("TIME", justify="right")

    for i, t in enumerate(traces, 1):
        passed = t.get("passed", 0)
        total = t.get("total", 0)
        success = t.get("success", False)
        result_str = f"{passed}/{total}"
        result_style = "green" if success else "red"
        tokens_in = t.get("tokens_in", 0)
        tokens_out = t.get("tokens_out", 0)
        time_ms = t.get("time_ms", 0)

        table.add_row(
            str(i),
            t.get("skill_version", ""),
            t.get("model", ""),
            t.get("task", ""),
            f"[{result_style}]{result_str}[/]",
            f"{tokens_in}+{tokens_out}",
            f"{time_ms}ms",
        )

    console.print(table)


# ------------------------------------------------------------------
# show-trace
# ------------------------------------------------------------------


@app.command(name="show-trace")
def show_trace_cmd(
    name: str = typer.Argument(help="Skill name"),
    trace_id: Optional[str] = typer.Argument(
        None,
        help="Trace ID, number (#1, #2, ...), or omit for --last",
    ),
    section: Optional[str] = typer.Option(
        None,
        "--section",
        "-s",
        help="Show only one section: prompt, response, code, result",
    ),
    last: bool = typer.Option(
        False,
        "--last",
        "-l",
        help="Show the most recent trace",
    ),
) -> None:
    """Show the contents of a trace.

    Examples:

      evo show-trace docx --last             # most recent trace
      evo show-trace docx --last -s code     # just the code from latest
      evo show-trace docx 1                  # first trace (by index)
      evo show-trace docx -1                 # last trace (negative index)
      evo show-trace docx <full_trace_id>    # by exact ID
    """
    from evolution.lineage.traces import TraceStore

    store = _get_store()
    try:
        skill = store.get(name)
    except KeyError:
        console.print(f"[red]Skill {name!r} not found.[/red]")
        raise typer.Exit(1)

    ts = TraceStore(skill)
    index = ts.list()

    if not index:
        console.print("No traces found.")
        raise typer.Exit(1)

    # Resolve trace_id
    if last or trace_id is None:
        resolved_id = index[-1]["id"]
    elif trace_id.lstrip("-").isdigit():
        # Numeric index: 1-based positive, or negative from end
        idx = int(trace_id)
        if idx > 0:
            idx -= 1  # convert 1-based to 0-based
        try:
            resolved_id = index[idx]["id"]
        except IndexError:
            console.print(f"[red]Index {trace_id} out of range (1-{len(index)}).[/red]")
            raise typer.Exit(1)
    else:
        resolved_id = trace_id

    trace_dir = ts.traces_dir / resolved_id
    display_id = trace_id or "latest"

    if not trace_dir.is_dir():
        console.print(f"[red]Trace {resolved_id!r} not found.[/red]")
        raise typer.Exit(1)

    # Load result metadata
    result_file = trace_dir / "result.json"
    if result_file.exists():
        result = json.loads(result_file.read_text())
    else:
        result = {}

    sections = {
        "prompt": ("prompt.md", "Prompt", "markdown"),
        "response": ("response.md", "LLM Response", "markdown"),
        "code": ("solve.py", "Extracted Code", "python"),
        "result": ("result.json", "Result", "json"),
    }

    if section:
        if section not in sections:
            console.print(
                f"[red]Unknown section: {section}. Use: prompt, response, code, result[/red]"
            )
            raise typer.Exit(1)
        to_show = {section: sections[section]}
    else:
        to_show = sections

    # Header
    success = result.get("success", False)
    passed = result.get("passed", 0)
    total = result.get("total", 0)
    status = (
        f"[green]PASS[/green] {passed}/{total}" if success else f"[red]FAIL[/red] {passed}/{total}"
    )

    console.print(
        Panel(
            f"Skill: [cyan]{name}[/cyan] {result.get('skill_version', '')}\n"
            f"Model: {result.get('model', '')}\n"
            f"Task:  {result.get('task', '')}\n"
            f"Result: {status}\n"
            f"Tokens: {result.get('tokens_in', 0)} in + {result.get('tokens_out', 0)} out\n"
            f"Time:  {result.get('time_ms', 0)}ms",
            title=f"Trace: {display_id}",
        )
    )

    for key, (filename, title, lang) in to_show.items():
        filepath = trace_dir / filename
        if not filepath.exists():
            continue

        content = filepath.read_text(encoding="utf-8")

        if key == "result":
            console.print(
                Panel(
                    Syntax(content, lang, theme="monokai"),
                    title=title,
                )
            )
        elif key == "code":
            console.print(
                Panel(
                    Syntax(content, lang, theme="monokai", line_numbers=True),
                    title=title,
                )
            )
        else:
            # For prompt and response, truncate if very long
            lines = content.split("\n")
            if len(lines) > 60 and section is None:
                preview = "\n".join(
                    lines[:30] + [f"\n... ({len(lines) - 60} lines omitted) ...\n"] + lines[-30:]
                )
                console.print(Panel(preview, title=f"{title} ({len(lines)} lines)"))
            else:
                console.print(Panel(content, title=title))


# ------------------------------------------------------------------
# solve
# ------------------------------------------------------------------


@app.command(name="solve")
def solve_cmd(
    task_name: str = typer.Argument(help="Task name"),
    model: str = typer.Option(
        ...,
        "--model",
        "-m",
        help="Model name (e.g. openai/llama3, claude, gpt-4o)",
    ),
    skill_name: Optional[str] = typer.Option(
        None,
        "--skill",
        "-s",
        help="Skill to inject (default: auto from task requirements)",
    ),
    workspace: Path = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Workspace directory (default: /tmp/evo-<task>)",
    ),
    api_base: Optional[str] = typer.Option(
        None,
        "--api-base",
        help="API base URL (e.g. http://localhost:8000/v1 for vLLM)",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="API key (default: from env)",
    ),
    max_tokens: int = typer.Option(
        16384,
        "--max-tokens",
        help="Max tokens for LLM response (increase for thinking models)",
    ),
    timeout: float = typer.Option(
        300.0,
        "--timeout",
        help="Script execution timeout in seconds",
    ),
    no_skill: bool = typer.Option(
        False,
        "--no-skill",
        help="Run without skill (baseline)",
    ),
    snapshot: Optional[str] = typer.Option(
        None,
        "--snapshot",
        help="Use skill versions from a container snapshot (e.g. s1)",
    ),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        "-e",
        help="Use skills from an experiment instead of main store",
    ),
    tasks_dir: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Tasks directory (default: ./tasks)",
    ),
    manifest: Optional[Path] = typer.Option(
        None,
        "--manifest",
        help=(
            "Split manifest JSON. If set, `--manifest-scope` must also be "
            "given and the task must fall inside that scope."
        ),
    ),
    manifest_scope: Optional[str] = typer.Option(
        None,
        "--manifest-scope",
        help=(
            "One of skill_evolution (train), dev (train+val), reporting "
            "(test), all. Required when --manifest is set."
        ),
    ),
) -> None:
    """Solve a task with an LLM and evaluate the result.

    Examples:

      # With a vLLM server:
      evo solve xlsx-recover-data -m openai/llama3 --api-base http://localhost:8000/v1

      # With Claude:
      evo solve xlsx-recover-data -m claude

      # Baseline (no skill):
      evo solve xlsx-recover-data -m claude --no-skill

      # Use a specific container snapshot:
      evo solve sb-de-duckdb-assembly -m claude --snapshot s1

      # Use skills from an experiment:
      evo solve sb-de-duckdb-assembly -m claude --experiment ablation-1

      # Scope-aware reporting run (test only):
      evo solve sb-de-duckdb-assembly -m claude \\
         --manifest evolution/splits/skillsbench/manifests/domain-stratified-v1.json \\
         --manifest-scope reporting
    """
    from evolution.eval.solver import solve_task
    from evolution.eval.task import discover_tasks
    from evolution.eval.workspace import setup_workspace
    from evolution.llm.backend import get_backend

    tasks_path = tasks_dir or Path.cwd() / "tasks"
    tasks = discover_tasks(tasks_path)
    by_name = {t.name: t for t in tasks}

    if task_name not in by_name:
        console.print(f"[red]Task {task_name!r} not found.[/red]")
        raise typer.Exit(1)

    task = by_name[task_name]

    # Set up workspace
    ws = workspace or Path(f"/tmp/evo-{task_name}")
    setup_workspace(task, ws)

    # Build backend
    backend_kwargs = {}
    if api_base:
        backend_kwargs["api_base"] = api_base
    if api_key:
        backend_kwargs["api_key"] = api_key
    elif api_base:
        # vLLM and other local servers don't need auth but litellm requires a key
        backend_kwargs["api_key"] = "not-needed"
    backend_kwargs["max_tokens"] = max_tokens
    backend = get_backend(model, **backend_kwargs)

    # Resolve skill(s)
    resolved_skills = []
    if not no_skill:
        if experiment:
            from evolution.core.experiments import get_experiment_store

            store = get_experiment_store(Path.cwd(), experiment)
            exp_info = f" (experiment {experiment})"
        else:
            store = _get_store()
            exp_info = ""
        name = skill_name or (task.skills_required[0] if task.skills_required else None)
        if name:
            resolved_skills = _resolve_skills(name, store, snapshot=snapshot)
            if resolved_skills:
                names = ", ".join(s.name for s in resolved_skills)
                snap_info = f" (snapshot {snapshot})" if snapshot else ""
                console.print(f"Using skill(s): [green]{names}[/green]{snap_info}{exp_info}")
            else:
                console.print(
                    f"[yellow]Skill/container {name!r} not found, running without skill[/yellow]"
                )
    else:
        console.print("Running [yellow]without skill[/yellow] (baseline)")

    split_manifest = None
    if manifest is not None:
        if not manifest_scope:
            console.print(
                "[red]--manifest-scope is required when --manifest is set "
                "(one of skill_evolution, dev, reporting, all).[/red]"
            )
            raise typer.Exit(2)
        split_manifest = SplitManifest.load(manifest)
        console.print(
            f"Manifest: [cyan]{split_manifest.name}[/cyan] "
            f"({split_manifest.sha256[:12]}) scope={manifest_scope}"
        )

    console.print(f"Model: [cyan]{model}[/cyan]")
    console.print(f"Task: [cyan]{task.name}[/cyan]")
    console.print(f"Workspace: {ws}\n")

    # Solve
    result = solve_task(
        task,
        backend,
        ws,
        skills=resolved_skills or None,
        timeout=timeout,
        model_name=model,
        manifest=split_manifest,
        manifest_scope=manifest_scope,
    )

    # Report
    if result.llm_response:
        resp = result.llm_response
        total = resp.tokens_in + resp.tokens_out
        console.print(f"LLM: {resp.tokens_in} prompt + {resp.tokens_out} gen = {total} tokens")
        console.print(
            f"Timing: llm {resp.time_ms}ms | exec {result.exec_ms}ms | "
            f"eval {result.eval_ms}ms | total {result.completion_ms}ms"
        )

    if result.script_exit_code == 0:
        console.print("[green]Script executed successfully[/green]")
    elif result.script_path:
        console.print(f"[red]Script failed (exit {result.script_exit_code})[/red]")
        if result.script_output:
            # Show last 10 lines of output
            lines = result.script_output.strip().split("\n")
            for line in lines[-10:]:
                console.print(f"  {line}")

    if result.eval_result:
        er = result.eval_result
        if er.success:
            console.print(f"\n[green]PASS[/green] {er.passed}/{er.total_tests} tests")
        else:
            console.print(
                f"\n[red]FAIL[/red] {er.passed}/{er.total_tests} passed, {er.failed} failed"
            )
            for err in er.errors[:5]:
                console.print(f"  [red]x[/red] {err}")

    for err in result.errors:
        console.print(f"[red]Error:[/red] {err}")

    if not result.success:
        raise typer.Exit(1)


# ------------------------------------------------------------------
# Lifecycle command groups
# ------------------------------------------------------------------

# The flat commands above are kept for backwards compatibility.  The grouped
# commands below are the documented framework-first surface for new users.
task_app.command(name="list")(list_tasks)
task_app.command(name="setup")(setup_task_cmd)
task_app.command(name="eval")(run_task_cmd)
task_app.command(name="eval-all")(run_tasks_cmd)
task_app.command(name="import-sb-bench")(import_sb_bench)

skill_app.command(name="create")(create)
skill_app.command(name="list")(list_skills)
skill_app.command(name="show")(show)
skill_app.command(name="validate")(validate)
skill_app.command(name="search")(search)
skill_app.command(name="status")(status)
skill_app.command(name="lineage")(lineage)
skill_app.command(name="diff")(diff_cmd)
skill_app.command(name="history")(history)
skill_app.command(name="commit")(commit)
skill_app.command(name="promote")(promote)
skill_app.command(name="discard")(discard)
skill_app.command(name="repair")(repair_cmd)
skill_app.command(name="delete-version")(delete_version_cmd)
skill_app.command(name="import")(import_cmd)
skill_app.command(name="import-skillsbench")(import_skillsbench)

run_app.command(name="solve")(solve_cmd)
run_app.add_typer(_bench_app, name="bench")

trace_app.command(name="list")(traces_cmd)
trace_app.command(name="show")(show_trace_cmd)

evolve_app.command(name="reflect")(reflect_cmd)
evolve_app.command(name="rewrite")(rewrite_cmd)

paper_app.command(name="split-check")(split_check_cmd)
paper_app.command(name="split-list")(split_list_cmd)


if __name__ == "__main__":  # `python -m evolution.cli.main`
    app()
