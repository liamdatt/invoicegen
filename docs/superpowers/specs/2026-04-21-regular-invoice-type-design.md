# Regular Invoice Type — Design

**Date:** 2026-04-21
**Status:** Approved (pending implementation)

## Summary

Add a third invoice type, "Regular", alongside the existing General and Proforma types. Regular invoices share Proforma's layout and single-vehicle field set but display a plain "INVOICE" header (no "Proforma" wording) and include a GCT registration line in the business letterhead. Regular invoices are numbered starting at 2000 in a number space shared with the other types — General and Proforma continue counting from the current high-water mark and skip over any numbers a Regular invoice has already claimed once they reach 2000.

## Goals

- Users can select "Regular" as an invoice type in the create/edit form.
- Regular invoices render using a Proforma-style single-vehicle layout with "INVOICE" as the document title and a GCT registration line added to the business header block.
- The first Regular invoice is numbered 2000, subsequent Regulars count up from there.
- General and Proforma invoices continue their current sequential numbering, skipping over numbers already assigned to Regular invoices when collisions occur.
- Invoice numbers are permanent: editing an invoice's type after creation does not reassign its number.
- Existing invoices retain their current customer-visible numbers.

## Non-goals

- Renaming the `proforma_*` fields to generic vehicle field names. (Considered and deferred — pure cleanup, not required for this change.)
- Redesigning the PDF layout, line-item model, or GCT calculation.
- Per-type sequences backed by DB sequences (would couple us to Postgres and break the SQLite dev fallback).
- Locking the invoice type dropdown on edit. Type remains editable; only `invoice_number` is locked once assigned.

## Current state

- `Invoice.invoice_type` has two choices: `GENERAL` and `PROFORMA`.
- There is no dedicated invoice-number field. The displayed "invoice number" everywhere (PDFs, dashboard, email body, filenames) is the Django auto-increment primary key `invoice.pk`.
- Current DB has 16 invoices; the max `pk` is 16.
- Proforma's PDF is rendered from `templates/invoices/detail_pdf_proforma.html` which includes the `_detail_document_proforma.html` partial. General uses `detail_pdf.html` + `_detail_document_general.html`.
- `Invoice.generate_pdf_bytes()` dispatches between the two templates based on `invoice_type`.

## Design

### 1. Data model (`core/models.py`)

**New type choice.** Add `REGULAR = 'REGULAR', 'Regular'` to `Invoice.Type`.

**New field.**

```python
invoice_number = models.PositiveIntegerField(
    unique=True,
    null=True,
    blank=True,
    db_index=True,
)
```

Nullable at the schema level so the backfill migration can run in two steps. In practice, every persisted row has a non-null number after `save()` completes.

**Constants.** `Invoice.REGULAR_START = 2000` — the lowest number any Regular invoice can claim.

**Allocator.**

```python
@classmethod
def allocate_number(cls, invoice_type: str) -> int:
    with transaction.atomic():
        if invoice_type == cls.Type.REGULAR:
            current_max = cls.objects.filter(invoice_type=cls.Type.REGULAR) \
                             .aggregate(m=models.Max('invoice_number'))['m']
            return max(cls.REGULAR_START, (current_max or (cls.REGULAR_START - 1)) + 1)
        else:
            current_max = cls.objects.filter(
                invoice_type__in=[cls.Type.GENERAL, cls.Type.PROFORMA]
            ).aggregate(m=models.Max('invoice_number'))['m'] or 0
            candidate = current_max + 1
            taken = set(
                cls.objects.filter(invoice_number__gte=candidate)
                           .values_list('invoice_number', flat=True)
            )
            while candidate in taken:
                candidate += 1
            return candidate
```

- The Regular branch never scans existing General/Proforma numbers because Regulars start at 2000, well above the current high-water (16).
- The General/Proforma branch scans the "taken" set above its candidate so it can walk past any Regular-assigned number once the sequence catches up to 2000.
- **Concurrency.** `select_for_update()` was considered but its row-lock semantics don't help here: we're aggregating, not locking specific rows. Instead the allocator relies on the `unique=True` constraint on `invoice_number` as the serialization point: if two concurrent creates allocate the same number, one `INSERT` succeeds and the other raises `IntegrityError`. Callers (`Invoice.save()`) catch `IntegrityError`, re-run `allocate_number()`, and retry the insert up to 3 times. On SQLite (dev), writes are serialized at the connection level so the retry loop is effectively unreachable. On Postgres (prod), the retry loop is the actual correctness mechanism.

**`save()` override.** On insert (`self.pk is None` and `self.invoice_number is None`), call `allocate_number(self.invoice_type)`, assign, and attempt the insert inside a retry loop: on `IntegrityError` caused by the `invoice_number` unique constraint, re-allocate and retry (max 3 attempts). On update, leave `invoice_number` untouched. Numbers are permanent across type edits; a direct `Invoice.objects.update(...)` or explicit external assignment of `invoice_number` also bypasses allocation (intentional — allows data repair).

### 2. Migrations

**Migration A — schema.**
- Extend `invoice_type` choices to include `REGULAR`.
- Add `invoice_number` as `PositiveIntegerField(null=True, blank=True, db_index=True)` (not yet unique).

**Migration B — data backfill.**
- For every existing row, set `invoice_number = pk`. This preserves the numbers already printed on past PDFs and emailed to customers.
- Forward-only. Reverse migration is a no-op.

**Migration C — uniqueness.**
- Alter `invoice_number` to add `unique=True`. Safe to run after backfill because `pk` is unique by construction.

Keeping backfill and uniqueness in separate migrations avoids the pitfall of a single migration that tries to add a unique constraint before the backfill runs.

### 3. Form & UI

**`core/forms.py`:**
- No new field-list constants. `InvoiceForm.Meta.fields` already includes `INVOICE_PROFORMA_FIELDS`; Regular reuses those fields unchanged.
- Extend `InvoiceForm.clean()` so the required-field check for `proforma_make`, `proforma_model`, `proforma_price` applies to both `PROFORMA` and `REGULAR` (currently hardcoded to `PROFORMA` at forms.py:109). Error messages should read "required for this invoice type" rather than "required for proforma invoices."
- No change to the `data-proforma-required` widget attributes — they continue to annotate the same three fields for both types.

**`templates/invoices/form.html`:**
- Add "Regular" option to the type dropdown.
- One-attribute diff on the single-vehicle field section: change `data-invoice-type-visible="PROFORMA"` to `data-invoice-type-visible="PROFORMA,REGULAR"`. The existing JS at form.html:408–410 already splits on comma and does case-insensitive matching, so no JS change is needed.
- Dropdown remains editable on edit (per non-goals). Numbers are protected by the `save()` override, not by UI constraints.

### 4. PDF templates

**New files:**
- `templates/invoices/_detail_document_regular.html` — copy of `_detail_document_proforma.html` with three diffs:
  1. `<div class="document-title">INVOICE</div>` (was `PROFORMA INVOICE`).
  2. Add a line under `Email: stepmathauto100@gmail.com`: `<div>GCT REG. NO. 001-621-840</div>`.
  3. Remove the `proforma` CSS class from the outer `.invoice-screen` / `.invoice-paper` divs (or rename styles if coupling is tight — decide during implementation).
- `templates/invoices/detail_pdf_regular.html` — copy of `detail_pdf_proforma.html` with `<title>Invoice {{ invoice.invoice_number }}</title>` and the include pointed at `_detail_document_regular.html`.

**Existing templates and code updated to use `invoice_number` instead of `invoice.pk` for display:**
- `templates/dashboard.html` (line 282)
- `templates/clients/detail.html` (line 117)
- `templates/emails/invoice_email.txt` (line 3)
- `templates/invoices/detail_pdf.html` and `detail_pdf_proforma.html` (title tags)
- `templates/invoices/_detail_document_general.html`, `_detail_document_proforma.html`, and the new `_detail_document_regular.html` wherever a number is shown
- `core/views.py:550` — `subject = f"Invoice #{invoice.invoice_number}"` in `invoice_send_email` (customer-visible email subject)

**URL `{% url 'invoice_pdf' invoice.pk %}` references stay on `pk`** — those are database lookups, not user-facing numbers, and the URL routing still goes by primary key.

**Internal-only references stay on `pk`** — e.g., `core/management/commands/regenerate_pdfs.py` uses `invoice.pk` in log output, which is fine for operator-facing logs.

**Drive filenames.** `core/google.py::upload_invoice_pdf` uses the filename only as the Drive file's display name (no parsing). New uploads will use `invoice-{invoice_number}-…`. Existing Drive files keep their old `invoice-{pk}-…` names; for the 16 rows already in the DB, `invoice_number == pk` after backfill, so there's no visible inconsistency.

### 5. Invoice class plumbing

Extend `Invoice`'s PDF methods to handle the Regular case:

```python
def generate_regular_pdf(self, overwrite=True, store_local=True) -> bytes:
    filename = f"invoice-{self.invoice_number}-regular.pdf"
    pdf_content = self._render_pdf("invoices/detail_pdf_regular.html")
    if store_local:
        self._store_pdf(filename, pdf_content, overwrite=overwrite)
    return pdf_content

def pdf_filename(self) -> str:
    suffix = {
        self.Type.GENERAL: "general",
        self.Type.PROFORMA: "proforma",
        self.Type.REGULAR: "regular",
    }[self.invoice_type]
    return f"invoice-{self.invoice_number}-{suffix}.pdf"

def generate_pdf_bytes(self, overwrite=False, store_local=False) -> tuple[str, bytes]:
    if self.invoice_type == self.Type.GENERAL:
        content = self.generate_general_pdf(overwrite=overwrite, store_local=store_local)
    elif self.invoice_type == self.Type.PROFORMA:
        content = self.generate_proforma_pdf(overwrite=overwrite, store_local=store_local)
    else:
        content = self.generate_regular_pdf(overwrite=overwrite, store_local=store_local)
    return self.pdf_filename(), content
```

Also update the existing `generate_general_pdf` / `generate_proforma_pdf` filename strings to use `self.invoice_number` instead of `self.pk` for consistency with customer-visible numbering.

### 6. Views and partial dispatch

- The PDF-template dispatch lives entirely in `Invoice.generate_pdf_bytes()` (model-layer), not in any view. The `REGULAR` branch is added there (see §5).
- `core/views.py::invoice_detail` renders the wrapper `templates/invoices/detail.html`, which is responsible for including the right on-screen document partial based on `invoice.invoice_type`. Extend the include logic in `detail.html` (or in the partials it already picks) to cover `REGULAR → _detail_document_regular.html`.
- `invoice_pdf`: no changes (flows through `generate_pdf_bytes()`).
- `invoice_send_email`: update the hardcoded `subject = f"Invoice #{invoice.pk}"` (views.py:550) to use `invoice.invoice_number`. No other logic changes.

### 7. Tests (`core/tests.py`)

Targeted unit tests covering the allocator and save flow:
- First Regular invoice is assigned number 2000.
- Second Regular invoice is assigned 2001.
- First General invoice (with no prior invoices) is assigned 1.
- When `max(invoice_number)` over General/Proforma is 1999 and a Regular has claimed 2000, the next General is assigned 2001. Similarly, if Regular has claimed 2000 and 2002, next General after 1999 goes 2001 → skip 2002 → 2003 progression proven.
- Creating an invoice assigns `invoice_number` exactly once; subsequent `save()` calls do not mutate it.
- Changing `invoice_type` on an existing invoice (e.g., Proforma → Regular) does not reassign `invoice_number`.
- Backfill migration sets `invoice_number = pk` for all rows that existed before the migration.

## Data flow

```
create invoice (POST /clients/<id>/invoices/new/)
  → InvoiceForm.save() (commit=False)
  → Invoice.save()
      → if pk is None and invoice_number is None:
          invoice_number = Invoice.allocate_number(self.invoice_type)
      → INSERT row
  → ItemFormSet.save() (General only)
  → redirect to invoice_detail

render PDF (GET /invoices/<id>/pdf/)
  → Invoice.generate_pdf_bytes()
      → dispatch on invoice_type to general/proforma/regular PDF renderer
      → _render_pdf(template)
          → render_to_string with logo_src + signature_src data URLs
          → Playwright headless Chromium → PDF bytes
      → pdf_filename() uses invoice_number (not pk)
  → FileResponse
```

## Error handling

- Allocator runs inside `transaction.atomic()`; if the surrounding request transaction rolls back, the allocated number is never persisted.
- Concurrency correctness comes from the `unique=True` constraint on `invoice_number`: two concurrent creates that allocate the same number will have exactly one insert succeed; the loser catches `IntegrityError`, re-allocates, and retries (max 3 attempts in `Invoice.save()`). `select_for_update()` was deliberately not used because aggregate queries don't lock rows meaningfully.
- `InvoiceForm.clean()` enforces that `proforma_make`, `proforma_model`, and `proforma_price` are present for both `PROFORMA` and `REGULAR` types.
- If `allocate_number()` is called with an unrecognized `invoice_type`, treat it as General/Proforma (default branch). In practice the form choices prevent this path.

## Rollout

1. Deploy schema migration A.
2. Deploy backfill migration B (idempotent if re-run: `WHERE invoice_number IS NULL`).
3. Deploy uniqueness migration C.
4. Deploy code changes (form, views, templates, tests).

Migrations and code can ship in one deploy since Django migrations run before the new process binds; the backfill completes before the new `save()` override runs against any new requests.

## Open questions

None. All prior questions resolved:
- Shared number space with skip-on-collision semantics — confirmed.
- Document title becomes plain "INVOICE" — confirmed.
- Reuse `proforma_*` fields for Regular (no rename) — confirmed.
- Numbers are permanent across type edits — confirmed.
- GCT registration line added under email in Regular template — confirmed.
