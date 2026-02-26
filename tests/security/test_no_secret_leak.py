from tce_api.redaction import redact_payload


def test_no_private_key_in_redacted_payload():
    payload = {"secret": "-----BEGIN PRIVATE KEY-----abc"}
    redacted, _ = redact_payload(payload)
    assert "PRIVATE KEY" not in redacted["secret"]
