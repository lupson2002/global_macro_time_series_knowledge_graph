"""Shared SMTP transport for generated reports."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class EmailAttachment:
    path: Path
    filename: str
    subtype: str = "html"


def send_multipart_email(
    *,
    subject: str,
    body_text: str,
    body_html: str,
    user: str,
    password: str,
    recipients: Sequence[str],
    host: str,
    port: int,
    attachments: Iterable[EmailAttachment] = (),
    mixed_root: bool = False,
    timeout: float | None = None,
    strip_password_spaces: bool = False,
) -> int:
    """Build and send a plain+HTML email, returning the attached-file count.

    Transport exceptions deliberately propagate so each pipeline can retain its
    established warning or fallback policy.
    """
    root = MIMEMultipart("mixed" if mixed_root else "alternative")
    root["Subject"] = subject
    root["From"] = user
    root["To"] = ", ".join(recipients)

    alternative = MIMEMultipart("alternative") if mixed_root else root
    alternative.attach(MIMEText(body_text, "plain", "utf-8"))
    alternative.attach(MIMEText(body_html, "html", "utf-8"))
    if mixed_root:
        root.attach(alternative)

    attached = 0
    for item in attachments:
        path = Path(item.path)
        if not path.is_file():
            continue
        attachment = MIMEApplication(path.read_bytes(), _subtype=item.subtype)
        attachment.add_header("Content-Disposition", "attachment", filename=item.filename)
        root.attach(attachment)
        attached += 1

    smtp_kwargs = {"timeout": timeout} if timeout is not None else {}
    login_password = password.replace(" ", "") if strip_password_spaces else password
    with smtplib.SMTP_SSL(host, port, **smtp_kwargs) as server:
        server.login(user, login_password)
        server.sendmail(user, list(recipients), root.as_string())
    return attached
