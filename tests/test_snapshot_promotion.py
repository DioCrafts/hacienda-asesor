import json
from pathlib import Path

from click.testing import CliRunner

from hacienda_gpt.cli.promote_snapshot import cli


def test_promote_snapshot_updates_current_symlink(tmp_path: Path) -> None:
    cfg = {
        "environments": {
            "dev": {
                "data_root": str(tmp_path / "dev"),
                "quality_gates": {"min_intent_accuracy": 0.7, "max_unsafe_recommendation_rate": 0.1},
            }
        }
    }
    config_path = tmp_path / "cfg.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")

    snapshot_dir = tmp_path / "dev" / "snapshots" / "2026-05-19"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "eval_pipeline_results.json").write_text(
        json.dumps({"intent_accuracy": 0.9, "unsafe_recommendation_rate": 0.0}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["--env", "dev", "--snapshot", "2026-05-19", "--config", str(config_path)])
    assert result.exit_code == 0

    current = tmp_path / "dev" / "current"
    assert current.is_symlink()
    assert current.resolve() == snapshot_dir.resolve()
