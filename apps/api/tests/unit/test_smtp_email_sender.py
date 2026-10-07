"""`SmtpEmailSender` against a fake `smtplib.SMTP` (no network). The real server is Mailpit,
covered by the integration test and by the verifier."""

import smtplib
from email.message import EmailMessage as MimeMessage
from types import TracebackType

import pytest

from fragancia_api.shared.application.email import EmailMessage
from fragancia_api.shared.infrastructure.smtp_email_sender import SmtpEmailSender

MESSAGE = EmailMessage(to="staff@example.test", subject="Hola ñandú", body="Contraseña: ¡ya!\n")


class FakeSmtp:
    """Records the calls a connection receives; `instances` collects every connection."""

    instances: list[FakeSmtp] = []
    fail_on_send = False

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[MimeMessage] = []
        self.closed = False
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}:{password}")

    def send_message(self, message: MimeMessage) -> None:
        if FakeSmtp.fail_on_send:
            raise ConnectionRefusedError("smtp refused")
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSmtp.instances = []
    FakeSmtp.fail_on_send = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)


def _sender(
    *, username: str | None = None, password: str | None = None, starttls: bool = False
) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="smtp.test",
        port=1026,
        username=username,
        password=password,
        starttls=starttls,
        sender="La Fragancia Ideal <no-reply@lafraganciaideal.test>",
    )


async def test_sends_one_plain_text_message_with_the_headers_and_utf8_body() -> None:
    await _sender().send(MESSAGE)

    [connection] = FakeSmtp.instances
    [sent] = connection.sent
    assert (connection.host, connection.port, connection.timeout) == ("smtp.test", 1026, 10)
    assert sent["From"] == "La Fragancia Ideal <no-reply@lafraganciaideal.test>"
    assert sent["To"] == "staff@example.test"
    assert sent["Subject"] == "Hola ñandú"
    assert sent.get_content() == "Contraseña: ¡ya!\n"
    assert sent.get_content_type() == "text/plain"
    assert connection.closed


async def test_without_starttls_or_credentials_it_only_sends() -> None:
    await _sender().send(MESSAGE)

    assert FakeSmtp.instances[0].calls == ["send"]


async def test_starttls_comes_first_then_login_then_send() -> None:
    await _sender(username="user", password="secret", starttls=True).send(MESSAGE)

    assert FakeSmtp.instances[0].calls == ["starttls", "login:user:secret", "send"]


@pytest.mark.parametrize(
    ("username", "password"), [("user", None), (None, "secret"), (None, None), ("", "")]
)
async def test_it_logs_in_only_when_both_credentials_are_set(
    username: str | None, password: str | None
) -> None:
    await _sender(username=username, password=password).send(MESSAGE)

    assert FakeSmtp.instances[0].calls == ["send"]


async def test_a_failure_is_not_swallowed_so_the_relay_retries() -> None:
    FakeSmtp.fail_on_send = True

    with pytest.raises(ConnectionRefusedError, match="smtp refused"):
        await _sender().send(MESSAGE)

    assert FakeSmtp.instances[0].closed
