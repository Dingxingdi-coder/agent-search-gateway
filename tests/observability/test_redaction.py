import io
import logging
from pathlib import Path

from agent_search_gateway.observability import SecretValue, configure_debug_logging, log_event


def test_academic_credentials_and_contact_are_redacted_from_fields_and_traceback(
    tmp_path: Path,
) -> None:
    credential = SecretValue("CREDENTIAL_PAYLOAD_SENTINEL")
    contact = SecretValue("CONTACT_PAYLOAD_SENTINEL")
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    session.add_secrets((credential,), (contact,))
    try:
        try:
            raise RuntimeError(f"{credential.reveal()} {contact.reveal()}")
        except RuntimeError:
            log_event(
                logging.getLogger("agent_search_gateway.observability.redaction_test"),
                logging.DEBUG,
                "academic_redaction_probe",
                detail=f"{credential.reveal()} {contact.reveal()}",
                exc_info=True,
            )
    finally:
        session.close()

    text = log_file.read_text(encoding="utf-8")
    assert credential.reveal() not in text
    assert contact.reveal() not in text
    assert text.count("<redacted>") >= 4
    assert "event=academic_redaction_probe" in text
    assert "traceback=" in text
