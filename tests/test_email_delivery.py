import tempfile
import unittest
from email import message_from_string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.insight_report import send_email_with_visuals
from src.email_delivery import EmailAttachment, send_multipart_email
from src.orchestrator import _send_cio_email_with_visuals
from src.report_generator import send_email_report


class FakeSmtp:
    instances = []

    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.login_args = None
        self.sendmail_args = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, sender, recipients, payload):
        self.sendmail_args = (sender, recipients, payload)


class EmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        FakeSmtp.instances.clear()

    @patch("src.email_delivery.smtplib.SMTP_SSL", FakeSmtp)
    def test_plain_html_contract_and_daily_transport_options(self):
        count = send_multipart_email(
            subject="Daily", body_text="plain", body_html="<b>html</b>",
            user="from@example.com", password="a b c", recipients=("to@example.com",),
            host="smtp.example.com", port=465, strip_password_spaces=True,
        )

        smtp = FakeSmtp.instances[0]
        message = message_from_string(smtp.sendmail_args[2])
        self.assertEqual(count, 0)
        self.assertEqual(smtp.kwargs, {})
        self.assertEqual(smtp.login_args, ("from@example.com", "abc"))
        self.assertEqual(smtp.sendmail_args[:2], ("from@example.com", ["to@example.com"]))
        self.assertEqual(message.get_content_subtype(), "alternative")
        self.assertEqual([part.get_content_subtype() for part in message.get_payload()], ["plain", "html"])

    @patch("src.email_delivery.smtplib.SMTP_SSL", FakeSmtp)
    def test_mixed_message_attaches_existing_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "chart.html"
            existing.write_text("<html>chart</html>", encoding="utf-8")
            count = send_multipart_email(
                subject="Insight", body_text="plain", body_html="<p>html</p>",
                user="from@example.com", password="secret", recipients=("a@example.com", "b@example.com"),
                host="smtp.example.com", port=465, timeout=60, mixed_root=True,
                attachments=(EmailAttachment(existing, "chart.html"),
                             EmailAttachment(Path(directory) / "absent.html", "absent.html")),
            )

        smtp = FakeSmtp.instances[0]
        message = message_from_string(smtp.sendmail_args[2])
        self.assertEqual(count, 1)
        self.assertEqual(smtp.kwargs, {"timeout": 60})
        self.assertEqual(message.get_content_subtype(), "mixed")
        self.assertEqual(message.get_payload()[0].get_content_subtype(), "alternative")
        self.assertEqual(message.get_payload()[1].get_filename(), "chart.html")


def email_settings():
    return SimpleNamespace(
        user="from@example.com", password="a b c", recipients=("to@example.com",),
        smtp_host="smtp.example.com", smtp_port=465,
    )


class PipelineEmailPolicyTests(unittest.TestCase):
    def test_daily_warns_without_retrying_when_transport_raises(self):
        fake_settings = SimpleNamespace(email=email_settings())
        with patch("src.report_generator.settings", fake_settings), \
             patch("src.report_generator.send_multipart_email", side_effect=RuntimeError("offline")) as send:
            send_email_report("Daily", "body")

        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["strip_password_spaces"])
        self.assertNotIn("timeout", send.call_args.kwargs)

    def test_cio_transport_failure_uses_plain_report_fallback_once(self):
        fake_settings = SimpleNamespace(email=email_settings())
        with patch("src.orchestrator.settings", fake_settings), \
             patch("src.orchestrator.send_multipart_email", side_effect=RuntimeError("offline")) as send, \
             patch("src.orchestrator.send_email_report") as fallback:
            _send_cio_email_with_visuals("CIO", "body", {})

        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["timeout"], 60)
        self.assertTrue(send.call_args.kwargs["mixed_root"])
        self.assertTrue(send.call_args.kwargs["strip_password_spaces"])
        fallback.assert_called_once_with("CIO", "body")

    def test_insight_transport_failure_uses_plain_report_fallback_once(self):
        fake_settings = SimpleNamespace(email=email_settings())
        with patch("src.config.settings", fake_settings), \
             patch("scripts.insight_report.send_multipart_email", side_effect=RuntimeError("offline")) as send, \
             patch("src.report_generator.send_email_report") as fallback:
            send_email_with_visuals("body", {}, "Insight")

        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["timeout"], 60)
        self.assertTrue(send.call_args.kwargs["mixed_root"])
        self.assertNotIn("strip_password_spaces", send.call_args.kwargs)
        fallback.assert_called_once_with("Insight", "body")


if __name__ == "__main__":
    unittest.main()
