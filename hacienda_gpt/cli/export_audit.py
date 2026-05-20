from __future__ import annotations

import json
from pathlib import Path

import click

from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore


@click.command()
@click.option("--db", default="./data/api_case_state.sqlite3")
@click.option("--case-id", required=True)
@click.option("--output", default="./audit_export.json")
def cli(db: str, case_id: str, output: str) -> None:
    store = SQLiteCaseStateStore(db)
    case = store.get_case(case_id)
    if case is None:
        raise SystemExit(f"case not found: {case_id}")
    payload = {
        "case_id": case_id,
        "events": store.list_audit_events(case_id),
    }
    Path(output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(f"audit exported: {output}")


if __name__ == "__main__":
    cli()
