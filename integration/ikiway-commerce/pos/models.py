from django.db import models

from common.models import TimeStampedModel
from orders.models import Order
from tenancy.models import Branch


def default_cash_quick_amounts():
    return [1000, 2000, 5000, 10000, 20000]


def default_quick_access_variant_ids():
    return []


class PosTicket(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="pos_ticket")
    ticket_number = models.CharField(max_length=40, unique=True)
    receipt_payload = models.JSONField(default=dict, blank=True)


class PosConfiguration(TimeStampedModel):
    class ReceiptPaperWidth(models.TextChoices):
        MM_80 = "80", "Impresora 80 mm"
        MM_58 = "58", "Impresora 58 mm"

    class PrintMethod(models.TextChoices):
        BROWSER = "browser", "Navegador"
        AGENT = "agent", "Agente USB Ikiway"

    print_method = models.CharField(max_length=16, choices=PrintMethod.choices, default=PrintMethod.BROWSER)
    branch = models.OneToOneField(Branch, on_delete=models.CASCADE, related_name="pos_configuration")
    cash_quick_amounts = models.JSONField(default=default_cash_quick_amounts, blank=True)
    quick_access_variant_ids = models.JSONField(default=default_quick_access_variant_ids, blank=True)
    receipt_paper_width = models.CharField(max_length=2, choices=ReceiptPaperWidth.choices, default=ReceiptPaperWidth.MM_80)
    primary_color = models.CharField(max_length=16, default="#2457f5")
    secondary_color = models.CharField(max_length=16, default="#2457f5")
    accent_color = models.CharField(max_length=16, default="#16a34a")
    allow_cash_opening = models.BooleanField(default=True)
    show_product_images = models.BooleanField(default=True)
    require_supervisor_close_code = models.BooleanField(default=True)
    supervisor_close_code = models.CharField(max_length=4, default="1234")
    require_supervisor_return_code = models.BooleanField(default=True)
    supervisor_return_code = models.CharField(max_length=4, default="1234")
    require_supervisor_discount_code = models.BooleanField(default=True)
    supervisor_discount_code = models.CharField(max_length=4, default="1234")
    supervisor_discount_threshold_percent = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    ticket_header = models.CharField(max_length=160, blank=True)
    ticket_footer = models.CharField(max_length=220, blank=True)
    printer_name = models.CharField(max_length=120, blank=True)
    print_copies = models.PositiveSmallIntegerField(default=1)
    auto_print_receipt = models.BooleanField(default=False)
    show_change_on_receipt = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Configuracion POS"
        verbose_name_plural = "Configuraciones POS"

    def __str__(self):
        return f"Configuracion POS - {self.branch}"

    def get_cash_quick_amounts(self):
        amounts = []
        for amount in self.cash_quick_amounts or []:
            try:
                normalized = int(amount)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                amounts.append(normalized)
        return amounts or default_cash_quick_amounts()
