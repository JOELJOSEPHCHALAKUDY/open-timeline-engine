from tce_api.redaction import redact_payload


def test_redacts_email_and_token():
    payload = {
        "message": "email me at a@b.com",
        "token": "api_key=ABCDEF123456",
        "project": "allowed",
    }
    redacted, applied = redact_payload(payload)
    assert "REDACTED" in redacted["message"]
    assert "REDACTED" in redacted["token"]
    assert redacted["project"] == "allowed"
    assert applied
