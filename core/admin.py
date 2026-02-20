from django.contrib import admin
from .models import Client, Invoice, InvoiceItem


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 1


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'invoice_type', 'date', 'vehicle', 'total_display')
    list_filter = ('invoice_type', 'date')
    search_fields = ('client__name', 'vehicle', 'lic_no', 'chassis_no', 'engine_no')
    inlines = [InvoiceItemInline]

    def total_display(self, obj):
        if obj.invoice_type == Invoice.Type.PROFORMA:
            return obj.proforma_total_formatted or "—"
        return f"${obj.total:,.2f}"
    total_display.short_description = 'Total'


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'phone')
    search_fields = ('name', 'email', 'phone')


@admin.register(InvoiceItem)
class InvoiceItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'invoice', 'description', 'labour_cost', 'parts_cost')
    search_fields = ('description',)
