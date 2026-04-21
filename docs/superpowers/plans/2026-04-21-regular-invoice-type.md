# Regular Invoice Type Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third "Regular" invoice type that reuses the Proforma layout with an "INVOICE" header and a GCT registration line, backed by a new `invoice_number` field with a shared number space where Regular starts at 2000 and General/Proforma skip over Regular-claimed numbers on collision.

**Architecture:** New `invoice_number` integer field separate from the database primary key, assigned inside `Invoice.save()` via an `allocate_number()` classmethod with retry-on-IntegrityError for concurrency. A three-step migration (schema → data backfill → unique constraint) preserves the numbers already shown on existing invoices. The Regular PDF is a copy of the Proforma template with only the document title and a GCT registration line changed.

**Tech Stack:** Django 5.2, Playwright (PDF rendering), SQLite (dev), Postgres (prod). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-21-regular-invoice-type-design.md`

---

## File Structure

**Modified:**
- `core/models.py` — add `REGULAR` type choice, `invoice_number` field, `REGULAR_START` constant, `allocate_number()` classmethod, `save()` override with retry loop, `generate_regular_pdf()` method, update `pdf_filename()` and `generate_pdf_bytes()`, update existing PDF filename strings to use `invoice_number`.
- `core/forms.py` — extend `InvoiceForm.clean()` so `proforma_make`/`proforma_model`/`proforma_price` are required for both `PROFORMA` and `REGULAR`. Update error-message wording.
- `core/views.py` — line 550: change `subject = f"Invoice #{invoice.pk}"` to `f"Invoice #{invoice.invoice_number}"`.
- `core/tests.py` — add `InvoiceNumberingTests`, `InvoiceFormCleanTests`, `InvoicePdfDispatchTests`.
- `templates/invoices/form.html` — line 133: change `data-invoice-type-visible="PROFORMA"` to `"PROFORMA,REGULAR"`. No JS change.
- `templates/invoices/detail.html` — currently always includes `_detail_document.html`; replace with a `{% if/elif %}` that picks general/proforma/regular partial based on `invoice.invoice_type`. (Verify during implementation whether `_detail_document.html` is already dispatching or if `detail.html` needs the branching.)
- `templates/invoices/detail_pdf.html` — title tag: `invoice.pk` → `invoice.invoice_number`.
- `templates/invoices/detail_pdf_proforma.html` — title tag: `invoice.pk` → `invoice.invoice_number`.
- `templates/invoices/_detail_document_general.html` — any displayed invoice number: `invoice.pk` → `invoice.invoice_number`.
- `templates/invoices/_detail_document_proforma.html` — same.
- `templates/dashboard.html` — line 282: `inv.pk` → `inv.invoice_number`.
- `templates/clients/detail.html` — line 117: `inv.pk` → `inv.invoice_number`.
- `templates/emails/invoice_email.txt` — line 3: `invoice.pk` → `invoice.invoice_number`.

**Created:**
- `core/migrations/0007_invoice_regular_type_and_number.py` — adds `REGULAR` to `invoice_type` choices and `invoice_number` as nullable `PositiveIntegerField(db_index=True)`.
- `core/migrations/0008_backfill_invoice_number.py` — sets `invoice_number = pk` for all existing rows.
- `core/migrations/0009_invoice_number_unique.py` — alters `invoice_number` to `unique=True`.
- `templates/invoices/_detail_document_regular.html` — copy of `_detail_document_proforma.html` with `document-title` changed from `PROFORMA INVOICE` to `INVOICE`, a GCT reg. line added under the email line, and the `proforma` CSS class left unchanged (it only scopes styles, not user-visible).
- `templates/invoices/detail_pdf_regular.html` — copy of `detail_pdf_proforma.html` with title `Invoice {{ invoice.invoice_number }}` and the include pointed at `_detail_document_regular.html`.

**Untouched:** `core/urls.py`, `core/google.py` (Drive uses filename only as display name; no parsing), management commands (internal-only `pk` references are fine).

---

## Task 1: Schema migration — add REGULAR type choice and `invoice_number` field

**Files:**
- Modify: `core/models.py` (Invoice.Type, Invoice fields)
- Create: `core/migrations/0007_invoice_regular_type_and_number.py`

- [ ] **Step 1: Edit the `Type` choices in `core/models.py`**

In `Invoice.Type`, add:

```python
REGULAR = 'REGULAR', 'Regular'
```

Full block after edit:

```python
class Type(models.TextChoices):
    GENERAL = 'GENERAL', 'General'
    PROFORMA = 'PROFORMA', 'Proforma'
    REGULAR = 'REGULAR', 'Regular'
```

- [ ] **Step 2: Add the `invoice_number` field and constant**

Add near the top of the `Invoice` class (under the `Type` inner class, before the `client` ForeignKey):

```python
REGULAR_START = 2000
```

Add after the existing `proforma_*` fields, before `pdf_file`:

```python
invoice_number = models.PositiveIntegerField(
    null=True,
    blank=True,
    db_index=True,
)
```

Note: no `unique` kwarg here (Django defaults to `unique=False`). Task 3 adds `unique=True` after the backfill runs. Keeping the two states different this way ensures `makemigrations` in Task 3 detects the change.

- [ ] **Step 3: Generate the migration**

Run: `venv/bin/python manage.py makemigrations core --name invoice_regular_type_and_number`

Expected: creates `core/migrations/0007_invoice_regular_type_and_number.py` with an `AlterField` on `invoice_type` (choices) and an `AddField` for `invoice_number`.

- [ ] **Step 4: Verify migration applies cleanly**

Run: `venv/bin/python manage.py migrate core`

Expected: `Applying core.0007_invoice_regular_type_and_number... OK` and the Django dev DB now has a nullable `invoice_number` column.

Sanity check in shell:

```
venv/bin/python manage.py shell -c "from core.models import Invoice; print([(i.pk, i.invoice_number) for i in Invoice.objects.all()[:3]])"
```

Expected: list of tuples with `invoice_number=None`.

- [ ] **Step 5: Commit**

```bash
git add core/models.py core/migrations/0007_invoice_regular_type_and_number.py
git commit -m "Add REGULAR invoice type choice and nullable invoice_number field"
```

---

## Task 2: Data migration — backfill `invoice_number = pk`

**Files:**
- Create: `core/migrations/0008_backfill_invoice_number.py`

- [ ] **Step 1: Generate the empty data migration**

Run: `venv/bin/python manage.py makemigrations core --empty --name backfill_invoice_number`

Expected: creates `core/migrations/0008_backfill_invoice_number.py` with an empty `operations = []`.

- [ ] **Step 2: Write the forward operation**

Replace the file contents with:

```python
from django.db import migrations


def backfill_invoice_number(apps, schema_editor):
    Invoice = apps.get_model('core', 'Invoice')
    for invoice in Invoice.objects.filter(invoice_number__isnull=True):
        invoice.invoice_number = invoice.pk
        invoice.save(update_fields=['invoice_number'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_invoice_regular_type_and_number'),
    ]

    operations = [
        migrations.RunPython(backfill_invoice_number, migrations.RunPython.noop),
    ]
```

Filtering on `invoice_number__isnull=True` makes re-runs safe.

- [ ] **Step 3: Apply and verify**

Run: `venv/bin/python manage.py migrate core`

Expected: `Applying core.0008_backfill_invoice_number... OK`.

Verify in shell:

```
venv/bin/python manage.py shell -c "from core.models import Invoice; print([(i.pk, i.invoice_number) for i in Invoice.objects.all()]); print('null count:', Invoice.objects.filter(invoice_number__isnull=True).count())"
```

Expected: every row has `invoice_number == pk`; null count is 0.

- [ ] **Step 4: Commit**

```bash
git add core/migrations/0008_backfill_invoice_number.py
git commit -m "Backfill invoice_number from pk for existing invoices"
```

---

## Task 3: Schema migration — add unique constraint

**Files:**
- Modify: `core/models.py` (change `unique=False` to `unique=True`)
- Create: `core/migrations/0009_invoice_number_unique.py`

- [ ] **Step 1: Change the field definition**

In `core/models.py`, add `unique=True` to the `invoice_number` field. Final definition:

```python
invoice_number = models.PositiveIntegerField(
    unique=True,
    null=True,
    blank=True,
    db_index=True,
)
```

- [ ] **Step 2: Generate the migration**

Run: `venv/bin/python manage.py makemigrations core --name invoice_number_unique`

Expected: creates `core/migrations/0009_invoice_number_unique.py` with an `AlterField` that adds `unique=True`.

- [ ] **Step 3: Apply and verify**

Run: `venv/bin/python manage.py migrate core`

Expected: applies cleanly. (The 16 existing rows have unique `pk`-derived numbers, so the constraint is satisfied.)

Verify the constraint exists (SQLite):

```
venv/bin/python manage.py dbshell <<< ".schema core_invoice" | grep -i invoice_number
```

Expected: `"invoice_number" integer NULL UNIQUE` (or similar showing the unique flag).

- [ ] **Step 4: Commit**

```bash
git add core/models.py core/migrations/0009_invoice_number_unique.py
git commit -m "Add unique constraint to invoice_number"
```

---

## Task 4: `allocate_number()` classmethod — TDD

**Files:**
- Modify: `core/models.py` (add classmethod)
- Modify: `core/tests.py` (add `InvoiceNumberingTests`)

- [ ] **Step 1: Write failing tests**

Append to `core/tests.py` (add imports for `Client` and `Invoice` if not already imported at top-level — they are):

```python
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
        # Simulate General catching up to Regular range.
        self._make(Invoice.Type.PROFORMA, invoice_number=1999)
        self._make(Invoice.Type.REGULAR, invoice_number=2000)
        self._make(Invoice.Type.REGULAR, invoice_number=2002)
        # Next non-Regular candidate is 2000, which is taken; 2001 free; allocator should return 2001.
        self.assertEqual(Invoice.allocate_number(Invoice.Type.GENERAL), 2001)
        # If 2001 is then taken, next should skip 2002 and return 2003.
        self._make(Invoice.Type.GENERAL, invoice_number=2001)
        self.assertEqual(Invoice.allocate_number(Invoice.Type.PROFORMA), 2003)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python manage.py test core.tests.InvoiceNumberingTests -v 2`

Expected: All 5 tests FAIL with `AttributeError: type object 'Invoice' has no attribute 'allocate_number'`.

- [ ] **Step 3: Implement `allocate_number()`**

Add to `core/models.py` inside the `Invoice` class (before `_money`):

```python
@classmethod
def allocate_number(cls, invoice_type: str) -> int:
    from django.db import transaction

    with transaction.atomic():
        if invoice_type == cls.Type.REGULAR:
            current_max = cls.objects.filter(
                invoice_type=cls.Type.REGULAR
            ).aggregate(m=models.Max('invoice_number'))['m']
            if current_max is None:
                return cls.REGULAR_START
            return current_max + 1

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python manage.py test core.tests.InvoiceNumberingTests -v 2`

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/models.py core/tests.py
git commit -m "Add Invoice.allocate_number with skip-on-collision for shared number space"
```

---

## Task 5: `save()` override that calls allocator with retry — TDD

**Files:**
- Modify: `core/models.py` (override `save`)
- Modify: `core/tests.py` (add tests)

- [ ] **Step 1: Write failing tests**

Append to `InvoiceNumberingTests`:

```python
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
        self.assertLess(inv.invoice_number, 2000)  # still in General/Proforma range

    def test_explicit_invoice_number_is_respected(self) -> None:
        inv = Invoice.objects.create(
            client=self.client_obj,
            invoice_type=Invoice.Type.GENERAL,
            date=timezone.localdate(),
            invoice_number=42,
        )
        self.assertEqual(inv.invoice_number, 42)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python manage.py test core.tests.InvoiceNumberingTests -v 2`

Expected: the 5 new tests FAIL (number stays `None` on create).

- [ ] **Step 3: Implement the `save()` override**

Add to `core/models.py` inside `Invoice`, after `__str__`:

```python
def save(self, *args, **kwargs):
    from django.db import IntegrityError, transaction

    if self.pk is not None or self.invoice_number is not None:
        return super().save(*args, **kwargs)

    last_error = None
    for _ in range(3):
        self.invoice_number = Invoice.allocate_number(self.invoice_type)
        try:
            with transaction.atomic():
                return super().save(*args, **kwargs)
        except IntegrityError as exc:
            last_error = exc
            self.invoice_number = None
            continue
    raise last_error
```

Design notes (do not put in code):
- If `self.pk` is set, this is an update — never reallocate.
- If caller passed an explicit `invoice_number`, respect it (allows data repair + matches `test_explicit_invoice_number_is_respected`).
- Otherwise allocate, then retry up to 3 times on IntegrityError (concurrent inserts colliding on the unique constraint).

- [ ] **Step 4: Run the numbering tests**

Run: `venv/bin/python manage.py test core.tests.InvoiceNumberingTests -v 2`

Expected: all 10 tests PASS.

- [ ] **Step 5: Run the full test suite to catch regressions**

Run: `venv/bin/python manage.py test core -v 1`

Expected: all tests pass. Pay attention to `GoogleEmailTests.setUp` — it calls `Invoice.objects.create(client=..., date=...)` without setting `invoice_type`, which defaults to `GENERAL`. Save should now assign an `invoice_number` automatically. No test assertion should break.

- [ ] **Step 6: Commit**

```bash
git add core/models.py core/tests.py
git commit -m "Auto-assign invoice_number on create with retry-on-IntegrityError"
```

---

## Task 6: PDF dispatch + filename update for Regular — TDD

**Files:**
- Modify: `core/models.py` (`generate_regular_pdf`, `pdf_filename`, `generate_pdf_bytes`, existing filename strings)
- Modify: `core/tests.py` (add `InvoicePdfDispatchTests`)

- [ ] **Step 1: Write failing tests**

Append to `core/tests.py`:

```python
from unittest.mock import patch


class InvoicePdfDispatchTests(TestCase):
    def setUp(self) -> None:
        self.client_obj = Client.objects.create(name="PDF Client")

    def _make(self, invoice_type: str) -> Invoice:
        return Invoice.objects.create(
            client=self.client_obj,
            invoice_type=invoice_type,
            date=timezone.localdate(),
        )

    def test_pdf_filename_uses_invoice_number_and_type_suffix(self) -> None:
        inv = self._make(Invoice.Type.REGULAR)
        self.assertEqual(inv.pdf_filename(), f"invoice-{inv.invoice_number}-regular.pdf")

        inv2 = self._make(Invoice.Type.GENERAL)
        self.assertEqual(inv2.pdf_filename(), f"invoice-{inv2.invoice_number}-general.pdf")

        inv3 = self._make(Invoice.Type.PROFORMA)
        self.assertEqual(inv3.pdf_filename(), f"invoice-{inv3.invoice_number}-proforma.pdf")

    def test_generate_pdf_bytes_dispatches_regular_template(self) -> None:
        inv = self._make(Invoice.Type.REGULAR)
        with patch.object(Invoice, '_render_pdf', return_value=b"%PDF-test") as mock_render:
            filename, content = inv.generate_pdf_bytes(overwrite=False, store_local=False)
        mock_render.assert_called_once_with("invoices/detail_pdf_regular.html")
        self.assertEqual(filename, f"invoice-{inv.invoice_number}-regular.pdf")
        self.assertEqual(content, b"%PDF-test")

    def test_generate_pdf_bytes_still_dispatches_proforma(self) -> None:
        inv = self._make(Invoice.Type.PROFORMA)
        with patch.object(Invoice, '_render_pdf', return_value=b"%PDF-p") as mock_render:
            filename, _ = inv.generate_pdf_bytes(overwrite=False, store_local=False)
        mock_render.assert_called_once_with("invoices/detail_pdf_proforma.html")
        self.assertEqual(filename, f"invoice-{inv.invoice_number}-proforma.pdf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python manage.py test core.tests.InvoicePdfDispatchTests -v 2`

Expected: tests fail because `pdf_filename()` currently uses `self.pk` and there's no REGULAR branch in `generate_pdf_bytes()`.

- [ ] **Step 3: Update existing PDF generator filenames and add Regular variant**

In `core/models.py`:

Change `generate_general_pdf`:

```python
def generate_general_pdf(self, overwrite: bool = True, store_local: bool = True) -> bytes:
    filename = f"invoice-{self.invoice_number}-general.pdf"
    pdf_content = self._render_pdf("invoices/detail_pdf.html")
    if store_local:
        self._store_pdf(filename, pdf_content, overwrite=overwrite)
    return pdf_content
```

Change `generate_proforma_pdf` similarly (swap `self.pk` for `self.invoice_number`).

Add `generate_regular_pdf` below:

```python
def generate_regular_pdf(self, overwrite: bool = True, store_local: bool = True) -> bytes:
    filename = f"invoice-{self.invoice_number}-regular.pdf"
    pdf_content = self._render_pdf("invoices/detail_pdf_regular.html")
    if store_local:
        self._store_pdf(filename, pdf_content, overwrite=overwrite)
    return pdf_content
```

Update `pdf_filename`:

```python
def pdf_filename(self) -> str:
    suffix = {
        Invoice.Type.GENERAL: "general",
        Invoice.Type.PROFORMA: "proforma",
        Invoice.Type.REGULAR: "regular",
    }[self.invoice_type]
    return f"invoice-{self.invoice_number}-{suffix}.pdf"
```

Update `generate_pdf_bytes`:

```python
def generate_pdf_bytes(self, overwrite: bool = False, store_local: bool = False) -> tuple[str, bytes]:
    if self.invoice_type == Invoice.Type.GENERAL:
        content = self.generate_general_pdf(overwrite=overwrite, store_local=store_local)
    elif self.invoice_type == Invoice.Type.PROFORMA:
        content = self.generate_proforma_pdf(overwrite=overwrite, store_local=store_local)
    else:
        content = self.generate_regular_pdf(overwrite=overwrite, store_local=store_local)
    return self.pdf_filename(), content
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python manage.py test core.tests.InvoicePdfDispatchTests -v 2`

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/models.py core/tests.py
git commit -m "Route REGULAR invoices to regular PDF template and invoice_number filenames"
```

---

## Task 7: Form validation for REGULAR — TDD

**Files:**
- Modify: `core/forms.py` (`InvoiceForm.clean`)
- Modify: `core/tests.py` (add `InvoiceFormCleanTests`)

- [ ] **Step 1: Write failing tests**

Append to `core/tests.py`:

```python
from core.forms import InvoiceForm


class InvoiceFormCleanTests(TestCase):
    def setUp(self) -> None:
        self.client_obj = Client.objects.create(name="Form Client")

    def _form(self, invoice_type: str, **extra) -> InvoiceForm:
        data = {
            "client": self.client_obj.pk,
            "invoice_type": invoice_type,
            "date": timezone.localdate().isoformat(),
            "chassis_no": "",
            "engine_no": "",
            "vehicle": "",
            "lic_no": "",
            "proforma_make": "",
            "proforma_model": "",
            "proforma_year": "",
            "proforma_colour": "",
            "proforma_cc_rating": "",
            "proforma_price": "",
            "proforma_currency": "",
        }
        data.update(extra)
        return InvoiceForm(data=data)

    def test_regular_requires_make_model_price(self) -> None:
        form = self._form(Invoice.Type.REGULAR)
        self.assertFalse(form.is_valid())
        self.assertIn("proforma_make", form.errors)
        self.assertIn("proforma_model", form.errors)
        self.assertIn("proforma_price", form.errors)

    def test_regular_valid_with_required_fields(self) -> None:
        form = self._form(
            Invoice.Type.REGULAR,
            proforma_make="Toyota",
            proforma_model="Corolla",
            proforma_price="250000",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_general_does_not_require_proforma_fields(self) -> None:
        form = self._form(Invoice.Type.GENERAL)
        self.assertTrue(form.is_valid(), form.errors)

    def test_proforma_still_requires_make_model_price(self) -> None:
        form = self._form(Invoice.Type.PROFORMA)
        self.assertFalse(form.is_valid())
        self.assertIn("proforma_make", form.errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python manage.py test core.tests.InvoiceFormCleanTests -v 2`

Expected: `test_regular_requires_make_model_price` fails (form passes validation for Regular with empty fields).

- [ ] **Step 3: Update `InvoiceForm.clean()`**

In `core/forms.py`, change:

```python
if invoice_type == Invoice.Type.PROFORMA:
    required_fields = {
        "proforma_make": "Make",
        "proforma_model": "Model",
        "proforma_price": "Total Cost",
    }
    for field_name, label in required_fields.items():
        if not cleaned_data.get(field_name):
            self.add_error(field_name, f"{label} is required for proforma invoices.")
```

to:

```python
if invoice_type in (Invoice.Type.PROFORMA, Invoice.Type.REGULAR):
    required_fields = {
        "proforma_make": "Make",
        "proforma_model": "Model",
        "proforma_price": "Total Cost",
    }
    for field_name, label in required_fields.items():
        if not cleaned_data.get(field_name):
            self.add_error(field_name, f"{label} is required for this invoice type.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python manage.py test core.tests.InvoiceFormCleanTests -v 2`

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/forms.py core/tests.py
git commit -m "Require vehicle/price fields for REGULAR invoices in form validation"
```

---

## Task 8: Form template — show vehicle fields for Regular

**Files:**
- Modify: `templates/invoices/form.html`

- [ ] **Step 1: Update the visibility attribute**

Open `templates/invoices/form.html`, line 133. Change:

```html
<div class="row g-4 mt-3" data-invoice-type-visible="PROFORMA">
```

to:

```html
<div class="row g-4 mt-3" data-invoice-type-visible="PROFORMA,REGULAR">
```

The existing JS at `form.html:407-417` already splits on comma and does case-insensitive matching. No JS change needed.

- [ ] **Step 2: Smoke-test manually (form page only — do not submit)**

Run: `venv/bin/python manage.py runserver` in one terminal.

In a browser, log in and open the **new-invoice form page** for any client (do NOT submit — the detail-page dispatcher for REGULAR isn't wired yet; that's Task 11). Verify:
- The type dropdown now includes "Regular" as an option.
- Selecting "Regular" shows the same vehicle fields block as "Proforma".
- Selecting "General" hides the vehicle block.
- Selecting "Proforma" still shows the vehicle block (regression check).

Stop the server.

- [ ] **Step 3: Commit**

```bash
git add templates/invoices/form.html
git commit -m "Show vehicle fields for Regular invoice type in form"
```

---

## Task 9: Create Regular PDF templates

**Files:**
- Create: `templates/invoices/_detail_document_regular.html`
- Create: `templates/invoices/detail_pdf_regular.html`

- [ ] **Step 1: Copy the Proforma partial**

Run: `cp templates/invoices/_detail_document_proforma.html templates/invoices/_detail_document_regular.html`

- [ ] **Step 2: Edit the new partial**

In `templates/invoices/_detail_document_regular.html`:

- Change the document title line (around line 24):

```html
<div class="document-title">PROFORMA INVOICE</div>
```

to:

```html
<div class="document-title">INVOICE</div>
```

- Add a GCT reg. line immediately after the existing email line (line 19, which reads `<div>Email: stepmathauto100@gmail.com</div>`):

```html
<div>Email: stepmathauto100@gmail.com</div>
<div>GCT REG. NO. 001-621-840</div>
```

- Replace any remaining user-visible `{{ invoice.pk }}` with `{{ invoice.invoice_number }}` in this file (grep the file — if none, skip).

Do **not** change `{% url 'invoice_update' invoice.pk %}`, `{% url 'invoice_pdf' invoice.pk %}`, or `{% url 'invoice_delete' invoice.pk %}` references (the template includes all three at approximately lines 115–119). Those are database-lookup args and must stay on `pk`.

Do **not** rename the `proforma` CSS classes — they only scope styles within the template and are not user-visible.

- [ ] **Step 3: Copy the Proforma PDF wrapper**

Run: `cp templates/invoices/detail_pdf_proforma.html templates/invoices/detail_pdf_regular.html`

- [ ] **Step 4: Edit the new PDF wrapper**

In `templates/invoices/detail_pdf_regular.html`:

- Change the title tag:

```html
<title>Proforma Invoice {{ invoice.pk }}</title>
```

to:

```html
<title>Invoice {{ invoice.invoice_number }}</title>
```

- Change the include:

```html
{% include 'invoices/_detail_document_proforma.html' with for_pdf=True %}
```

to:

```html
{% include 'invoices/_detail_document_regular.html' with for_pdf=True %}
```

- [ ] **Step 5: Render a Regular PDF end-to-end**

Run in shell:

```
venv/bin/python manage.py shell <<'EOF'
from core.models import Client, Invoice
from django.utils import timezone
c, _ = Client.objects.get_or_create(name="Regular PDF Test", defaults={"email": "t@example.com"})
inv = Invoice.objects.create(client=c, invoice_type=Invoice.Type.REGULAR, date=timezone.localdate(),
                             proforma_make="Toyota", proforma_model="Corolla", proforma_year=2015,
                             proforma_price=500000, proforma_currency="JMD")
filename, pdf = inv.generate_pdf_bytes(overwrite=False, store_local=False)
print("filename:", filename)
print("size:", len(pdf))
print("number:", inv.invoice_number)
EOF
```

Expected: `filename` is `invoice-2000-regular.pdf` (or the next Regular number), `size` is several kilobytes, `number` is ≥ 2000.

Open the PDF manually (save `pdf` bytes to a file if you want to inspect visually) and confirm:
- Header says "INVOICE" (not "PROFORMA INVOICE").
- "GCT REG. NO. 001-621-840" appears under the email line in the letterhead.

- [ ] **Step 6: Clean up the test invoice**

```
venv/bin/python manage.py shell -c "from core.models import Invoice; Invoice.objects.filter(client__name='Regular PDF Test').delete()"
```

- [ ] **Step 7: Commit**

```bash
git add templates/invoices/_detail_document_regular.html templates/invoices/detail_pdf_regular.html
git commit -m "Add Regular invoice PDF templates with INVOICE header and GCT reg. line"
```

---

## Task 10: Update user-visible `pk` references to `invoice_number`

**Files:**
- Modify: `templates/dashboard.html` (line 282)
- Modify: `templates/clients/detail.html` (line 117)
- Modify: `templates/emails/invoice_email.txt` (line 3)
- Modify: `templates/invoices/detail_pdf.html` (title)
- Modify: `templates/invoices/detail_pdf_proforma.html` (title)
- Modify: `templates/invoices/_detail_document_general.html` (any visible number)
- Modify: `templates/invoices/_detail_document_proforma.html` (any visible number)
- Modify: `core/views.py` (line 550)

- [ ] **Step 1: Update dashboard**

`templates/dashboard.html` line 282:

```html
Invoice #{{ inv.pk }}
```

→

```html
Invoice #{{ inv.invoice_number }}
```

- [ ] **Step 2: Update client detail**

`templates/clients/detail.html` line 117:

```html
Invoice #{{ inv.pk }}
```

→

```html
Invoice #{{ inv.invoice_number }}
```

- [ ] **Step 3: Update email body**

`templates/emails/invoice_email.txt` line 3:

```
Please find your invoice (#{% if invoice.pk %}{{ invoice.pk }}{% endif %}) attached to this email.
```

→

```
Please find your invoice (#{% if invoice.invoice_number %}{{ invoice.invoice_number }}{% endif %}) attached to this email.
```

- [ ] **Step 4: Update PDF title tags**

`templates/invoices/detail_pdf.html`: change `{{ invoice.pk }}` in `<title>` to `{{ invoice.invoice_number }}`.

`templates/invoices/detail_pdf_proforma.html`: same.

- [ ] **Step 5: Audit the document partials for visible number display**

Search the two files for any customer-visible pk reference:

```
grep -n 'invoice\.pk\|inv\.pk' templates/invoices/_detail_document_general.html templates/invoices/_detail_document_proforma.html
```

For each match that renders a customer-visible number (e.g., "Invoice #{{ invoice.pk }}"), replace `invoice.pk` with `invoice.invoice_number`. Do NOT change `{% url ... invoice.pk %}` references — those are database routing and must stay on `pk`.

- [ ] **Step 6: Update the email subject in views.py**

`core/views.py` line 550:

```python
subject = f"Invoice #{invoice.pk}"
```

→

```python
subject = f"Invoice #{invoice.invoice_number}"
```

- [ ] **Step 7: Run the full test suite**

Run: `venv/bin/python manage.py test core -v 1`

Expected: all tests pass. (If an existing test asserts against a string containing `pk`, it will need updating — note and fix.)

- [ ] **Step 8: Smoke-test dashboard and client detail pages in browser**

Run: `venv/bin/python manage.py runserver`. Visit the dashboard and a client detail page; verify invoice numbers still display correctly (for existing invoices they'll equal the old `pk` value after backfill). Stop the server.

- [ ] **Step 9: Commit**

```bash
git add templates/ core/views.py
git commit -m "Display invoice_number (not pk) in user-visible invoice references"
```

---

## Task 11: Detail-page partial dispatch for Regular

**Files:**
- Read first: `templates/invoices/detail.html`, `templates/invoices/_detail_document.html`

- [ ] **Step 1: Inspect how the detail page currently picks a partial**

Run: `cat templates/invoices/_detail_document.html`

The current dispatch may live inside `_detail_document.html` (which `detail.html` includes unconditionally). Understand the existing pattern before editing.

- [ ] **Step 2: Extend dispatch to include Regular**

Wherever the file switches between `_detail_document_general.html` and `_detail_document_proforma.html`, add a branch for `Invoice.Type.REGULAR` that includes `_detail_document_regular.html`.

Example pattern (adapt to the actual file):

```django
{% if invoice.invoice_type == 'GENERAL' %}
  {% include 'invoices/_detail_document_general.html' %}
{% elif invoice.invoice_type == 'PROFORMA' %}
  {% include 'invoices/_detail_document_proforma.html' %}
{% elif invoice.invoice_type == 'REGULAR' %}
  {% include 'invoices/_detail_document_regular.html' %}
{% endif %}
```

- [ ] **Step 3: Smoke-test in browser**

Run: `venv/bin/python manage.py runserver`. Create a Regular invoice via the UI, visit its detail page, and confirm the on-screen document renders with "INVOICE" header and the GCT reg. line.

Also regression-check: visit an existing General and an existing Proforma invoice's detail page — both should still render correctly.

Stop the server.

- [ ] **Step 4: Commit**

```bash
git add templates/invoices/
git commit -m "Render Regular invoice partial on detail page"
```

---

## Task 12: End-to-end verification

**Files:**
- None modified.

- [ ] **Step 1: Full test suite**

Run: `venv/bin/python manage.py test core -v 2`

Expected: all tests pass, including all new tests.

- [ ] **Step 2: Migration check**

Run: `venv/bin/python manage.py makemigrations --check --dry-run`

Expected: `No changes detected`. Confirms no pending model/migration drift.

- [ ] **Step 3: Manual browser smoke test**

Run: `venv/bin/python manage.py runserver`. As a logged-in user, run through the full flow:

1. Create a **Regular** invoice for a client — expect its number to be ≥ 2000.
2. Create a **General** invoice — expect its number to continue from the existing max (e.g., 17).
3. Create a **Proforma** invoice — expect its number to continue the shared General/Proforma sequence (e.g., 18).
4. Download the Regular invoice PDF — confirm "INVOICE" title, GCT reg. line, correct invoice number.
5. Edit the Regular invoice, change type to Proforma, save — number stays ≥ 2000 (permanent).
6. Email a Regular invoice (if Google is connected) — subject line reads `Invoice #<regular_number>`.

Stop the server.

- [ ] **Step 4: Sanity-check the database**

Run in shell:

```
venv/bin/python manage.py shell -c "from core.models import Invoice; print(sorted((i.invoice_type, i.invoice_number) for i in Invoice.objects.all()))"
```

Expected: no duplicate `invoice_number` values; Regular numbers are ≥ 2000; General/Proforma numbers are below 2000 (unless they've actually caught up, which they shouldn't have in manual testing).

- [ ] **Step 5: Final commit (if any fixups were needed) and done**

If the smoke test revealed issues, fix and commit. Otherwise, the implementation is complete.

---

## Rollback notes

If anything goes wrong mid-deploy:

- Migrations 0007, 0008, 0009 are forward-safe but 0008's `RunPython.noop` reverse means rolling back to 0007 does NOT clear the backfilled numbers. To fully roll back, run `python manage.py migrate core 0006` after verifying no production writes depend on `invoice_number`. The `invoice_number` column will be dropped.
- Code changes can be reverted in a single revert commit since each task is an isolated commit.
