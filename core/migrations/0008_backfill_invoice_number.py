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
