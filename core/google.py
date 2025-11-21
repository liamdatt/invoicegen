"""Utilities for Google OAuth, Drive and Gmail integrations."""
from __future__ import annotations

import base64
import io
from email.message import EmailMessage
from typing import TYPE_CHECKING, List

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse

from .models import GoogleAccount, Invoice

if TYPE_CHECKING:  # pragma: no cover - typing helper
    from google_auth_oauthlib.flow import Flow

SCOPES = (
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.metadata",
    "openid",
)


class GoogleConfigurationError(RuntimeError):
    """Raised when Google OAuth configuration is missing."""


def _get_flow_class():
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GoogleConfigurationError(
            "Google OAuth libraries are not installed. Add 'google-auth-oauthlib' to your environment."
        ) from exc
    return Flow


def _get_build_function():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GoogleConfigurationError(
            "Google API client library is missing. Install 'google-api-python-client'."
        ) from exc
    return build


def _get_media_classes():
    try:
        from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GoogleConfigurationError(
            "Google API client library is missing. Install 'google-api-python-client'."
        ) from exc
    return MediaInMemoryUpload, MediaIoBaseDownload


def _client_config() -> dict:
    client_id = getattr(settings, "GOOGLE_CLIENT_ID", None)
    client_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", None)
    if not client_id or not client_secret:
        raise GoogleConfigurationError(
            "Google OAuth client configuration is missing. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the environment."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def build_flow(request: HttpRequest) -> Flow:
    Flow = _get_flow_class()
    redirect_uri = request.build_absolute_uri(reverse("google_callback"))
    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)


def ensure_account(user) -> GoogleAccount:
    account, _ = GoogleAccount.objects.get_or_create(user=user)
    return account


def build_drive_service(account: GoogleAccount):
    credentials = account.get_credentials()
    build = _get_build_function()
    return build("drive", "v3", credentials=credentials)


def build_gmail_service(account: GoogleAccount):
    credentials = account.get_credentials()
    build = _get_build_function()
    return build("gmail", "v1", credentials=credentials)


def list_drive_folders(account: GoogleAccount) -> List[dict]:
    service = build_drive_service(account)
    response = (
        service.files()
        .list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            spaces="drive",
            fields="files(id, name)"
        )
        .execute()
    )
    return sorted(response.get("files", []), key=lambda f: f.get("name", ""))


def upload_invoice_pdf(account: GoogleAccount, invoice: Invoice, filename: str, content: bytes) -> dict:
    service = build_drive_service(account)
    MediaInMemoryUpload, _ = _get_media_classes()
    media = MediaInMemoryUpload(content, mimetype="application/pdf", resumable=False)
    metadata = {
        "name": filename,
    }
    if account.drive_folder_id:
        metadata["parents"] = [account.drive_folder_id]

    file_id = invoice.drive_file_id
    if file_id:
        file = (
            service.files()
            .update(fileId=file_id, media_body=media, fields="id, name, webViewLink, webContentLink")
            .execute()
        )
    else:
        file = (
            service.files()
            .create(body=metadata, media_body=media, fields="id, name, webViewLink, webContentLink")
            .execute()
        )
    return file


def download_drive_file(account: GoogleAccount, file_id: str) -> bytes:
    service = build_drive_service(account)
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    _, MediaIoBaseDownload = _get_media_classes()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def send_invoice_email(
    account: GoogleAccount,
    invoice: Invoice,
    filename: str,
    pdf_content: bytes,
    to_address: str,
    message_body: str,
    subject: str,
) -> dict:
    service = build_gmail_service(account)
    email = EmailMessage()
    sender = account.email or "me"
    email["To"] = to_address
    email["From"] = sender
    email["Subject"] = subject
    email.set_content(message_body)
    email.add_attachment(
        pdf_content,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )

    raw_message = base64.urlsafe_b64encode(email.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw_message}).execute()


def fetch_account_email(account: GoogleAccount) -> str:
    credentials = account.get_credentials()
    build = _get_build_function()
    service = build("oauth2", "v2", credentials=credentials)
    profile = service.userinfo().get().execute()
    email = profile.get("email", "")
    if email and email != account.email:
        account.email = email
        account.save(update_fields=["email", "updated_at"])
    return account.email
