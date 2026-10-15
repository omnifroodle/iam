from typer.testing import CliRunner

from iam.cli import app

runner = CliRunner()


def run(*args):
    r = runner.invoke(app, list(args))
    assert r.exit_code == 0, r.output
    return r.output


def test_cli_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("IAM_HOME", str(tmp_path))
    monkeypatch.setenv("IAM_EMBEDDER", "hash-256")
    assert "vault ready" in run("init")
    mid = run(
        "add",
        "--vault",
        "coding",
        "--cat",
        "python",
        "--reason",
        "seed",
        "Prefers OpenAI-compatible clients.",
    ).strip()
    assert "OpenAI-compatible" in run("search", "OpenAI-compatible clients")
    assert mid in run("list")
    assert '"provenance"' in run("show", mid)
    run("grant", "claude-code", "coding", "--mode", "read-write", "--reason", "daily driver")
    assert "claude-code" in run("grants")
    out = run("audit")
    assert "remember" in out and "grant" in out and "daily driver" in out
    assert mid in run("audit", "--memory", mid)
    run("forget", mid, "--reason", "wrong")
    assert "nothing matched" in runner.invoke(app, ["search", "OpenAI-compatible clients"]).output
    assert mid in run("list", "--status", "retracted")
    exported = run("export", "--include-retracted")
    assert mid in exported
