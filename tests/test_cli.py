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


def test_pull_is_this_project_plus_global(tmp_path, monkeypatch):
    monkeypatch.setenv("IAM_HOME", str(tmp_path))
    monkeypatch.setenv("IAM_EMBEDDER", "hash-256")
    run("init")
    run("grant", "claude-code", "coding", "--mode", "read", "--reason", "t")
    run("add", "--vault", "coding", "--reason", "s", "Global fact.")
    run("add", "--vault", "coding", "--project", "iam", "--reason", "s", "Iam fact.")
    run("add", "--vault", "coding", "--project", "other", "--reason", "s", "Other fact.")
    out = run("pull", "--client", "claude-code", "--project", str(tmp_path / "iam"))
    assert "project: iam" in out and "Global fact." in out and "Iam fact. (this project)" in out
    assert "Other fact." not in out
    assert "@iam" in run("list") and "Other fact." not in run("list", "--project", "iam")
