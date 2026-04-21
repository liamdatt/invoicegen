from __future__ import annotations

import json
from email.message import EmailMessage as PyEmailMessage
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .google import send_invoice_email
from .models import (
    Client,
    GoogleAccount,
    Invoice,
    WhatsAppFollowUp,
    WhatsAppMessageLog,
    WhatsAppSettings,
)
from .whatsapp import WhatsAppSendError, send_follow_up_message


class WhatsAppFollowUpModelTests(TestCase):
    def setUp(self) -> None:
        self.settings = WhatsAppSettings.load()
        self.settings.global_follow_up_days = 120
        self.settings.save()
        self.client = Client.objects.create(name="Alice", phone="+18761234567")

    def test_follow_up_defaults_to_global_interval(self) -> None:
        follow_up = WhatsAppFollowUp.objects.create(
            client=self.client,
            last_service_date=date(2024, 1, 1),
        )
        follow_up.refresh_schedule(settings=self.settings, commit=True)
        self.assertEqual(
            follow_up.next_follow_up_date,
            date(2024, 1, 1) + timedelta(days=120),
        )

    def test_follow_up_override_interval(self) -> None:
        follow_up = WhatsAppFollowUp.objects.create(
            client=self.client,
            last_service_date=date(2024, 1, 1),
            follow_up_days_override=45,
        )
        follow_up.refresh_schedule(settings=self.settings, commit=True)
        self.assertEqual(
            follow_up.next_follow_up_date,
            date(2024, 1, 1) + timedelta(days=45),
        )


@override_settings(TWILIO_CONTENT_SID="HX1234567890abcdef")
class WhatsAppSendTests(TestCase):
    def setUp(self) -> None:
        self.settings = WhatsAppSettings.load()
        self.settings.global_follow_up_days = 30
        self.settings.business_name = "Test Garage"
        self.settings.save()

    @patch("core.whatsapp._sender_number", return_value="whatsapp:+1234567890")
    @patch("core.whatsapp._twilio_client")
    def test_send_follow_up_success(self, mock_twilio_client: MagicMock, mock_sender: MagicMock) -> None:
        client = Client.objects.create(name="Bob", phone="+18761234567")
        follow_up = WhatsAppFollowUp.objects.create(
            client=client,
            last_service_date=timezone.localdate() - timedelta(days=60),
        )
        follow_up.refresh_schedule(settings=self.settings, commit=True)

        message_mock = MagicMock(sid="SM123", status="queued")
        mock_twilio_client.return_value.messages.create.return_value = message_mock

        result = send_follow_up_message(
            follow_up,
            trigger=WhatsAppMessageLog.Trigger.MANUAL,
            settings_obj=self.settings,
        )

        follow_up.refresh_from_db()
        self.assertEqual(result.sid, "SM123")
        self.assertIsNotNone(follow_up.last_sent_at)
        self.assertEqual(
            follow_up.next_follow_up_date,
            timezone.localdate() + timedelta(days=30),
        )
        log = WhatsAppMessageLog.objects.get()
        self.assertEqual(log.status, WhatsAppMessageLog.Status.SENT)
        self.assertEqual(log.trigger, WhatsAppMessageLog.Trigger.MANUAL)
        mock_twilio_client.return_value.messages.create.assert_called_once()
        kwargs = mock_twilio_client.return_value.messages.create.call_args.kwargs
        self.assertEqual(kwargs["content_sid"], "HX1234567890abcdef")
        variables = json.loads(kwargs["content_variables"])
        self.assertEqual(
            variables,
            {
                "1": "Bob",
                "2": "Test Garage",
                "3": "60",
                "4": (timezone.localdate() - timedelta(days=60)).strftime("%B %d, %Y"),
            },
        )
        self.assertNotIn("body", kwargs)

    @override_settings(
        TWILIO_CONTENT_VARIABLE_MAP={"1": "client_name", "2": "missing", "3": "days_since_service"}
    )
    @patch("core.whatsapp._sender_number", return_value="whatsapp:+1234567890")
    @patch("core.whatsapp._twilio_client")
    def test_send_follow_up_custom_variables(self, mock_twilio_client: MagicMock, mock_sender: MagicMock) -> None:
        client = Client.objects.create(name="Dana", phone="+18761234567")
        follow_up = WhatsAppFollowUp.objects.create(client=client)

        message_mock = MagicMock(sid="SM456", status="queued")
        mock_twilio_client.return_value.messages.create.return_value = message_mock

        send_follow_up_message(
            follow_up,
            trigger=WhatsAppMessageLog.Trigger.MANUAL,
            settings_obj=self.settings,
        )

        kwargs = mock_twilio_client.return_value.messages.create.call_args.kwargs
        variables = json.loads(kwargs["content_variables"])
        self.assertEqual(variables, {"1": "Dana"})

    def test_send_follow_up_without_phone_raises(self) -> None:
        client = Client.objects.create(name="Charlie", phone="")
        follow_up = WhatsAppFollowUp.objects.create(client=client)

        with self.assertRaises(WhatsAppSendError):
            send_follow_up_message(
                follow_up,
                trigger=WhatsAppMessageLog.Trigger.MANUAL,
                settings_obj=self.settings,
            )

        self.assertFalse(WhatsAppMessageLog.objects.exists())


class GoogleEmailTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="user",
            email="user@example.com",
            password="pass1234",
        )
        self.account = GoogleAccount.objects.create(user=self.user, email="sender@example.com")
        self.client = Client.objects.create(name="Eve", email="eve@example.com")
        self.invoice = Invoice.objects.create(
            client=self.client,
            date=timezone.localdate(),
        )

    @patch("core.google.build_gmail_service")
    def test_send_invoice_email_attaches_bytes(self, mock_build_service: MagicMock) -> None:
        service = MagicMock()
        users = MagicMock()
        messages = MagicMock()
        send_mock = MagicMock()
        send_mock.execute.return_value = {"id": "MSG123"}
        messages.send.return_value = send_mock
        users.messages.return_value = messages
        service.users.return_value = users
        mock_build_service.return_value = service

        pdf_bytes = b"%PDF-1.4 test"
        original_add_attachment = PyEmailMessage.add_attachment
        captured_payloads: list[tuple[bytes | bytearray, dict]] = []

        def _asserting_add_attachment(message_self, data, *args, **kwargs):
            captured_payloads.append((data, kwargs))
            return original_add_attachment(message_self, data, *args, **kwargs)

        with patch.object(PyEmailMessage, "add_attachment", new=_asserting_add_attachment):
            response = send_invoice_email(
                self.account,
                self.invoice,
                "invoice.pdf",
                pdf_bytes,
                "eve@example.com",
                "Body",
                "Subject",
            )

        self.assertEqual(response, {"id": "MSG123"})
        self.assertTrue(captured_payloads)
        payload, kwargs = captured_payloads[0]
        self.assertIsInstance(payload, (bytes, bytearray))
        self.assertEqual(payload, pdf_bytes)
        self.assertEqual(kwargs["maintype"], "application")
        self.assertEqual(kwargs["subtype"], "pdf")
        self.assertEqual(kwargs["filename"], "invoice.pdf")


class InvoiceNumberingTests(TestCase):
    def setUp(self) -> None:
        self.client_obj = Client.objects.create(name="Numbering Client")

    def _make(self, invoice_type: str, invoice_number: int | None = None) -> Invoice:
        kwargs = dict(
            client=self.client_obj,
            invoice_type=invoice_type,
            date=timezone.localdate(),
        )
        if invoice_number is not None:
            kwargs['invoice_number'] = invoice_number
        return Invoice.objects.create(**kwargs)

    def test_allocate_first_regular_uses_start(self) -> None:
        self.assertEqual(Invoice.allocate_number(Invoice.Type.REGULAR), 2000)

    def test_allocate_next_regular_increments(self) -> None:
        self._make(Invoice.Type.REGULAR, invoice_number=2000)
        self.assertEqual(Invoice.allocate_number(Invoice.Type.REGULAR), 2001)

    def test_allocate_first_general_is_one(self) -> None:
        self.assertEqual(Invoice.allocate_number(Invoice.Type.GENERAL), 1)

    def test_allocate_general_continues_from_max(self) -> None:
        self._make(Invoice.Type.GENERAL, invoice_number=5)
        self._make(Invoice.Type.PROFORMA, invoice_number=7)
        self.assertEqual(Invoice.allocate_number(Invoice.Type.GENERAL), 8)

    def test_allocate_general_skips_regular_numbers(self) -> None:
        self._make(Invoice.Type.PROFORMA, invoice_number=1999)
        self._make(Invoice.Type.REGULAR, invoice_number=2000)
        self._make(Invoice.Type.REGULAR, invoice_number=2002)
        self.assertEqual(Invoice.allocate_number(Invoice.Type.GENERAL), 2001)
        self._make(Invoice.Type.GENERAL, invoice_number=2001)
        self.assertEqual(Invoice.allocate_number(Invoice.Type.PROFORMA), 2003)

    def test_new_invoice_gets_number_assigned_on_save(self) -> None:
        inv = Invoice.objects.create(
            client=self.client_obj,
            invoice_type=Invoice.Type.GENERAL,
            date=timezone.localdate(),
        )
        self.assertIsNotNone(inv.invoice_number)

    def test_first_regular_from_empty_db_gets_2000(self) -> None:
        inv = Invoice.objects.create(
            client=self.client_obj,
            invoice_type=Invoice.Type.REGULAR,
            date=timezone.localdate(),
        )
        self.assertEqual(inv.invoice_number, 2000)

    def test_saving_again_does_not_change_number(self) -> None:
        inv = Invoice.objects.create(
            client=self.client_obj,
            invoice_type=Invoice.Type.GENERAL,
            date=timezone.localdate(),
        )
        original = inv.invoice_number
        inv.vehicle = "updated"
        inv.save()
        inv.refresh_from_db()
        self.assertEqual(inv.invoice_number, original)

    def test_changing_type_does_not_reassign_number(self) -> None:
        inv = Invoice.objects.create(
            client=self.client_obj,
            invoice_type=Invoice.Type.PROFORMA,
            date=timezone.localdate(),
        )
        original = inv.invoice_number
        inv.invoice_type = Invoice.Type.REGULAR
        inv.save()
        inv.refresh_from_db()
        self.assertEqual(inv.invoice_number, original)
        self.assertLess(inv.invoice_number, 2000)

    def test_explicit_invoice_number_is_respected(self) -> None:
        inv = Invoice.objects.create(
            client=self.client_obj,
            invoice_type=Invoice.Type.GENERAL,
            date=timezone.localdate(),
            invoice_number=42,
        )
        self.assertEqual(inv.invoice_number, 42)
