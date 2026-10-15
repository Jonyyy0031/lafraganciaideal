"""SMTP adapter for the `EmailSender` port (Mailpit in development)."""

import asyncio
import smtplib
from email.message import EmailMessage as MimeMessage

from fragancia_api.shared.application.email import EmailMessage, EmailRejected


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        starttls: bool,
        sender: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._starttls = starttls
        self._sender = sender

    async def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        # Only a permanent recipient refusal becomes `EmailRejected`; any other failure reaches
        # the relay so the event is retried.
        await asyncio.to_thread(self._send_blocking, mime)

    def _send_blocking(self, mime: MimeMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            try:
                smtp.send_message(mime)
            except smtplib.SMTPRecipientsRefused as error:
                # Rejected only when every recipient got a permanent (5xx) refusal; a 4xx
                # (greylisting, mailbox busy) is temporary, so the event is retried.
                if all(code >= 500 for code, _ in error.recipients.values()):
                    raise EmailRejected(str(error)) from error
                raise
            # Sender, helo and DATA failures are not caught here: they propagate and the
            # event is retried.
