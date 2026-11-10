from typer.testing import CliRunner

from quality_platform.cli.app import app


def test_cli_has_project_workflow_and_benchmark_groups() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("project", "workflow", "benchmark"):
        assert command in result.stdout
