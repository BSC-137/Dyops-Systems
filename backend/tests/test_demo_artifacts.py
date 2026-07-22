from __future__ import annotations

import hashlib
import json
from pathlib import Path


def test_example_incident_export_is_explicitly_unsigned_and_hashes() -> None:
    path = Path(__file__).resolve().parents[2] / "examples" / "incident-export.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))

    assert artifact["schema_version"] == "2.0"
    assert artifact["software"]["name"] == "dyops"
    assert artifact["classification"]["unsigned"] is True
    assert "not a regulatory attestation" in artifact["classification"]["non_claim"]
    assert artifact["deterministic_evidence"]["source"] == (
        "server_deterministic_replay"
    )
    assert artifact["optional_llm_evidence"]["present"] is False
    assert "not a digital signature" in artifact["integrity_notice"]

    expected = artifact.pop("content_sha256")
    canonical = json.dumps(
        artifact,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == expected
