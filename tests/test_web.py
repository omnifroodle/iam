import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from iam.models import USER
from iam.web import serve


@pytest.fixture
def site(seeded):
    seeded.via = "web"
    server, url = serve(seeded, port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    page = urllib.request.urlopen(url).read().decode()
    token = re.search(r'name="iam-token" content="([^"]+)"', page).group(1)
    yield seeded, url, token
    server.shutdown()
    server.server_close()


def get(url, path, host=None):
    req = urllib.request.Request(url + path.lstrip("/"))
    if host:
        req.add_header("Host", host)
    return json.loads(urllib.request.urlopen(req).read())


def post(url, path, body, token=None, origin=None):
    req = urllib.request.Request(url + path.lstrip("/"), data=json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Iam-Token", token)
    if origin:
        req.add_header("Origin", origin)
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def propose(iam, text, project=None):
    return iam.remember(
        USER, text, "fact", "coding", reason="extract", status="proposed", project=project
    )


def audit_count(iam):
    return iam.store.conn.execute("SELECT count(*) FROM audit").fetchone()[0]


def test_state_shows_pending_and_history_without_logging(site):
    iam, url, _ = site
    p = propose(iam, "Uses uv.", project="iam")
    before = audit_count(iam)
    s = get(url, "/api/state")
    assert [x["id"] for x in s["pending"]] == [p.id] and s["pending"][0]["project"] == "iam"
    assert s["head"] == s["history"][0]["id"]
    top = s["history"][0]
    assert top["title"] == "propose: Uses uv." and top["project"] == "iam"
    assert top["reason"] == "extract" and not top["read"]
    assert get(url, "/api/head")["head"] == s["head"]
    assert audit_count(iam) == before  # polling the page is not a vault event


def test_accept_reject_go_through_core_and_are_audited_via_web(site):
    iam, url, token = site
    a, b = propose(iam, "Uses uv.", project="iam"), propose(iam, "Wrong guess.")
    status, out = post(
        url, "/api/accept", {"id": a.id, "everywhere": True, "text": "Uses uv everywhere."}, token
    )
    assert status == 200 and out["status"] == "active" and out["project"] is None
    assert post(url, "/api/reject", {"id": b.id, "reason": "not true"}, token)[0] == 200
    e = iam.audit(memory_id=b.id)[0]
    assert e.action == "forget" and e.via == "web" and e.reason == "not true"
    e = iam.audit(memory_id=a.id)[0]
    assert e.via == "web" and e.reason == "accepted in review"
    assert iam.store.get_memory(a.id).text == "Uses uv everywhere."
    assert get(url, "/api/state")["pending"] == []
    titles = [c["title"] for c in get(url, "/api/state")["history"][:2]]
    assert titles == ["forget: Wrong guess.", "accept: Uses uv everywhere."]


def test_writes_need_the_token_and_a_local_origin(site):
    iam, url, token = site
    p = propose(iam, "Uses uv.")
    before = audit_count(iam)
    assert post(url, "/api/accept", {"id": p.id})[0] == 403
    assert post(url, "/api/accept", {"id": p.id}, "guess")[0] == 403
    assert post(url, "/api/accept", {"id": p.id}, token, origin="https://evil.example")[0] == 403
    assert audit_count(iam) == before and iam.store.get_memory(p.id).status == "proposed"
    assert post(url, "/api/accept", {"id": "nope"}, token)[0] == 400


def test_wrong_host_is_refused(site):
    _, url, _ = site
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url, "/api/state", host="attacker.example:80")
    assert e.value.code == 403


def test_llm_config_round_trips_without_the_key(tmp_path, monkeypatch):
    from iam.config import Config

    monkeypatch.setenv("IAM_LLM_MODEL", "from-env")
    cfg = Config.load(tmp_path, env=False)
    cfg.llm_base_url, cfg.llm_model = "http://127.0.0.1:1234/v1", "qwen"
    cfg.save(tmp_path)
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["llm"] == {"base_url": "http://127.0.0.1:1234/v1", "model": "qwen"}
    assert "key" not in json.dumps(saved)
    assert Config.load(tmp_path).llm_model == "from-env"  # env still wins at run time
