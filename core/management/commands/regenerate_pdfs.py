from django.core.management.base import BaseCommand
from core.models import Invoice


class Command(BaseCommand):
    help = 'Regenerate PDF files for all invoices to include signature'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually regenerating PDFs',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        invoices = Invoice.objects.all()
        total_count = invoices.count()

        self.stdout.write(f"Found {total_count} invoices to process")

        processed = 0
        regenerated = 0

        for invoice in invoices:
            processed += 1

            if dry_run:
                self.stdout.write(f"[DRY RUN] Would regenerate PDF for invoice {invoice.pk} ({invoice.invoice_type})")
                regenerated += 1
                continue

            try:
                # Force regeneration of PDF
                if invoice.invoice_type == Invoice.Type.GENERAL:
                    invoice.generate_general_pdf(overwrite=True, store_local=True)
                else:
                    invoice.generate_proforma_pdf(overwrite=True, store_local=True)

                regenerated += 1
                self.stdout.write(
                    self.style.SUCCESS(f"✓ Regenerated PDF for invoice {invoice.pk} ({invoice.invoice_type})")
                )

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"✗ Failed to regenerate PDF for invoice {invoice.pk}: {e}")
                )

        self.stdout.write(f"\nProcessed {processed} invoices, regenerated {regenerated} PDFs")
        if dry_run:
            self.stdout.write("This was a dry run - no actual changes made")
