from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cashier.models import CashMovement, CashSession
from catalog.models import Product, ProductVariant
from inventory.models import InventoryMovement, StockItem
from orders.models import Order, OrderLine
from payments.models import Payment
from pos.models import PosConfiguration, PosTicket
from sii.models import DteStatus, DteType, SiiCompanySettings, SiiDocument


class PosTerminalTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())
        self.user = get_user_model().objects.get(email="vendedor@ikiway.cl")
        self.client.force_login(self.user)
        self.stock_item = StockItem.objects.select_related("variant").get(variant__sku="TEC-CABLE-USBC-1M", branch__code="STORE-001")

    def test_terminal_loads_with_open_cash_session(self):
        response = self.client.get(reverse("pos:terminal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TPV Ikiway")
        self.assertContains(response, "Cerrar caja")
        self.assertNotContains(response, "Volver al panel")
        self.assertContains(response, self.stock_item.variant.product.name)
        self.assertContains(response, "$1.000")
        self.assertContains(response, "$20.000")
        self.assertContains(response, f'form="product-add-{self.stock_item.id}"')
        self.assertContains(response, 'class="product-media product-media-action"')
        self.assertContains(response, 'data-checkout-discounts aria-label="Descuento general de venta">')
        self.assertNotContains(response, 'data-checkout-discounts aria-label="Descuento general de venta" open')
        self.assertContains(response, 'data-confirm-payment data-checkout-action="sale"')
        self.assertContains(response, 'data-confirm-payment data-checkout-action="print"')

    def test_variable_product_selector_uses_variant_image_and_adds_selected_stock(self):
        product = Product.objects.create(
            company=self.stock_item.variant.product.company,
            name="Polera TPV",
            slug="polera-tpv",
            product_type=Product.TYPE_VARIABLE,
            is_sellable_pos=True,
            is_sellable_online=False,
        )
        red = ProductVariant.objects.create(
            product=product,
            name="Roja",
            attributes={"Color": "Rojo"},
            sku="POL-TPV-ROJA",
            sale_price=Decimal("10000"),
            image="product_variant_images/polera-roja.png",
        )
        green = ProductVariant.objects.create(
            product=product,
            name="Verde",
            attributes={"Color": "Verde"},
            sku="POL-TPV-VERDE",
            sale_price=Decimal("11000"),
            image="product_variant_images/polera-verde.png",
        )
        red_stock = StockItem.objects.create(
            branch=self.stock_item.branch,
            variant=red,
            quantity=Decimal("3"),
            minimum_quantity=Decimal("1"),
        )
        green_stock = StockItem.objects.create(
            branch=self.stock_item.branch,
            variant=green,
            quantity=Decimal("5"),
            minimum_quantity=Decimal("1"),
        )

        response = self.client.get(reverse("pos:terminal"))

        self.assertEqual(response.status_code, 200)
        group = next(item for item in response.context["products"] if item.product.id == product.id)
        self.assertEqual(group.variant_count, 2)
        self.assertEqual({item.id for item in group.variants}, {red_stock.id, green_stock.id})
        self.assertContains(response, 'data-variant-select')
        self.assertContains(response, red.image.url)
        self.assertContains(response, green.image.url)
        self.assertContains(response, f'form="product-add-{red_stock.id}"')

        add_response = self.client.post(
            reverse("pos:terminal"),
            {"action": "add", "stock_item_id": green_stock.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(add_response.status_code, 200)
        self.assertTrue(add_response.json()["ok"])
        self.assertIn(str(green.id), self.client.session["pos_cart"])
        self.assertNotIn(str(red.id), self.client.session["pos_cart"])
        self.assertIn(green.image.url, add_response.json()["cart_html"])
        self.assertIn("Verde", add_response.json()["cart_html"])

        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.show_product_images = False
        config.save(update_fields=["show_product_images", "updated_at"])
        hidden_response = self.client.get(reverse("pos:terminal"))
        self.assertNotContains(hidden_response, red.image.url)
        self.assertNotContains(hidden_response, green.image.url)
        hidden_add_response = self.client.post(
            reverse("pos:terminal"),
            {"action": "add", "stock_item_id": green_stock.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertNotIn(green.image.url, hidden_add_response.json()["cart_html"])
    def test_terminal_can_open_cash_session_when_none_is_open(self):
        CashSession.objects.update(status=CashSession.Status.CLOSED)

        response = self.client.get(reverse("pos:terminal"))
        self.assertContains(response, "Abrir caja")
        self.assertContains(response, "No hay caja abierta")

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "open_cash",
                "branch_id": self.stock_item.branch_id,
                "opening_amount": "25000",
                "notes": "Apertura de prueba",
            },
            follow=True,
        )

        self.assertContains(response, "Caja abierta")
        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)
        self.assertEqual(session.opening_amount, Decimal("25000"))
        self.assertEqual(session.expected_cash_amount, Decimal("25000"))
        self.assertEqual(session.notes, "Apertura de prueba")

    def test_admin_user_sees_return_to_admin_button(self):
        admin = get_user_model().objects.get(email="admin@ikiway.cl")
        self.client.force_login(admin)

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, "Volver al panel")
        self.assertContains(response, 'href="/dashboard/"')

    def test_can_close_cash_session_from_pos(self):
        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "close_cash", "counted_cash_amount": str(session.expected_cash_amount), "close_notes": "Cierre de turno"},
            follow=True,
        )

        self.assertContains(response, "Caja cerrada sin diferencias")
        session.refresh_from_db()
        self.assertEqual(session.status, CashSession.Status.CLOSED)
        self.assertEqual(session.closed_by, self.user)
        self.assertEqual(session.counted_cash_amount, session.expected_cash_amount)
        self.assertEqual(session.closing_difference_amount, Decimal("0"))
        self.assertEqual(session.closing_difference_type, CashSession.DifferenceType.BALANCED)
        self.assertFalse(session.supervisor_override_required)
        self.assertEqual(session.notes, "Cierre de turno")

    def test_close_cash_with_shortage_requires_supervisor_code(self):
        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)
        counted = session.expected_cash_amount - Decimal("1000")

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "close_cash", "counted_cash_amount": str(counted)},
            follow=True,
        )

        self.assertContains(response, "Se requiere codigo supervisor")
        session.refresh_from_db()
        self.assertEqual(session.status, CashSession.Status.OPEN)
        self.assertIsNone(session.counted_cash_amount)

    def test_ajax_close_cash_wrong_supervisor_code_keeps_session_open(self):
        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)
        config, _created = PosConfiguration.objects.get_or_create(branch=session.branch)
        config.supervisor_close_code = "9876"
        config.save(update_fields=["supervisor_close_code", "updated_at"])
        counted = session.expected_cash_amount - Decimal("1000")

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "close_cash",
                "counted_cash_amount": str(counted),
                "supervisor_close_code": "1111",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("codigo supervisor" in message["text"] for message in payload["messages"]))
        session.refresh_from_db()
        self.assertEqual(session.status, CashSession.Status.OPEN)
        self.assertIsNone(session.counted_cash_amount)

    def test_close_cash_difference_can_skip_code_when_disabled(self):
        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)
        config, _created = PosConfiguration.objects.get_or_create(branch=session.branch)
        config.require_supervisor_close_code = False
        config.save(update_fields=["require_supervisor_close_code", "updated_at"])
        counted = session.expected_cash_amount - Decimal("1000")

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "close_cash", "counted_cash_amount": str(counted)},
            follow=True,
        )

        self.assertContains(response, "Caja cerrada con diferencia")
        session.refresh_from_db()
        self.assertEqual(session.status, CashSession.Status.CLOSED)
        self.assertEqual(session.closing_difference_amount, Decimal("-1000"))
        self.assertEqual(session.closing_difference_type, CashSession.DifferenceType.SHORT)
        self.assertFalse(session.supervisor_override_required)
        self.assertFalse(session.supervisor_override_approved)

    def test_close_cash_with_supervisor_code_records_difference(self):
        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)
        config, _created = PosConfiguration.objects.get_or_create(branch=session.branch)
        config.supervisor_close_code = "9876"
        config.save(update_fields=["supervisor_close_code", "updated_at"])
        counted = session.expected_cash_amount + Decimal("1500")

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "close_cash",
                "counted_cash_amount": str(counted),
                "supervisor_close_code": "9876",
            },
            follow=True,
        )

        self.assertContains(response, "Caja cerrada con autorizacion supervisor")
        session.refresh_from_db()
        self.assertEqual(session.status, CashSession.Status.CLOSED)
        self.assertEqual(session.counted_cash_amount, counted)
        self.assertEqual(session.closing_difference_amount, Decimal("1500"))
        self.assertEqual(session.closing_difference_type, CashSession.DifferenceType.OVER)
        self.assertTrue(session.supervisor_override_required)
        self.assertTrue(session.supervisor_override_approved)
        self.assertEqual(session.supervisor_override_by, self.user)
        self.assertIsNotNone(session.supervisor_override_at)

    def test_pos_logout_requires_closed_cash_session(self):
        response = self.client.post(reverse("pos:logout"), follow=True)

        self.assertContains(response, "Debes cerrar y cuadrar la caja")
        self.assertIn("_auth_user_id", self.client.session)

        session = CashSession.objects.get(opened_by=self.user, status=CashSession.Status.OPEN)
        session.status = CashSession.Status.CLOSED
        session.closed_by = self.user
        session.closed_at = session.opened_at
        session.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])

        response = self.client.post(reverse("pos:logout"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_close_cash_modal_shows_summary_and_transaction_history(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CASH, Payment.Method.CARD],
                "amount_received_cash": "1000",
                "amount_received_card": "4990",
                "tax_document_choice": "none",
            },
        )

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, "Resumen de cierre")
        self.assertContains(response, "Transacciones")
        self.assertContains(response, "Total vendido")
        self.assertContains(response, "Medios de pago")
        self.assertContains(response, "Ver historial de transacciones")
        self.assertContains(response, self.stock_item.variant.product.name)
        summary = response.context["close_cash_summary"]
        self.assertEqual(summary["sales_count"], 1)
        self.assertEqual(summary["sales_total"], Decimal("5990"))
        payments = {row["method"]: row for row in summary["payment_rows"]}
        self.assertEqual(payments[Payment.Method.CASH]["total"], Decimal("1000"))
        self.assertEqual(payments[Payment.Method.CARD]["total"], Decimal("4990"))

    def test_terminal_uses_branch_pos_cash_quick_amounts(self):
        PosConfiguration.objects.create(branch=self.stock_item.branch, cash_quick_amounts=[3000, 7000])

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, "$3.000")
        self.assertContains(response, "$7.000")
        self.assertNotContains(response, "$20.000")

    def test_terminal_uses_configured_gradient_colors(self):
        PosConfiguration.objects.create(
            branch=self.stock_item.branch,
            primary_color="#fa00b7",
            secondary_color="#fa00b7",
            accent_color="#16a34a",
        )

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, "--brand: #fa00b7")
        self.assertContains(response, "--brand-2: #fa00b7")

    def test_terminal_shows_integer_stock_and_stock_alerts(self):
        self.stock_item.quantity = Decimal("5.000")
        self.stock_item.save(update_fields=["quantity", "updated_at"])

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, "Stock bajo")
        self.assertContains(response, "<strong>5</strong>", html=True)
        self.assertNotContains(response, "<strong>5.000</strong>", html=True)

        self.stock_item.quantity = Decimal("0.000")
        self.stock_item.save(update_fields=["quantity", "updated_at"])

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, "Sin stock")
        self.assertContains(response, "disabled")

    def test_latest_sales_section_shows_last_pos_sale(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )

        response = self.client.get(f"{reverse('pos:terminal')}?view=sales")
        order = Order.objects.get(channel=Order.Channel.POS)

        self.assertContains(response, "Ultimas ventas")
        self.assertContains(response, "Listado de las ventas mas recientes")
        self.assertContains(response, f"#{order.id}")
        self.assertContains(response, "Productos")
        self.assertContains(response, self.stock_item.variant.product.name)
        self.assertContains(response, "Reimprimir ticket")
        self.assertContains(response, "Registrar devolucion")

    def test_quick_access_view_orders_favorite_best_sellers_first(self):
        other_item = (
            StockItem.objects.select_related("variant", "variant__product")
            .exclude(id=self.stock_item.id)
            .filter(branch=self.stock_item.branch, variant__product__is_sellable_pos=True)
            .first()
        )
        Product.objects.filter(id__in=[self.stock_item.variant.product_id, other_item.variant.product_id]).update(is_quick_access=True)
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(reverse("pos:terminal"), {"action": "checkout", "payment_methods": [Payment.Method.CARD], "amount_received_card": str(self.stock_item.variant.sale_price), "tax_document_choice": "none"})
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": other_item.id})
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": other_item.id})
        self.client.post(reverse("pos:terminal"), {"action": "checkout", "payment_methods": [Payment.Method.CARD], "amount_received_card": str(other_item.variant.sale_price * 2), "tax_document_choice": "none"})

        response = self.client.get(f"{reverse('pos:terminal')}?view=quick")

        self.assertContains(response, "Favoritos")
        self.assertEqual(response.context["quick_access_items"][0].variant_id, other_item.variant_id)
        self.assertEqual(response.context["quick_access_items"][1].variant_id, self.stock_item.variant_id)

    def test_quick_access_uses_catalog_favorites_instead_of_pos_configuration(self):
        other_item = (
            StockItem.objects.select_related("variant", "variant__product")
            .exclude(id=self.stock_item.id)
            .filter(branch=self.stock_item.branch, variant__product__is_sellable_pos=True)
            .first()
        )
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.quick_access_variant_ids = [other_item.variant_id]
        config.save(update_fields=["quick_access_variant_ids", "updated_at"])
        self.stock_item.variant.product.is_quick_access = True
        self.stock_item.variant.product.save(update_fields=["is_quick_access", "updated_at"])

        response = self.client.get(f"{reverse('pos:terminal')}?view=quick")

        self.assertEqual(response.context["quick_access_items"][0].variant_id, self.stock_item.variant_id)
        self.assertNotContains(response, other_item.variant.product.name)

    def test_current_cash_summary_shows_day_sales_by_payment_method(self):
        self.client.post(reverse('pos:terminal'), {'action': 'add', 'stock_item_id': self.stock_item.id})
        self.client.post(
            reverse('pos:terminal'),
            {
                'action': 'checkout',
                'payment_methods': [Payment.Method.CASH, Payment.Method.CARD],
                'amount_received_cash': '1000',
                'amount_received_card': '4990',
            },
        )

        response = self.client.get(reverse('pos:terminal'))

        self.assertContains(response, 'Caja actual')
        self.assertContains(response, 'Ventas del dia')
        self.assertContains(response, 'Total vendido')
        self.assertContains(response, 'Deberia haber en caja')
        self.assertContains(response, 'Efectivo')
        self.assertContains(response, 'Tarjeta')
        summary = response.context['cash_summary']
        self.assertEqual(summary['sales_count'], 1)
        self.assertEqual(summary['sales_total'], Decimal('5990'))
        self.assertEqual(summary['expected_cash_amount'], Decimal('51000'))
        payments = {row['method']: row for row in summary['payment_rows']}
        self.assertEqual(payments[Payment.Method.CASH]['total'], Decimal('1000'))
        self.assertEqual(payments[Payment.Method.CARD]['total'], Decimal('4990'))

    def test_ticket_view_renders_printable_receipt(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)

        response = self.client.get(reverse("pos:ticket", args=[order.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Imprimir boleta")
        self.assertContains(response, order.pos_ticket.ticket_number)

        print_response = self.client.get(f"{reverse('pos:ticket', args=[order.id])}?print=1")
        self.assertContains(print_response, 'onload="window.print()"')

    def test_ticket_uses_electronic_document_header_and_ted_pdf417(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CASH],
                "amount_received_cash": "20000",
                "tax_document_choice": "none",
            },
        )
        order = Order.objects.get(channel=Order.Channel.POS)
        SiiCompanySettings.objects.update_or_create(
            company=order.company,
            defaults={
                "legal_name": "IKIWAY SPA",
                "rut": "78315818-3",
                "business_activity": "Venta al por menor",
                "address": "Bulnes 136",
                "comuna": "Talcahuano",
                "city": "Talcahuano",
                "email": "pos@ikiway.cl",
                "economic_activity_code": "479100",
                "sii_resolution_number": "80",
                "sii_resolution_date": "2026-08-13",
                "is_enabled": True,
            },
        )
        signed_xml = (
            '<DTE><Documento><TED version="1.0"><DD><RE>78315818-3</RE><TD>39</TD>'
            '<F>321</F><FE>2026-09-08</FE><RR>66666666-6</RR><RSR>Cliente</RSR>'
            '<MNT>5990</MNT><IT1>Producto</IT1></DD><FRMT algoritmo="SHA1withRSA">firma</FRMT>'
            '</TED></Documento></DTE>'
        )
        SiiDocument.objects.create(
            company=order.company,
            branch=order.branch,
            order=order,
            document_type=DteType.BOLETA_ELECTRONICA,
            status=DteStatus.SIGNED,
            folio=321,
            issued_at=timezone.now(),
            xml_signed=signed_xml,
        )

        response = self.client.get(reverse("pos:ticket", args=[order.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IKIWAY SPA")
        self.assertContains(response, "RUT: 78315818-3")
        self.assertContains(response, "Giro: Venta al por menor")
        self.assertContains(response, "Direccion: Bulnes 136, Talcahuano")
        self.assertContains(response, "Boleta electronica afecta")
        self.assertContains(response, "Folio 321")
        self.assertContains(response, 'src="data:image/png;base64,', html=False)
        self.assertContains(response, "Timbre electronico S.I.I.")

        config, _created = PosConfiguration.objects.get_or_create(branch=order.branch)
        config.print_method = PosConfiguration.PrintMethod.AGENT
        config.save(update_fields=["print_method", "updated_at"])
        agent_response = self.client.get(f"{reverse('pos:ticket', args=[order.id])}?print=1")

        self.assertEqual(agent_response.status_code, 200)
        self.assertContains(agent_response, "Imprimir boleta por USB")
        self.assertContains(agent_response, "ikiway.receipt.v1")
        self.assertContains(agent_response, "ikiway-printer.js")
        self.assertNotContains(agent_response, 'onload="window.print()"')
        printer_payload = agent_response.context["printer_payload"]
        self.assertEqual(printer_payload["electronic_document"]["folio"], 321)
        self.assertTrue(printer_payload["electronic_document"]["pdf417_data_uri"].startswith("data:image/png;base64,"))

    def test_agent_refuses_electronic_document_without_ted(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)
        SiiDocument.objects.create(
            company=order.company,
            branch=order.branch,
            order=order,
            document_type=DteType.BOLETA_ELECTRONICA,
            status=DteStatus.SIGNED,
            folio=99,
            issued_at=timezone.now(),
        )
        config, _created = PosConfiguration.objects.get_or_create(branch=order.branch)
        config.print_method = PosConfiguration.PrintMethod.AGENT
        config.save(update_fields=["print_method", "updated_at"])

        response = self.client.get(f"{reverse('pos:ticket', args=[order.id])}?print=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "El DTE no tiene TED disponible")
        self.assertNotContains(response, "ikiway-printer.js")

    def test_can_add_product_to_cart_and_checkout_cash_sale(self):
        starting_quantity = self.stock_item.quantity

        add_response = self.client.post(
            reverse("pos:terminal"),
            {"action": "add", "stock_item_id": self.stock_item.id},
            follow=True,
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertContains(add_response, "Finalizar venta")

        checkout_response = self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_method": Payment.Method.CASH, "amount_received": "20000"},
            follow=True,
        )

        self.assertEqual(checkout_response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.total, self.stock_item.variant.sale_price)
        self.assertEqual(OrderLine.objects.filter(order=order).count(), 1)
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.amount, order.total)
        self.assertEqual(payment.raw_response["amount_received"], "20000")
        self.assertEqual(Decimal(payment.raw_response["change_due"]), Decimal("20000") - order.total)
        self.assertTrue(PosTicket.objects.filter(order=order).exists())
        self.assertTrue(CashMovement.objects.filter(session=order.cash_session, amount=order.total).exists())
        self.assertTrue(
            InventoryMovement.objects.filter(
                stock_item=self.stock_item,
                movement_type=InventoryMovement.MovementType.SALE,
                quantity=Decimal("-1.000"),
            ).exists()
        )

        self.stock_item.refresh_from_db()
        self.assertEqual(self.stock_item.quantity, starting_quantity - Decimal("1"))

    def test_ajax_add_product_updates_cart_and_payment_modal(self):
        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "add", "stock_item_id": self.stock_item.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cart_count"], 1)
        self.assertIn("Carrito de ventas", payload["cart_html"])
        self.assertIn(self.stock_item.variant.product.name, payload["cart_html"])
        self.assertIn("Descuento general", payload["payment_html"])
        self.assertIn("sale_discount_type", payload["payment_html"])
        first_stock_update = next(item for item in payload["stock_updates"] if item["stock_item_id"] == self.stock_item.id)
        self.assertEqual(first_stock_update["quantity"], int(self.stock_item.quantity) - 1)

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "add", "stock_item_id": self.stock_item.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        second_stock_update = next(item for item in payload["stock_updates"] if item["stock_item_id"] == self.stock_item.id)
        self.assertEqual(second_stock_update["quantity"], int(self.stock_item.quantity) - 2)
        self.assertEqual(Decimal(self.client.session["pos_cart"][str(self.stock_item.variant_id)]["quantity"]), Decimal("2"))

    def test_ajax_checkout_returns_receipt_print_prompt_data(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CASH],
                "amount_received_cash": "20000",
                "tax_document_choice": "boleta",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        order = Order.objects.get(channel=Order.Channel.POS)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checkout_receipt"]["order_id"], order.id)
        self.assertEqual(payload["checkout_receipt"]["ticket_number"], order.pos_ticket.ticket_number)
        self.assertEqual(payload["checkout_receipt"]["document_label"], "Boleta electronica afecta")
        self.assertEqual(payload["checkout_receipt"]["ticket_url"], reverse("pos:ticket", args=[order.id]))
        self.assertEqual(payload["checkout_receipt"]["print_url"], f"{reverse('pos:ticket', args=[order.id])}?print=1")
        self.assertIn("El carrito esta vacio", payload["cart_html"])
        self.assertIn("Finalizar venta", payload["cart_html"])

    def test_ajax_cart_line_discount_updates_totals_and_payment_modal(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "discount_line",
                "cart_key": str(self.stock_item.variant_id),
                "line_discount_type": "amount",
                "line_discount_value": "990",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("-$990", payload["cart_html"])
        self.assertIn(f'data-cart-key="{self.stock_item.variant_id}"', payload["cart_html"])
        self.assertIn("$5.000", payload["cart_html"])
        self.assertIn("Descuento general", payload["payment_html"])
        self.assertIn("sale_discount_type", payload["payment_html"])
        self.assertIn('data-checkout-discounts aria-label="Descuento general de venta" open', payload["payment_html"])
        self.assertIn("Ya incluye $990 de descuento por producto", payload["payment_html"])
        session_line = self.client.session["pos_cart"][str(self.stock_item.variant_id)]
        self.assertEqual(session_line["discount_type"], "amount")
        self.assertEqual(session_line["discount_value"], "990")

    def test_checkout_rejects_cash_payment_below_total(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_method": Payment.Method.CASH, "amount_received": "100"},
            follow=True,
        )

        self.assertContains(response, "El monto recibido no cubre el total")
        self.assertFalse(Order.objects.exists())

    def test_void_sale_requires_supervisor_return_code(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "void_sale", "order_id": order.id, "return_reason": "Cliente devuelve producto."},
            follow=True,
        )

        self.assertContains(response, "Se requiere codigo supervisor")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_ajax_void_sale_wrong_supervisor_code_keeps_modal_retryable(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.supervisor_return_code = "9876"
        config.save(update_fields=["supervisor_return_code", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "void_sale",
                "order_id": order.id,
                "return_reason": "Cliente devuelve producto.",
                "supervisor_return_code": "1111",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("codigo supervisor" in message["text"] for message in payload["messages"]))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_void_sale_can_skip_code_when_disabled(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.require_supervisor_return_code = False
        config.save(update_fields=["require_supervisor_return_code", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "void_sale", "order_id": order.id, "return_reason": "Cliente devuelve producto."},
            follow=True,
        )

        self.assertContains(response, "Devolucion de venta")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_can_void_latest_paid_sale(self):
        starting_quantity = self.stock_item.quantity
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "void_sale",
                "order_id": order.id,
                "return_reason": "Cliente devuelve producto por error de compra.",
                "supervisor_return_code": "1234",
            },
            follow=True,
        )

        self.assertContains(response, "Devolucion de venta")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertIn("Cliente devuelve producto", order.notes)
        self.assertEqual(Payment.objects.get(order=order).status, Payment.Status.REFUNDED)
        self.stock_item.refresh_from_db()
        self.assertEqual(self.stock_item.quantity, starting_quantity)

    def test_void_sale_requires_return_reason(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_methods": [Payment.Method.CASH], "amount_received_cash": "20000"},
        )
        order = Order.objects.get(channel=Order.Channel.POS)

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "void_sale", "order_id": order.id},
            follow=True,
        )

        self.assertContains(response, "Debes ingresar un motivo")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_checkout_can_split_payment_between_cash_and_card(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        cash_amount = Decimal("1000")
        card_amount = self.stock_item.variant.sale_price - cash_amount

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CASH, Payment.Method.CARD],
                "amount_received_cash": str(cash_amount),
                "amount_received_card": str(card_amount),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        self.assertEqual(Payment.objects.filter(order=order).count(), 2)
        self.assertTrue(Payment.objects.filter(order=order, method=Payment.Method.CASH, amount=cash_amount).exists())
        self.assertTrue(Payment.objects.filter(order=order, method=Payment.Method.CARD, amount=card_amount).exists())
        self.assertTrue(CashMovement.objects.filter(session=order.cash_session, amount=cash_amount).exists())

    def test_can_sell_temporary_product_without_inventory_movement(self):
        movement_count = InventoryMovement.objects.count()

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "add_temp_product",
                "temp_name": "Servicio express",
                "temp_price": "3500",
                "temp_quantity": "2",
            },
            follow=True,
        )
        self.assertContains(response, "Servicio express")
        self.assertContains(response, "Temporal")

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CASH],
                "amount_received_cash": "7000",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        line = OrderLine.objects.get(order=order)
        self.assertEqual(order.total, Decimal("7000"))
        self.assertEqual(line.description, "Servicio express")
        self.assertEqual(line.quantity, Decimal("2.000"))
        self.assertEqual(InventoryMovement.objects.count(), movement_count)

    def test_checkout_applies_general_discount_to_sale_total(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "amount",
                "sale_discount_value": "990",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "5000",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        line = OrderLine.objects.get(order=order)
        payment = Payment.objects.get(order=order)
        self.assertEqual(order.subtotal, Decimal("5990"))
        self.assertEqual(order.discount_total, Decimal("990"))
        self.assertEqual(order.total, Decimal("5000"))
        self.assertEqual(line.discount_amount, Decimal("990"))
        self.assertEqual(line.line_total, Decimal("5000"))
        self.assertEqual(payment.amount, Decimal("5000"))

    def test_catalog_discount_price_is_used_and_blocks_extra_discount_by_default(self):
        self.stock_item.variant.pos_discount_price = Decimal("4990")
        self.stock_item.variant.allow_pos_discount_on_discount = False
        self.stock_item.variant.save(update_fields=["pos_discount_price", "allow_pos_discount_on_discount", "updated_at"])

        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "discount_line",
                "cart_key": str(self.stock_item.variant_id),
                "line_discount_type": "amount",
                "line_discount_value": "990",
            },
            follow=True,
        )
        self.assertContains(response, "no permite descuento adicional")

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "amount",
                "sale_discount_value": "990",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "4990",
                "tax_document_choice": "none",
            },
            follow=True,
        )
        self.assertContains(response, "No se puede aplicar descuento general")
        self.assertFalse(Order.objects.filter(channel=Order.Channel.POS).exists())

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "4990",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        line = OrderLine.objects.get(order=order)
        self.assertEqual(order.subtotal, Decimal("4990"))
        self.assertEqual(order.discount_total, Decimal("0"))
        self.assertEqual(order.total, Decimal("4990"))
        self.assertEqual(line.unit_price, Decimal("4990"))
        self.assertEqual(line.discount_amount, Decimal("0"))
        self.assertEqual(line.line_total, Decimal("4990"))

    def test_payment_modal_disables_general_discount_for_locked_catalog_discount(self):
        self.stock_item.variant.pos_discount_price = Decimal("4990")
        self.stock_item.variant.allow_pos_discount_on_discount = False
        self.stock_item.variant.save(update_fields=["pos_discount_price", "allow_pos_discount_on_discount", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.get(reverse("pos:terminal"))

        self.assertContains(response, 'data-discount-blocked-by-catalog="true"')
        self.assertContains(response, 'data-sale-discount-type disabled')
        self.assertContains(response, 'data-sale-discount-value disabled')
        self.assertContains(response, "No disponible porque el carrito contiene productos con oferta TPV bloqueada.")

    def test_general_discount_is_blocked_when_mixed_cart_has_catalog_discount(self):
        other_item = (
            StockItem.objects.select_related("variant")
            .exclude(id=self.stock_item.id)
            .filter(branch=self.stock_item.branch, variant__product__is_sellable_pos=True)
            .first()
        )
        self.stock_item.variant.pos_discount_price = Decimal("4990")
        self.stock_item.variant.allow_pos_discount_on_discount = False
        self.stock_item.variant.save(update_fields=["pos_discount_price", "allow_pos_discount_on_discount", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": other_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "amount",
                "sale_discount_value": "990",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "50000",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertContains(response, "No se puede aplicar descuento general")
        self.assertFalse(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_line_discount_sent_at_checkout_is_blocked_for_catalog_discount(self):
        self.stock_item.variant.pos_discount_price = Decimal("4990")
        self.stock_item.variant.allow_pos_discount_on_discount = False
        self.stock_item.variant.save(update_fields=["pos_discount_price", "allow_pos_discount_on_discount", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "discount_cart_key": [str(self.stock_item.variant_id)],
                "line_discount_type": ["amount"],
                "line_discount_value": ["990"],
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "4990",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertContains(response, "No se puede aplicar otro descuento")
        self.assertFalse(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_catalog_discount_price_can_allow_extra_discount(self):
        self.stock_item.variant.pos_discount_price = Decimal("4990")
        self.stock_item.variant.allow_pos_discount_on_discount = True
        self.stock_item.variant.save(update_fields=["pos_discount_price", "allow_pos_discount_on_discount", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "discount_cart_key": [str(self.stock_item.variant_id)],
                "line_discount_type": ["amount"],
                "line_discount_value": ["990"],
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "4000",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        line = OrderLine.objects.get(order=order)
        self.assertEqual(order.subtotal, Decimal("4990"))
        self.assertEqual(order.discount_total, Decimal("990"))
        self.assertEqual(order.total, Decimal("4000"))
        self.assertEqual(line.discount_amount, Decimal("990"))
        self.assertEqual(line.line_total, Decimal("4000"))

    def test_cart_discount_panel_stays_open_after_selecting_discount_type(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "discount_line",
                "cart_key": str(self.stock_item.variant_id),
                "line_discount_type": "percent",
                "line_discount_value": "0",
            },
            follow=True,
        )

        self.assertContains(
            response,
            f'<details class="line-discount-form" data-cart-key="{self.stock_item.variant_id}" open>',
            html=False,
        )

    def test_discount_under_supervisor_threshold_does_not_require_code(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.require_supervisor_discount_code = True
        config.supervisor_discount_code = "2468"
        config.supervisor_discount_threshold_percent = Decimal("50")
        config.save(update_fields=["require_supervisor_discount_code", "supervisor_discount_code", "supervisor_discount_threshold_percent", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "percent",
                "sale_discount_value": "40",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "3590",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_discount_at_supervisor_threshold_requires_code(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.require_supervisor_discount_code = True
        config.supervisor_discount_code = "2468"
        config.supervisor_discount_threshold_percent = Decimal("50")
        config.save(update_fields=["require_supervisor_discount_code", "supervisor_discount_code", "supervisor_discount_threshold_percent", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "percent",
                "sale_discount_value": "50",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "2990",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertContains(response, "requiere codigo supervisor")
        self.assertFalse(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_ajax_discount_wrong_supervisor_code_keeps_checkout_retryable(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.require_supervisor_discount_code = True
        config.supervisor_discount_code = "2468"
        config.supervisor_discount_threshold_percent = Decimal("50")
        config.save(update_fields=["require_supervisor_discount_code", "supervisor_discount_code", "supervisor_discount_threshold_percent", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "percent",
                "sale_discount_value": "50",
                "supervisor_discount_code": "1111",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "2990",
                "tax_document_choice": "none",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("Codigo supervisor incorrecto" in message["text"] for message in payload["messages"]))
        self.assertFalse(any("5E+1%" in message["text"] for message in payload["messages"]))
        self.assertFalse(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_discount_at_supervisor_threshold_accepts_correct_code(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.require_supervisor_discount_code = True
        config.supervisor_discount_code = "2468"
        config.supervisor_discount_threshold_percent = Decimal("50")
        config.save(update_fields=["require_supervisor_discount_code", "supervisor_discount_code", "supervisor_discount_threshold_percent", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "percent",
                "sale_discount_value": "50",
                "supervisor_discount_code": "2468",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "2990",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_discount_supervisor_code_can_be_disabled(self):
        config, _created = PosConfiguration.objects.get_or_create(branch=self.stock_item.branch)
        config.require_supervisor_discount_code = False
        config.supervisor_discount_threshold_percent = Decimal("50")
        config.save(update_fields=["require_supervisor_discount_code", "supervisor_discount_threshold_percent", "updated_at"])
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "sale_discount_type": "percent",
                "sale_discount_value": "50",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "2990",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Order.objects.filter(channel=Order.Channel.POS).exists())

    def test_checkout_keeps_cart_line_discount_when_finalizing_sale(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {
                "action": "discount_line",
                "cart_key": str(self.stock_item.variant_id),
                "line_discount_type": "amount",
                "line_discount_value": "990",
            },
        )

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "5000",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        line = OrderLine.objects.get(order=order)
        self.assertEqual(order.discount_total, Decimal("990"))
        self.assertEqual(order.total, Decimal("5000"))
        self.assertEqual(line.discount_amount, Decimal("990"))
        self.assertEqual(line.line_total, Decimal("5000"))

    def test_line_discount_does_not_affect_other_products(self):
        other_item = (
            StockItem.objects.select_related("variant")
            .exclude(id=self.stock_item.id)
            .filter(branch=self.stock_item.branch, variant__product__is_sellable_pos=True)
            .first()
        )
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": other_item.id})
        expected_total = self.stock_item.variant.sale_price + other_item.variant.sale_price - Decimal("990")

        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "discount_cart_key": [str(self.stock_item.variant_id), str(other_item.variant_id)],
                "line_discount_type": ["amount", "none"],
                "line_discount_value": ["990", "0"],
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": str(expected_total),
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        lines = {line.variant_id: line for line in OrderLine.objects.filter(order=order)}
        self.assertEqual(order.discount_total, Decimal("990"))
        self.assertEqual(lines[self.stock_item.variant_id].discount_amount, Decimal("990"))
        self.assertEqual(lines[other_item.variant_id].discount_amount, Decimal("0"))
        self.assertEqual(lines[other_item.variant_id].line_total, other_item.variant.sale_price)

    def test_line_discount_cannot_zero_product(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})
        self.client.post(
            reverse("pos:terminal"),
            {
                "action": "discount_line",
                "cart_key": str(self.stock_item.variant_id),
                "line_discount_type": "percent",
                "line_discount_value": "100",
            },
        )
        response = self.client.post(
            reverse("pos:terminal"),
            {
                "action": "checkout",
                "payment_methods": [Payment.Method.CARD],
                "amount_received_card": "10",
                "supervisor_discount_code": "1234",
                "tax_document_choice": "none",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(channel=Order.Channel.POS)
        line = OrderLine.objects.get(order=order)
        self.assertEqual(order.discount_total, self.stock_item.variant.sale_price - Decimal("10"))
        self.assertEqual(order.total, Decimal("10"))
        self.assertEqual(line.line_total, Decimal("10"))

    def test_checkout_requires_open_cash_session(self):
        CashSession.objects.update(status=CashSession.Status.CLOSED)

        response = self.client.post(
            reverse("pos:terminal"),
            {"action": "checkout", "payment_method": Payment.Method.CASH},
            follow=True,
        )

        self.assertContains(response, "No hay caja abierta")
        self.assertFalse(Order.objects.exists())

    def test_cart_quantity_can_increment_decrement_and_remove(self):
        self.client.post(reverse("pos:terminal"), {"action": "add", "stock_item_id": self.stock_item.id})

        self.client.post(
            reverse("pos:terminal"),
            {"action": "update", "variant_id": self.stock_item.variant_id, "delta": "1"},
        )
        session = self.client.session
        self.assertEqual(session["pos_cart"][str(self.stock_item.variant_id)]["quantity"], "2.000")

        self.client.post(
            reverse("pos:terminal"),
            {"action": "update", "variant_id": self.stock_item.variant_id, "delta": "-1"},
        )
        session = self.client.session
        self.assertEqual(session["pos_cart"][str(self.stock_item.variant_id)]["quantity"], "1.000")

        self.client.post(
            reverse("pos:terminal"),
            {"action": "update", "variant_id": self.stock_item.variant_id, "quantity": "0"},
        )
        session = self.client.session
        self.assertNotIn(str(self.stock_item.variant_id), session.get("pos_cart", {}))

    def test_quick_access_tab_only_shows_catalog_favorites(self):
        Product.objects.update(is_quick_access=False)
        favorite_product = self.stock_item.variant.product
        favorite_product.is_quick_access = True
        favorite_product.save(update_fields=["is_quick_access", "updated_at"])

        response = self.client.get(reverse("pos:terminal"), {"view": "quick"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Favoritos definidos desde el panel de Productos")
        self.assertContains(response, "Favorito TPV")
        self.assertContains(response, favorite_product.name)
        self.assertNotContains(response, "Pack stickers Ikiway")
