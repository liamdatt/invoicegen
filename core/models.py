from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.contrib.staticfiles import finders
from django.core.files.base import ContentFile
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone

from django.contrib.auth import get_user_model
User = get_user_model()

GCT_RATE = Decimal('0.15')


class Client(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.name


class Invoice(models.Model):
    class Type(models.TextChoices):
        GENERAL = 'GENERAL', 'General'
        PROFORMA = 'PROFORMA', 'Proforma'

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='invoices')
    invoice_type = models.CharField(max_length=10, choices=Type.choices, default=Type.GENERAL)

    vehicle = models.CharField(max_length=255, blank=True)
    lic_no = models.CharField("Lic#", max_length=50, blank=True)
    chassis_no = models.CharField("Chassis#", max_length=100, blank=True)
    date = models.DateField()

    proforma_make = models.CharField("Make", max_length=100, blank=True)
    proforma_model = models.CharField("Model", max_length=100, blank=True)
    proforma_year = models.PositiveIntegerField("Year", blank=True, null=True)
    proforma_colour = models.CharField("Colour", max_length=50, blank=True)
    proforma_cc_rating = models.CharField("CC Rating", max_length=50, blank=True)
    proforma_price = models.DecimalField("Total Cost", max_digits=15, decimal_places=2, blank=True, null=True)
    proforma_currency = models.CharField("Currency", max_length=10, blank=True, default="JMD")

    pdf_file = models.FileField(upload_to='invoices/', blank=True, null=True)
    drive_file_id = models.CharField(max_length=255, blank=True)
    drive_web_view_link = models.URLField(blank=True)
    drive_download_link = models.URLField(blank=True)
    drive_synced_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self) -> str:
        return f"{self.get_invoice_type_display()} #{self.pk or 'new'} - {self.client.name}"

    @property
    def parts_subtotal(self) -> Decimal:
        v = self.items.aggregate(total=models.Sum('parts_cost'))['total'] or Decimal('0')
        return v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def labour_subtotal(self) -> Decimal:
        v = self.items.aggregate(total=models.Sum('labour_cost'))['total'] or Decimal('0')
        return v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def gct(self) -> Decimal:
        return (self.parts_subtotal * GCT_RATE).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        return (self.parts_subtotal + self.labour_subtotal + self.gct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def _money(self, v: Decimal | None, currency: str | None = None) -> str:
        if v is None:
            return ""
        amount = v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        currency = (currency or '').strip()
        if currency:
            return f"{currency} {amount:,.2f}"
        return f"${amount:,.2f}"

    @property
    def proforma_total_formatted(self) -> str:
        return self._money(self.proforma_price, self.proforma_currency)

    def _logo_data_url(self) -> str | None:
        logo_path = None
        for candidate in ("invoicegen/logo.jpeg", "logo.jpeg"):
            resolved = finders.find(candidate)
            if resolved:
                logo_path = Path(resolved)
                break

        if not logo_path or not logo_path.exists():
            return None

        try:
            import base64

            with open(logo_path, "rb") as f:
                logo_data = f.read()
            return f"data:image/jpeg;base64,{base64.b64encode(logo_data).decode()}"
        except Exception:
            return logo_path.resolve().as_uri()

    def _signature_data_url(self) -> str | None:
        signature_path = None
        for candidate in ("Stepmath_signature-no background.png", "resources/Stepmath_signature-no background.png"):
            resolved = finders.find(candidate)
            if resolved:
                signature_path = Path(resolved)
                break

        # Also check direct path in resources folder
        if not signature_path:
            resources_path = Path(settings.BASE_DIR) / "resources" / "Stepmath_signature-no background.png"
            if resources_path.exists():
                signature_path = resources_path

        if not signature_path or not signature_path.exists():
            return None

        try:
            import base64

            with open(signature_path, "rb") as f:
                signature_data = f.read()
            return f"data:image/png;base64,{base64.b64encode(signature_data).decode()}"
        except Exception:
            return signature_path.resolve().as_uri()

    def _render_pdf(self, template_name: str) -> bytes:
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required to generate invoice PDFs. Install the 'playwright' package and its browsers with "
                "'playwright install chromium'."
            ) from exc

        logo_data_url = self._logo_data_url()
        signature_data_url = self._signature_data_url()

        html = render_to_string(
            template_name,
            {
                "invoice": self,
                "for_pdf": True,
                "logo_src": logo_data_url,
                "signature_src": signature_data_url,
            },
        )

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page()
                    page.set_viewport_size({"width": 1280, "height": 1920})
                    page.set_content(html, wait_until="networkidle")
                    page.emulate_media(media="screen")
                    pdf_content = page.pdf(
                        format="A4",
                        print_background=True,
                        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            raise RuntimeError(
                "Playwright could not render the invoice PDF. Ensure Chromium is installed via 'playwright install chromium'."
            ) from exc

        return pdf_content

    def _store_pdf(self, filename: str, pdf_content: bytes, overwrite: bool = True) -> None:
        if overwrite and self.pdf_file:
            self.pdf_file.delete(save=False)
        if not self.pdf_file or overwrite:
            self.pdf_file.save(filename, ContentFile(pdf_content), save=True)

    def generate_general_pdf(self, overwrite: bool = True, store_local: bool = True) -> bytes:
        filename = f"invoice-{self.pk}-general.pdf"
        pdf_content = self._render_pdf("invoices/detail_pdf.html")
        if store_local:
            self._store_pdf(filename, pdf_content, overwrite=overwrite)
        return pdf_content

    def generate_proforma_pdf(self, overwrite: bool = True, store_local: bool = True) -> bytes:
        filename = f"invoice-{self.pk}-proforma.pdf"
        pdf_content = self._render_pdf("invoices/detail_pdf_proforma.html")
        if store_local:
            self._store_pdf(filename, pdf_content, overwrite=overwrite)
        return pdf_content

    def pdf_filename(self) -> str:
        suffix = "general" if self.invoice_type == Invoice.Type.GENERAL else "proforma"
        return f"invoice-{self.pk}-{suffix}.pdf"

    def generate_pdf_bytes(self, overwrite: bool = False, store_local: bool = False) -> tuple[str, bytes]:
        if self.invoice_type == Invoice.Type.GENERAL:
            content = self.generate_general_pdf(overwrite=overwrite, store_local=store_local)
        else:
            content = self.generate_proforma_pdf(overwrite=overwrite, store_local=store_local)
        return self.pdf_filename(), content

    def mark_drive_file(
        self,
        file_id: str,
        web_view_link: str | None,
        download_link: str | None,
        *,
        clear_local: bool = True,
    ) -> None:
        if clear_local and self.pdf_file:
            self.pdf_file.delete(save=False)
            self.pdf_file = None
        self.drive_file_id = file_id
        self.drive_web_view_link = web_view_link or ""
        self.drive_download_link = download_link or ""
        self.drive_synced_at = timezone.now()
        self.save(update_fields=[
            "drive_file_id",
            "drive_web_view_link",
            "drive_download_link",
            "drive_synced_at",
            "pdf_file",
        ])

    def clear_drive_file(self) -> None:
        self.drive_file_id = ""
        self.drive_web_view_link = ""
        self.drive_download_link = ""
        self.drive_synced_at = None
        self.save(update_fields=[
            "drive_file_id",
            "drive_web_view_link",
            "drive_download_link",
            "drive_synced_at",
        ])

    @property
    def has_drive_file(self) -> bool:
        return bool(self.drive_file_id)


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    description = models.CharField(max_length=255)
    labour_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    parts_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ['id']

    def __str__(self) -> str:
        return f"{self.description} (L:{self.labour_cost} P:{self.parts_cost})"


class GoogleAccount(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='google_account')
    email = models.EmailField(blank=True)
    credentials = models.JSONField(default=dict, blank=True)
    drive_folder_id = models.CharField(max_length=255, blank=True)
    drive_folder_name = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Google account for {self.user}" if self.user_id else "Unassigned Google account"

    @staticmethod
    def _parse_expiry(expiry: str | None):
        if not expiry:
            return None
        from datetime import datetime

        try:
            return datetime.fromisoformat(expiry)
        except ValueError:
            return None

    def _serialize_credentials(self, credentials) -> dict:
        data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes or []),
        }
        if getattr(credentials, "expiry", None):
            data["expiry"] = credentials.expiry.isoformat()
        return data

    def save_credentials(self, credentials) -> None:
        self.credentials = self._serialize_credentials(credentials)
        self.save(update_fields=["credentials", "updated_at"])

    def get_credentials(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        data = dict(self.credentials or {})
        if not data:
            raise RuntimeError("No Google credentials stored for this user.")
        expiry = data.get("expiry")
        if isinstance(expiry, str):
            parsed = self._parse_expiry(expiry)
            if parsed is not None:
                data["expiry"] = parsed
            else:
                data.pop("expiry", None)

        credentials = Credentials(**data)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.save_credentials(credentials)
        return credentials

    def clear_credentials(self) -> None:
        self.credentials = {}
        self.drive_folder_id = ""
        self.drive_folder_name = ""
        self.save(update_fields=["credentials", "drive_folder_id", "drive_folder_name", "updated_at"])

    @property
    def is_connected(self) -> bool:
        return bool(self.credentials)

    @property
    def drive_folder_display(self) -> str:
        if not self.drive_folder_id:
            return ""
        return self.drive_folder_name or self.drive_folder_id


class WhatsAppSettings(models.Model):
    DEFAULT_TEMPLATE = (
        "Hi {client_name}, just checking in from {business_name}! It's been {days_since_service} days since we last "
        "serviced you on {last_service_date}. Let us know if you need anything."
    )

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    global_follow_up_days = models.PositiveIntegerField(default=90)
    business_name = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "WhatsApp settings"

    def __str__(self) -> str:
        return "WhatsApp Settings"

    @classmethod
    def load(cls) -> "WhatsAppSettings":
        obj, _ = cls.objects.get_or_create(
            singleton_id=1,
            defaults={
                "global_follow_up_days": 90,
            },
        )
        return obj


class WhatsAppFollowUp(models.Model):
    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name="whatsapp_follow_up")
    is_active = models.BooleanField(default=True)
    last_service_date = models.DateField(null=True, blank=True)
    follow_up_days_override = models.PositiveIntegerField(null=True, blank=True)
    next_follow_up_date = models.DateField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["client__name"]

    def __str__(self) -> str:
        return f"WhatsApp follow-up for {self.client.name}"

    def follow_up_days(self, settings: WhatsAppSettings | None = None) -> int:
        if self.follow_up_days_override:
            return self.follow_up_days_override
        settings = settings or WhatsAppSettings.load()
        return settings.global_follow_up_days

    def compute_next_follow_up_date(self, settings: WhatsAppSettings | None = None) -> date | None:
        if not self.last_service_date:
            return None
        interval = self.follow_up_days(settings=settings)
        return self.last_service_date + timedelta(days=interval)

    def refresh_schedule(self, settings: WhatsAppSettings | None = None, *, commit: bool = True) -> None:
        settings = settings or WhatsAppSettings.load()
        self.next_follow_up_date = self.compute_next_follow_up_date(settings)
        if commit:
            self.save(update_fields=["next_follow_up_date"])

    def register_success(self, *, settings: WhatsAppSettings | None = None) -> None:
        self.last_sent_at = timezone.now()
        self.last_error = ""
        settings = settings or WhatsAppSettings.load()
        # Schedule the next follow-up relative to the send date to keep reminders recurring.
        interval = self.follow_up_days(settings=settings)
        self.next_follow_up_date = timezone.localdate() + timedelta(days=interval)
        self.save(update_fields=["last_sent_at", "last_error", "next_follow_up_date"])

    def register_failure(self, error: str) -> None:
        self.last_error = error
        self.save(update_fields=["last_error"])

    def message_context(self, settings: WhatsAppSettings | None = None) -> dict:
        settings = settings or WhatsAppSettings.load()
        last_service_str = self.last_service_date.strftime("%B %d, %Y") if self.last_service_date else ""
        days_since_service = ""
        if self.last_service_date:
            days_since_service = str((timezone.localdate() - self.last_service_date).days)
        context = {
            "client_name": self.client.name,
            "client_email": self.client.email,
            "client_phone": self.client.phone,
            "business_name": settings.business_name or "our team",
            "last_service_date": last_service_str,
            "days_since_service": days_since_service,
            "next_follow_up_date": self.next_follow_up_date.strftime("%B %d, %Y") if self.next_follow_up_date else "",
            "follow_up_days": str(self.follow_up_days(settings=settings)),
        }
        return context

    def build_message(self, settings: WhatsAppSettings | None = None) -> str:
        settings = settings or WhatsAppSettings.load()

        class _DefaultDict(dict):
            def __missing__(self, key):  # type: ignore[override]
                return ""

        template = WhatsAppSettings.DEFAULT_TEMPLATE
        return template.format_map(_DefaultDict(self.message_context(settings=settings)))


class WhatsAppMessageLog(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"

    follow_up = models.ForeignKey(WhatsAppFollowUp, on_delete=models.CASCADE, related_name="messages")
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    trigger = models.CharField(max_length=20, choices=Trigger.choices, default=Trigger.SCHEDULED)
    body = models.TextField()
    twilio_sid = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"WhatsApp message to {self.follow_up.client.name} ({self.get_status_display()})"
