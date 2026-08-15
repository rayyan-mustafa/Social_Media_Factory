"""Met preflight must send a User-Agent (Incapsula 403s bare clients)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.services.farm_preflight import probe_met_ready


def test_probe_met_ready_sends_user_agent():
    search = MagicMock()
    search.status_code = 200
    search.json.return_value = {"total": 3, "objectIDs": [1, 2, 3]}
    search.text = ""

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = search

    with patch("src.services.farm_preflight.httpx.Client", return_value=client) as ctor:
        out = probe_met_ready()

    assert out.get("ok") is True
    assert out.get("total") == 3
    kwargs = ctor.call_args.kwargs.get("headers") or {}
    assert "User-Agent" in kwargs
    assert kwargs["User-Agent"]


def test_probe_met_ready_reports_incapsula_403():
    bad = MagicMock()
    bad.status_code = 403
    bad.text = '<iframe id="main-iframe" src="/_Incapsula_Resource'

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = bad

    with patch("src.services.farm_preflight.httpx.Client", return_value=client):
        out = probe_met_ready()

    assert out.get("ok") is False
    assert "403" in str(out.get("error") or "")
