from __future__ import annotations

from star.api.routers.analysis_audit import _redact_credentials


def test_private_generation_evidence_redacts_only_credentials() -> None:
    value = _redact_credentials(
        {
            "asset_id": "private-but-expected-in-authenticated-evidence",
            "description": "private accepted evidence",
            "authorization": "Bearer secret",
            "nested": {
                "api-key": "secret",
                "access_token": "secret",
                "input_tokens": 123,
                "output_tokens": 45,
            },
        }
    )

    assert value == {
        "asset_id": "private-but-expected-in-authenticated-evidence",
        "description": "private accepted evidence",
        "authorization": "[REDACTED]",
        "nested": {
            "api-key": "[REDACTED]",
            "access_token": "[REDACTED]",
            "input_tokens": 123,
            "output_tokens": 45,
        },
    }
