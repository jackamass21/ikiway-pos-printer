from io import StringIO
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cashier.models import CashSession
from catalog.models import Category, Product, ProductReview, ProductVariant, StorefrontFavorite
from customers.models import Customer, NewsletterSubscriber
from inventory.models import StockItem
from orders.models import Order, OrderLine
from payments.models import Payment
from promotions.models import Promotion
from pos.models import PosConfiguration, PosTicket
from shipping.models import Shipment
from tenancy.models import (
    Branch,
    Company,
    Plan,
    StoreOnboarding,
    StorefrontSettings,
    StorefrontBuilderAsset,
    Subscription,
    SubscriptionPayment,
    SupportMessage,
    SupportTicket,
    TenantMembership,
)


class SeedInitialDataTests(TestCase):
    def test_seed_initial_data_is_idempotent(self):
        call_command("seed_initial_data", stdout=StringIO())
        call_command("seed_initial_data", stdout=StringIO())

        self.assertEqual(Company.objects.filter(slug="ikiway").count(), 1)
        self.assertEqual(Branch.objects.count(), 2)
        self.assertEqual(ProductVariant.objects.count(), 4)
        self.assertEqual(StockItem.objects.count(), 8)
        self.assertEqual(CashSession.objects.filter(status=CashSession.Status.OPEN).count(), 1)
        self.assertTrue(get_user_model().objects.filter(email="vendedor@ikiway.cl").exists())
        self.assertTrue(TenantMembership.objects.filter(company__slug="ikiway", user__email="dueno@ikiway.cl").exists())
        self.assertTrue(Subscription.objects.filter(company__slug="ikiway", status=Subscription.Status.ACTIVE).exists())


class AdminLoginTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="usuario.desactivado@example.com",
            password="ClaveSegura123!",
            is_active=False,
        )

    def test_inactive_user_with_valid_password_gets_contact_message(self):
        response = self.client.post(
            reverse("admin:login"),
            {"username": self.user.email, "password": "ClaveSegura123!", "next": reverse("tenant_dashboard")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Esta cuenta está desactivada.")
        self.assertContains(response, "contactar a administración")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_user_with_invalid_password_gets_contact_message(self):
        response = self.client.post(
            reverse("admin:login"),
            {"username": self.user.email, "password": "ClaveIncorrecta", "next": reverse("tenant_dashboard")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Esta cuenta está desactivada.")
        self.assertContains(response, "contactar a administración")
        self.assertNotIn("_auth_user_id", self.client.session)


class SaaSOnboardingTests(TestCase):
    def test_home_redirects_to_ikiway_storefront(self):
        response = self.client.get(reverse("home"))

        self.assertRedirects(response, reverse("storefront_home", kwargs={"slug": "ikiway"}), fetch_redirect_response=False)

    def test_onboarding_creates_pending_payment_then_activates_subscription(self):
        plan = Plan.objects.get(slug="emprende")

        response = self.client.post(
            f"{reverse('onboarding')}?plan={plan.slug}",
            {
                "plan": plan.id,
                "payment_provider": SubscriptionPayment.Provider.SIMULATED,
                "store_name": "Tienda SaaS Test",
                "legal_name": "Tienda SaaS Test SpA",
                "tax_id": "78.315.818-3",
                "owner_name": "Dueno SaaS",
                "owner_email": "dueno.saas@example.com",
                "owner_phone": "+56912345678",
                "store_address": "Av SaaS 123",
                "store_city": "Santiago",
                "desired_slug": "tienda-saas-test",
                "notes": "Alta de prueba",
            },
        )

        self.assertEqual(response.status_code, 302)
        payment = SubscriptionPayment.objects.get(onboarding__desired_slug="tienda-saas-test")
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        self.assertTrue(response.headers["Location"].endswith(reverse("subscription_payment_return", kwargs={"payment_id": payment.id})))

        activation_response = self.client.get(reverse("subscription_payment_return", kwargs={"payment_id": payment.id}))

        self.assertEqual(activation_response.status_code, 302)
        self.assertEqual(activation_response.headers["Location"], reverse("onboarding_success"))
        self.assertTrue(Company.objects.filter(slug="tienda-saas-test", is_active=True).exists())
        self.assertTrue(Branch.objects.filter(company__slug="tienda-saas-test", code="STORE-001").exists())
        self.assertTrue(Branch.objects.filter(company__slug="tienda-saas-test", code="ONLINE", is_online_store=True).exists())
        self.assertTrue(TenantMembership.objects.filter(company__slug="tienda-saas-test", user__email="dueno.saas@example.com").exists())
        self.assertTrue(get_user_model().objects.get(email="dueno.saas@example.com").groups.filter(name="Administradores").exists())
        self.assertTrue(Subscription.objects.filter(company__slug="tienda-saas-test", plan=plan, status=Subscription.Status.ACTIVE).exists())
        self.assertTrue(StoreOnboarding.objects.filter(desired_slug="tienda-saas-test", status=StoreOnboarding.Status.ACTIVATED).exists())
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PAID)

    def test_protected_pages_block_inactive_subscriptions(self):
        call_command("seed_initial_data", stdout=StringIO())
        user = get_user_model().objects.get(email="vendedor@ikiway.cl")
        Subscription.objects.update(status=Subscription.Status.EXPIRED)
        self.client.force_login(user)

        response = self.client.get(reverse("pos:terminal"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("subscription_blocked"))


class TenantIsolationTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())
        self.user = get_user_model().objects.get(email="vendedor@ikiway.cl")
        self.client.force_login(self.user)
        membership = TenantMembership.objects.get(company__slug="ikiway", user=self.user)
        membership.module_access = ["pos", "products", "reports"]
        membership.save(update_fields=["module_access"])
        self.other_company = Company.objects.create(name="Otra Tienda", slug="otra-tienda", is_active=True)
        self.other_branch = Branch.objects.create(
            company=self.other_company,
            name="Sucursal Ajena",
            code="OTHER-001",
            is_active=True,
        )
        other_category = Category.objects.create(company=self.other_company, name="Categoria Ajena", slug="categoria-ajena")
        other_product = Product.objects.create(
            company=self.other_company,
            category=other_category,
            name="Producto Ajeno",
            slug="producto-ajeno",
            is_active=True,
        )
        self.other_variant = ProductVariant.objects.create(
            product=other_product,
            sku="OTHER-SKU-001",
            name="Producto Ajeno",
            sale_price="9990",
        )
        self.other_order = Order.objects.create(
            company=self.other_company,
            branch=self.other_branch,
            channel=Order.Channel.POS,
            status=Order.Status.PAID,
            subtotal="9990",
            tax_total="1594",
            total="9990",
        )
        OrderLine.objects.create(
            order=self.other_order,
            variant=self.other_variant,
            description="Producto Ajeno",
            quantity="1",
            unit_price="9990",
            tax_amount="1594",
            line_total="9990",
        )
        Payment.objects.create(order=self.other_order, method=Payment.Method.CASH, status=Payment.Status.PAID, amount="9990")
        PosTicket.objects.create(order=self.other_order, ticket_number="POS-OTHER-001")

    def test_api_products_are_limited_to_current_tenant(self):
        response = self.client.get("/api/v1/products/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cable USB-C 1m")
        self.assertNotContains(response, "Producto Ajeno")

    def test_api_create_customer_ignores_foreign_company_id(self):
        own_company = Company.objects.get(slug="ikiway")

        response = self.client.post(
            "/api/v1/customers/",
            {
                "company": self.other_company.id,
                "first_name": "Cliente",
                "last_name": "Tenant",
                "email": "cliente.tenant@example.com",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(own_company.customers.filter(email="cliente.tenant@example.com").exists())
        self.assertFalse(self.other_company.customers.filter(email="cliente.tenant@example.com").exists())

    def test_api_blocks_product_creation_when_plan_limit_is_reached(self):
        own_company = Company.objects.get(slug="ikiway")
        subscription = own_company.subscriptions.select_related("plan").order_by("-created_at").first()
        subscription.plan.max_products = own_company.products.count()
        subscription.plan.save(update_fields=["max_products"])

        response = self.client.post(
            "/api/v1/products/",
            {
                "name": "Producto sobre limite",
                "slug": "producto-sobre-limite",
                "is_active": "true",
                "is_sellable_online": "true",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(own_company.products.filter(slug="producto-sobre-limite").exists())

    def test_reports_do_not_show_other_company_sales(self):
        response = self.client.get(reverse("reports:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Producto Ajeno")
        self.assertNotContains(response, "$9.990")

    def test_pos_ticket_from_other_company_returns_404(self):
        response = self.client.get(reverse("pos:ticket", kwargs={"order_id": self.other_order.id}))

        self.assertEqual(response.status_code, 404)


class SalesChannelsManagerTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())
        self.user = get_user_model().objects.get(email="admin@ikiway.cl")
        self.company = Company.objects.get(slug="ikiway")
        self.client.force_login(self.user)

    def test_sales_channels_manager_loads(self):
        response = self.client.get(reverse("sales_channels_manager"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuracion de canales")
        self.assertContains(response, "TPV fisico")
        self.assertContains(response, "Ecommerce propio")
        self.assertContains(response, "Venta manual")
        self.assertContains(response, reverse("pos_configuration_manager"))

    def test_pos_configuration_manager_loads_and_updates_terminal(self):
        branch = Branch.objects.get(company=self.company, code="STORE-001")

        response = self.client.get(reverse("pos_configuration_manager"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuracion TPV")
        self.assertContains(response, "Apariencia")
        self.assertContains(response, "Impresion")

        update_response = self.client.post(
            reverse("pos_configuration_manager"),
            {
                "branch_id": branch.id,
                "branch_is_active": "on",
                "allow_cash_opening": "on",
                "show_product_images": "on",
                "require_supervisor_close_code": "on",
                "supervisor_close_code": "2468",
                "require_supervisor_return_code": "on",
                "supervisor_return_code": "1357",
                "require_supervisor_discount_code": "on",
                "supervisor_discount_code": "9753",
                "supervisor_discount_threshold_percent": "45",
                "primary_color": "#123456",
                "secondary_color": "#654321",
                "accent_color": "#22aa66",
                "receipt_paper_width": PosConfiguration.ReceiptPaperWidth.MM_58,
                "show_change_on_receipt": "on",
                "ticket_header": "Caja central",
                "ticket_footer": "Gracias por comprar",
                "printer_name": "Epson 80",
                "print_method": PosConfiguration.PrintMethod.AGENT,
                "print_copies": "2",
                "auto_print_receipt": "on",
                "cash_quick_amounts": "1000, 2000, 10000",
            },
        )

        self.assertEqual(update_response.status_code, 302)
        config = PosConfiguration.objects.get(branch=branch)
        self.assertEqual(config.primary_color, "#123456")
        self.assertEqual(config.secondary_color, "#654321")
        self.assertEqual(config.accent_color, "#22aa66")
        self.assertTrue(config.show_product_images)
        self.assertEqual(config.receipt_paper_width, PosConfiguration.ReceiptPaperWidth.MM_58)
        self.assertTrue(config.require_supervisor_close_code)
        self.assertEqual(config.supervisor_close_code, "2468")
        self.assertTrue(config.require_supervisor_return_code)
        self.assertEqual(config.supervisor_return_code, "1357")
        self.assertTrue(config.require_supervisor_discount_code)
        self.assertEqual(config.supervisor_discount_code, "9753")
        self.assertEqual(config.supervisor_discount_threshold_percent, Decimal("45"))
        self.assertEqual(config.ticket_header, "Caja central")
        self.assertEqual(config.ticket_footer, "Gracias por comprar")
        self.assertEqual(config.printer_name, "Epson 80")
        self.assertEqual(config.print_method, PosConfiguration.PrintMethod.AGENT)
        self.assertEqual(config.print_copies, 2)
        self.assertTrue(config.auto_print_receipt)
        self.assertEqual(config.get_cash_quick_amounts(), [1000, 2000, 10000])

    def test_seller_cannot_access_pos_configuration_manager(self):
        seller = get_user_model().objects.get(email="vendedor@ikiway.cl")
        self.client.force_login(seller)

        response = self.client.get(reverse("pos_configuration_manager"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("pos:terminal"))

    def test_can_update_pos_channel(self):
        branch = Branch.objects.get(company=self.company, code="STORE-001")

        response = self.client.post(
            reverse("sales_channels_manager"),
            {
                "action": "update_pos_channel",
                "branch_id": branch.id,
                "is_active": "on",
                "receipt_paper_width": PosConfiguration.ReceiptPaperWidth.MM_58,
                "cash_quick_amounts": "1000, 3000, 7000",
            },
        )

        self.assertEqual(response.status_code, 302)
        config = PosConfiguration.objects.get(branch=branch)
        self.assertEqual(config.receipt_paper_width, PosConfiguration.ReceiptPaperWidth.MM_58)
        self.assertEqual(config.get_cash_quick_amounts(), [1000, 3000, 7000])

    def test_can_update_ecommerce_channel(self):
        branch = Branch.objects.get(company=self.company, code="ONLINE")
        storefront = StorefrontSettings.objects.get(company=self.company)

        response = self.client.post(
            reverse("sales_channels_manager"),
            {
                "action": "update_ecommerce_channel",
                "branch_id": branch.id,
                "branch_is_active": "on",
                "is_published": "on",
                "pickup_enabled": "on",
                "default_shipping_price": "4990",
            },
        )

        self.assertEqual(response.status_code, 302)
        storefront.refresh_from_db()
        self.assertTrue(storefront.is_published)
        self.assertTrue(storefront.pickup_enabled)
        self.assertFalse(storefront.delivery_enabled)
        self.assertEqual(storefront.default_shipping_price, Decimal("4990"))

    def test_can_update_manual_channel(self):
        response = self.client.post(
            reverse("sales_channels_manager"),
            {
                "action": "update_manual_channel",
                "contact_phone": "+56 9 1111 2222",
                "contact_email": "ventas@ikiway.cl",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.company.refresh_from_db()
        self.assertEqual(self.company.contact_phone, "+56 9 1111 2222")
        self.assertEqual(self.company.contact_email, "ventas@ikiway.cl")


class TenantAccountManagerTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())
        self.user_model = get_user_model()
        self.company = Company.objects.get(slug="ikiway")
        self.admin_user = self.user_model.objects.get(email="admin@ikiway.cl")
        self.client.force_login(self.admin_user)

    def test_account_manager_loads_without_django_admin_link(self):
        response = self.client.get(reverse("tenant_account_manager"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mi cuenta")
        self.assertContains(response, "Datos de mi usuario")
        self.assertNotContains(response, "Usuarios de la tienda")
        self.assertNotContains(response, "/admin/password_change/")

    def test_panel_header_logout_closes_session(self):
        response = self.client.get(reverse("tenant_account_manager"))
        self.assertContains(response, reverse("tenant_logout"))
        self.assertContains(response, "Cerrar sesión")

        response = self.client.post(reverse("tenant_logout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("admin:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_user_can_update_own_profile(self):
        response = self.client.post(
            reverse("tenant_account_manager"),
            {
                "action": "update_profile",
                "first_name": "Admin",
                "last_name": "Actualizado",
                "email": "admin.actualizado@ikiway.cl",
                "phone": "+56 9 5555 5555",
            },
        )

        self.admin_user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.admin_user.email, "admin.actualizado@ikiway.cl")
        self.assertEqual(self.admin_user.phone, "+56 9 5555 5555")

    def test_user_can_change_own_password(self):
        response = self.client.post(
            reverse("tenant_account_manager"),
            {
                "action": "change_password",
                "current_password": "Ikiway12345",
                "password": "NuevaClave123!",
                "password_confirm": "NuevaClave123!",
            },
        )

        self.admin_user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.admin_user.check_password("NuevaClave123!"))

    def test_owner_can_create_support_ticket_and_confirmation_modal_is_shown(self):
        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        self.client.force_login(owner)

        response = self.client.post(
            reverse("tenant_support_center"),
            {
                "subject": "Problema al cerrar caja",
                "priority": SupportTicket.Priority.HIGH,
                "initial_message": "El cierre no permite confirmar la diferencia.",
            },
            follow=True,
        )

        ticket = SupportTicket.objects.get(subject="Problema al cerrar caja")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(f'{reverse("tenant_support_center")}?created={ticket.id}', 302)])
        self.assertContains(response, "Ticket enviado correctamente")
        self.assertContains(response, f"Ticket #{ticket.id}")
        self.assertEqual(ticket.company, self.company)
        self.assertEqual(ticket.opened_by, owner)
        self.assertTrue(ticket.messages.filter(is_internal=False, body__contains="diferencia").exists())

        saas_owner = self.user_model.objects.create_superuser(email="biobiohost@gmail.com", password="Admin12345")
        self.client.force_login(saas_owner)
        response = self.client.get(reverse("saas_admin_tickets"))
        self.assertContains(response, "Problema al cerrar caja")
        self.assertContains(response, self.company.name)
        self.assertContains(response, owner.email)
    def test_non_owner_cannot_open_support_module_or_create_tickets(self):
        response = self.client.get(reverse("tenant_account_manager"))
        self.assertNotContains(response, "Nuevo ticket")
        self.assertNotContains(response, reverse("tenant_support_center"))

        response = self.client.get(reverse("tenant_support_center"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("tenant_dashboard"))

        response = self.client.post(
            reverse("tenant_support_center"),
            {
                "subject": "Intento sin permiso",
                "priority": SupportTicket.Priority.NORMAL,
                "initial_message": "No debe crearse.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SupportTicket.objects.filter(subject="Intento sin permiso").exists())
    def test_owner_ticket_detail_hides_internal_notes_and_allows_reply(self):
        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        ticket = SupportTicket.objects.create(
            company=self.company,
            opened_by=owner,
            subject="Seguimiento",
            priority=SupportTicket.Priority.NORMAL,
        )
        SupportMessage.objects.create(ticket=ticket, sender=owner, body="Mensaje visible", is_internal=False)
        SupportMessage.objects.create(ticket=ticket, sender=self.admin_user, body="Nota secreta SaaS", is_internal=True)
        self.client.force_login(owner)

        response = self.client.get(reverse("tenant_support_ticket_detail", kwargs={"ticket_id": ticket.id}))
        self.assertContains(response, "Mensaje visible")
        self.assertNotContains(response, "Nota secreta SaaS")

        response = self.client.post(
            reverse("tenant_support_ticket_detail", kwargs={"ticket_id": ticket.id}),
            {"body": "Información adicional del dueño."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ticket.messages.filter(body="Información adicional del dueño.", is_internal=False).exists())

    def test_owner_can_create_tenant_user_with_module_access(self):
        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        self.client.force_login(owner)
        response = self.client.post(
            reverse("tenant_user_manager"),
            {
                "action": "create_user",
                "first_name": "Cajero",
                "last_name": "Nuevo",
                "email": "cajero.nuevo@ikiway.cl",
                "phone": "+56 9 1111 1111",
                "user_role": self.user_model.Role.SELLER,
                "modules": ["pos", "cashier"],
                "password": "ClaveNueva123!",
                "password_confirm": "ClaveNueva123!",
                "is_active": "on",
            },
        )

        new_user = self.user_model.objects.get(email="cajero.nuevo@ikiway.cl")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(new_user.check_password("ClaveNueva123!"))
        self.assertTrue(new_user.is_staff)
        self.assertTrue(new_user.groups.filter(name="Vendedores").exists())
        membership = TenantMembership.objects.get(company=self.company, user=new_user)
        self.assertEqual(membership.role, TenantMembership.Role.MEMBER)
        self.assertEqual(membership.module_access, ["pos", "cashier"])

    def test_owner_can_update_tenant_user_and_reset_password(self):
        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        self.client.force_login(owner)
        user = self.user_model.objects.get(email="vendedor@ikiway.cl")

        response = self.client.post(
            reverse("tenant_user_manager"),
            {
                "action": "update_user",
                "user_id": user.id,
                "first_name": "Vendedor",
                "last_name": "Editado",
                "email": "vendedor.editado@ikiway.cl",
                "phone": "+56 9 2222 2222",
                "user_role": self.user_model.Role.WAREHOUSE,
                "modules": ["products", "inventory"],
                "password": "ClaveEditada123!",
                "password_confirm": "ClaveEditada123!",
                "is_active": "on",
            },
        )

        user.refresh_from_db()
        membership = TenantMembership.objects.get(company=self.company, user=user)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(user.email, "vendedor.editado@ikiway.cl")
        self.assertEqual(user.role, self.user_model.Role.WAREHOUSE)
        self.assertEqual(membership.module_access, ["products", "inventory"])
        self.assertTrue(user.check_password("ClaveEditada123!"))
        self.assertTrue(user.groups.filter(name="Bodega").exists())

    def test_editing_user_without_new_password_preserves_current_password(self):
        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        self.client.force_login(owner)
        user = self.user_model.objects.get(email="vendedor@ikiway.cl")
        previous_password_hash = user.password

        response = self.client.post(
            reverse("tenant_user_manager"),
            {
                "action": "update_user",
                "user_id": user.id,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "phone": user.phone,
                "user_role": user.role,
                "modules": ["pos", "cashier"],
                "password": "",
                "password_confirm": "",
                "is_active": "on",
            },
        )

        user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(user.password, previous_password_hash)

    def test_seller_cannot_create_tenant_user(self):
        seller = self.user_model.objects.get(email="vendedor@ikiway.cl")
        self.client.force_login(seller)

        response = self.client.post(
            reverse("tenant_user_manager"),
            {
                "action": "create_user",
                "first_name": "Sin",
                "last_name": "Permiso",
                "email": "sin.permiso@ikiway.cl",
                "user_role": self.user_model.Role.SELLER,
                "membership_role": TenantMembership.Role.MEMBER,
                "password": "ClaveNueva123!",
                "password_confirm": "ClaveNueva123!",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.user_model.objects.filter(email="sin.permiso@ikiway.cl").exists())


    def test_only_owner_sees_and_opens_user_manager(self):
        response = self.client.get(reverse("tenant_user_manager"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("tenant_dashboard"))

        dashboard = self.client.get(reverse("tenant_dashboard"))
        self.assertNotContains(dashboard, reverse("tenant_user_manager"))

        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        self.client.force_login(owner)
        response = self.client.get(reverse("tenant_user_manager"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Módulos habilitados")
        self.assertContains(response, reverse("tenant_user_manager"))
        self.assertContains(response, 'data-open-product-modal="edit-user-modal-')
        self.assertContains(response, 'name="action" value="update_user"')
        self.assertContains(response, "Guardar cambios")
        self.assertContains(response, 'minlength="8"')
        self.assertContains(response, "ambas claves deben coincidir")
        self.assertContains(response, "Ambas contraseñas coinciden.")
        self.assertContains(response, "La contraseña actual está protegida.")
        self.assertContains(response, "Deja ambos campos vacíos para conservarla.")

    def test_module_access_hides_menu_and_blocks_direct_url(self):
        seller = self.user_model.objects.get(email="vendedor@ikiway.cl")
        membership = TenantMembership.objects.get(company=self.company, user=seller)
        membership.module_access = ["pos"]
        membership.save(update_fields=["module_access"])
        self.client.force_login(seller)

        dashboard = self.client.get(reverse("tenant_dashboard"))
        self.assertContains(dashboard, 'href="/pos/"')
        self.assertNotContains(dashboard, 'href="/reports/"')

        blocked = self.client.get(reverse("reports:dashboard"))
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(blocked.headers["Location"], reverse("tenant_dashboard"))

    def test_owner_cannot_assign_owner_role_to_another_user(self):
        owner = self.user_model.objects.get(email="dueno@ikiway.cl")
        self.client.force_login(owner)
        response = self.client.post(
            reverse("tenant_user_manager"),
            {
                "action": "create_user",
                "email": "otro.dueno@ikiway.cl",
                "user_role": self.user_model.Role.OWNER,
                "password": "ClaveNueva123!",
                "password_confirm": "ClaveNueva123!",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.user_model.objects.filter(email="otro.dueno@ikiway.cl").exists())


class SaaSAdminTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())
        self.saas_owner = get_user_model().objects.create_superuser(email="biobiohost@gmail.com", password="Admin12345")
        self.client.force_login(self.saas_owner)

    def test_dashboard_loads_for_staff(self):
        response = self.client.get(reverse("saas_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard SaaS")
        self.assertContains(response, "Clientes activos")
        self.assertContains(response, "MRR estimado")

    def test_saas_owner_login_redirects_to_saas_dashboard(self):
        self.client.logout()

        response = self.client.post(
            reverse("admin:login"),
            {"username": self.saas_owner.email, "password": "Admin12345"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("saas_admin_dashboard"))

    def test_saas_owner_can_still_open_django_admin_directly(self):
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)

    def test_client_list_links_manual_client_creation(self):
        response = self.client.get(reverse("saas_admin_clients"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nuevo cliente")
        self.assertContains(response, reverse("saas_admin_client_create"))

    def test_client_creation_form_uses_spanish_owner_labels(self):
        response = self.client.get(reverse("saas_admin_client_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Usuario propietario")
        self.assertContains(response, "Nombre del propietario")
        self.assertContains(response, "Correo del propietario")
        self.assertContains(response, "Teléfono del propietario")
        self.assertContains(response, "Clave del propietario")
        self.assertNotContains(response, "Usuario owner")

    def test_staff_can_create_client_manually(self):
        plan = Plan.objects.get(slug="emprende")

        response = self.client.post(
            reverse("saas_admin_client_create"),
            {
                "plan": plan.id,
                "status": Subscription.Status.ACTIVE,
                "months": "3",
                "store_name": "Cliente Manual",
                "legal_name": "Cliente Manual SpA",
                "tax_id": "78.315.818-3",
                "contact_email": "contacto@manual.cl",
                "contact_phone": "+56 9 1111 2222",
                "desired_slug": "cliente-manual",
                "physical_branch_name": "Casa matriz",
                "store_address": "Av Manual 123",
                "store_city": "Concepcion",
                "owner_name": "Owner Manual",
                "owner_email": "owner.manual@example.com",
                "owner_phone": "+56 9 3333 4444",
                "owner_password": "Manual12345!",
                "is_published": "on",
                "notes": "Alta por vendedor.",
            },
        )

        company = Company.objects.get(slug="cliente-manual")
        owner = get_user_model().objects.get(email="owner.manual@example.com")
        subscription = company.subscriptions.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("saas_admin_client_detail", kwargs={"company_id": company.id}))
        self.assertEqual(company.name, "Cliente Manual")
        self.assertTrue(owner.check_password("Manual12345!"))
        self.assertTrue(owner.groups.filter(name="Administradores").exists())
        self.assertTrue(TenantMembership.objects.filter(company=company, user=owner, role=TenantMembership.Role.OWNER).exists())
        self.assertTrue(Branch.objects.filter(company=company, code="STORE-001", name="Casa matriz").exists())
        self.assertTrue(Branch.objects.filter(company=company, code="ONLINE", is_online_store=True).exists())
        self.assertTrue(StorefrontSettings.objects.filter(company=company, is_published=True).exists())
        self.assertTrue(PosConfiguration.objects.filter(branch__company=company, branch__code="STORE-001").exists())
        self.assertEqual(subscription.plan, plan)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertTrue(StoreOnboarding.objects.filter(company=company, status=StoreOnboarding.Status.ACTIVATED).exists())

    def test_non_saas_owner_is_redirected_from_saas_admin(self):
        user = get_user_model().objects.get(email="admin@ikiway.cl")
        self.client.force_login(user)

        response = self.client.get(reverse("saas_admin_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard/")

    def test_saas_panel_only_lists_client_created_tickets(self):
        company = Company.objects.get(slug="ikiway")
        owner = get_user_model().objects.get(email="dueno@ikiway.cl")
        client_ticket = SupportTicket.objects.create(
            company=company,
            opened_by=owner,
            subject="Ayuda del cliente",
            priority=SupportTicket.Priority.HIGH,
        )
        SupportMessage.objects.create(
            ticket=client_ticket,
            sender=owner,
            body="Solicitud creada por la tienda.",
            is_internal=False,
        )
        internal_ticket = SupportTicket.objects.create(
            company=company,
            opened_by=self.saas_owner,
            subject="Gestion interna SaaS",
            priority=SupportTicket.Priority.NORMAL,
        )
        SupportMessage.objects.create(
            ticket=internal_ticket,
            sender=self.saas_owner,
            body="No debe mostrarse.",
            is_internal=True,
        )

        response = self.client.get(reverse("saas_admin_tickets"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ayuda del cliente")
        self.assertNotContains(response, "Gestion interna SaaS")
        self.assertNotContains(response, "Nuevo ticket interno")

        ticket_count = SupportTicket.objects.count()
        response = self.client.post(
            reverse("saas_admin_tickets"),
            {
                "company": company.id,
                "subject": "No crear desde SaaS",
                "priority": SupportTicket.Priority.HIGH,
                "initial_message": "Este formulario fue eliminado.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SupportTicket.objects.count(), ticket_count)
    def test_staff_can_assign_plan_manually(self):
        company = Company.objects.get(slug="ikiway")
        plan = Plan.objects.get(slug="crece")

        response = self.client.post(
            reverse("saas_admin_client_assign_plan", kwargs={"company_id": company.id}),
            {"plan": plan.id, "status": Subscription.Status.ACTIVE, "months": "2"},
        )

        self.assertEqual(response.status_code, 302)
        subscription = company.subscriptions.order_by("-created_at").first()
        self.assertEqual(subscription.plan, plan)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertTrue(company.is_active)

    def test_staff_can_edit_plan_and_home_reflects_changes(self):
        plan = Plan.objects.get(slug="crece")

        response = self.client.post(
            reverse("saas_admin_plan_edit", kwargs={"plan_id": plan.id}),
            {
                "name": "Crece Plus",
                "slug": "crece",
                "description": "Plan actualizado desde SaaS admin.",
                "price": "29990",
                "compare_at_price": "39990",
                "discount_label": "25% descuento",
                "billing_period": Plan.BillingPeriod.MONTHLY,
                "max_branches": "4",
                "max_users": "12",
                "max_products": "2000",
                "includes_physical_pos": "on",
                "includes_online_store": "on",
                "is_featured": "on",
                "is_active": "on",
                "sort_order": "2",
                "features_text": "Caja por vendedor\nSoporte prioritario\nReportes avanzados",
            },
        )

        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.name, "Crece Plus")
        self.assertEqual(plan.price, Decimal("29990"))
        self.assertEqual(plan.compare_at_price, Decimal("39990"))
        self.assertEqual(plan.discount_label, "25% descuento")
        self.assertEqual(plan.max_users, 12)
        self.assertEqual(plan.max_products, 2000)
        self.assertEqual(plan.features, ["Caja por vendedor", "Soporte prioritario", "Reportes avanzados"])

        plans_response = self.client.get(reverse("saas_admin_plans"))
        self.assertContains(plans_response, "Crece Plus")
        self.assertContains(plans_response, "$29.990")
        self.assertContains(plans_response, "$39.990")
        self.assertContains(plans_response, "25% descuento")
        self.assertContains(plans_response, "12")
        self.assertContains(plans_response, "2000")

    def test_saas_plan_list_loads(self):
        response = self.client.get(reverse("saas_admin_plans"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Planes SaaS")
        self.assertContains(response, "Editar")

    def test_staff_can_suspend_deactivate_and_reactivate_client(self):
        company = Company.objects.get(slug="ikiway")

        suspend_response = self.client.post(reverse("saas_admin_client_set_status", kwargs={"company_id": company.id, "action": "suspend"}))
        company.refresh_from_db()
        subscription = company.subscriptions.order_by("-created_at").first()
        self.assertEqual(suspend_response.status_code, 302)
        self.assertEqual(subscription.status, Subscription.Status.PAST_DUE)

        deactivate_response = self.client.post(reverse("saas_admin_client_set_status", kwargs={"company_id": company.id, "action": "deactivate"}))
        company.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(deactivate_response.status_code, 302)
        self.assertFalse(company.is_active)
        self.assertEqual(subscription.status, Subscription.Status.CANCELLED)

        reactivate_response = self.client.post(reverse("saas_admin_client_set_status", kwargs={"company_id": company.id, "action": "reactivate"}))
        company.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(reactivate_response.status_code, 302)
        self.assertTrue(company.is_active)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)

    def test_staff_can_edit_client_and_storefront_settings(self):
        company = Company.objects.get(slug="ikiway")

        response = self.client.post(
            reverse("saas_admin_client_edit", kwargs={"company_id": company.id}),
            {
                "name": "Ikiway Editado",
                "legal_name": "Ikiway SpA",
                "tax_id": "78.315.818-3",
                "contact_email": "nuevo@ikiway.cl",
                "contact_phone": "+56 9 1111 1111",
                "currency": "CLP",
                "timezone": "America/Santiago",
                "is_active": "on",
                "display_name": "Tienda Editada",
                "tagline": "Nueva bajada",
                "logo_url": "",
                "primary_color": "#1d4fe8",
                "secondary_color": "#111827",
                "accent_color": "#16a34a",
                "custom_domain": "",
                "pickup_enabled": "on",
                "delivery_enabled": "on",
                "default_shipping_price": "4990",
                "is_published": "on",
            },
        )

        company.refresh_from_db()
        company.storefront_settings.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(company.name, "Ikiway Editado")
        self.assertEqual(company.storefront_settings.display_name, "Tienda Editada")

    def test_staff_can_reset_store_user_password(self):
        company = Company.objects.get(slug="ikiway")
        user = get_user_model().objects.get(email="vendedor@ikiway.cl")

        response = self.client.post(
            reverse("saas_admin_client_reset_user_password", kwargs={"company_id": company.id}),
            {"user": user.id, "password": "NuevaClave123!", "password_confirm": "NuevaClave123!"},
        )

        user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(user.check_password("NuevaClave123!"))

    def test_password_reset_rejects_user_from_another_client(self):
        company = Company.objects.get(slug="ikiway")
        other_user = get_user_model().objects.create_user(email="otro.cliente@example.com", password="Anterior123!")

        response = self.client.post(
            reverse("saas_admin_client_reset_user_password", kwargs={"company_id": company.id}),
            {"user": other_user.id, "password": "NuevaClave123!", "password_confirm": "NuevaClave123!"},
        )

        other_user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(other_user.check_password("Anterior123!"))


class StorefrontTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())
        self.company = Company.objects.get(slug="ikiway")
        self.stock_item = StockItem.objects.get(variant__sku="TEC-CABLE-USBC-1M", branch__code="ONLINE")

    def _enable_storefront_favorites(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        sections = default_builder_sections(storefront)
        for section in sections:
            if section["type"] in {"catalog", "product_showcase"}:
                section["config"]["show_favorites"] = True
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])

    def _set_catalog_only_sections(self, **catalog_config):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        sections = default_builder_sections(storefront)
        for section in sections:
            section["is_active"] = section["type"] == "catalog"
            if section["type"] == "catalog":
                section["config"].update(catalog_config)
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])
        return storefront

    def test_storefront_catalog_loads_by_slug(self):
        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ikiway")
        self.assertContains(response, "Cable USB-C 1m")
        self.assertNotContains(response, "Favoritos")

    def test_storefront_account_link_never_points_to_internal_panel(self):
        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, reverse("storefront_account", kwargs={"slug": self.company.slug}))
        self.assertContains(response, reverse("storefront_register", kwargs={"slug": self.company.slug}))
        self.assertContains(response, "Registrarme")
        self.assertNotContains(response, 'href="/mi-cuenta/"')
        self.assertNotContains(response, 'href="/admin/login/')

    def test_customer_can_register_and_open_storefront_account(self):
        response = self.client.post(
            reverse("storefront_register", kwargs={"slug": self.company.slug}),
            {
                "first_name": "Camila",
                "last_name": "Compradora",
                "email": "camila@example.com",
                "phone": "+56911112222",
                "password1": "CompraSegura2026!",
                "password2": "CompraSegura2026!",
            },
        )

        user = get_user_model().objects.get(email="camila@example.com")
        self.assertRedirects(response, reverse("storefront_account", kwargs={"slug": self.company.slug}))
        self.assertEqual(user.role, get_user_model().Role.CUSTOMER)
        self.assertTrue(Customer.objects.filter(company=self.company, user=user).exists())
        self.assertEqual(str(self.client.session.get("_auth_user_id")), str(user.id))

    def test_storefront_register_uses_auth_defaults_and_real_form_fields(self):
        response = self.client.get(reverse("storefront_register", kwargs={"slug": self.company.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Crea tu")
        self.assertContains(response, "<strong>cuenta</strong>", html=True)
        self.assertContains(response, "Compra más rápido")
        self.assertContains(response, 'autocomplete="given-name"')
        self.assertContains(response, 'autocomplete="new-password"', count=2)
        self.assertContains(response, "data-password-toggle", count=2)
        self.assertNotContains(response, ">MI CUENTA<")

    def test_storefront_register_reports_duplicate_email_and_password_mismatch(self):
        get_user_model().objects.create_user(
            email="repetido@example.com",
            password="CompraSegura2026!",
            role=get_user_model().Role.CUSTOMER,
        )

        response = self.client.post(
            reverse("storefront_register", kwargs={"slug": self.company.slug}),
            {
                "first_name": "Camila",
                "last_name": "Compradora",
                "email": "repetido@example.com",
                "phone": "+56911112222",
                "password1": "CompraSegura2026!",
                "password2": "OtraClave2026!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ya existe una cuenta con este correo")
        self.assertContains(response, "Las contraseñas no coinciden")
        self.assertContains(response, 'id="id_email_errors"')
        self.assertContains(response, 'id="id_password2_errors"')

    def test_storefront_register_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)

        response = csrf_client.post(
            reverse("storefront_register", kwargs={"slug": self.company.slug}),
            {"email": "cliente@example.com", "password1": "Clave2026!", "password2": "Clave2026!"},
        )

        self.assertEqual(response.status_code, 403)

    def test_storefront_register_preserves_safe_next(self):
        cart_url = reverse("storefront_cart", kwargs={"slug": self.company.slug})

        response = self.client.post(
            reverse("storefront_register", kwargs={"slug": self.company.slug}),
            {
                "first_name": "Elena",
                "last_name": "Compradora",
                "email": "elena@example.com",
                "phone": "+56922223333",
                "password1": "CompraSegura2026!",
                "password2": "CompraSegura2026!",
                "next": cart_url,
            },
        )

        self.assertRedirects(response, cart_url)
        user = get_user_model().objects.get(email="elena@example.com")
        self.assertTrue(Customer.objects.filter(company=self.company, user=user).exists())

    def test_storefront_register_keeps_email_and_password_validation(self):
        response = self.client.post(
            reverse("storefront_register", kwargs={"slug": self.company.slug}),
            {
                "first_name": "Ana",
                "email": "correo-invalido",
                "password1": "123",
                "password2": "123",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertIn("password1", response.context["form"].errors)

    def test_storefront_register_custom_config_is_isolated_by_company(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.builder_sections = default_builder_sections(storefront)
        header = next(section for section in storefront.builder_sections if section["type"] == "header")
        header["config"]["account_access"]["register"] = {
            "title": "Únete a Ikiway ahora",
            "highlight_text": "Ikiway",
            "description": "Registro exclusivo de esta tienda.",
            "form_title": "Datos Ikiway",
            "form_subtitle": "Completa tu perfil.",
            "show_benefits": False,
        }
        storefront.save(update_fields=["builder_sections"])
        other_company = Company.objects.create(name="Otra tienda registro", slug="otra-tienda-registro")
        StorefrontSettings.objects.create(company=other_company, display_name=other_company.name, builder_enabled=True)

        ikiway_response = self.client.get(reverse("storefront_register", kwargs={"slug": self.company.slug}))
        other_response = self.client.get(reverse("storefront_register", kwargs={"slug": other_company.slug}))

        self.assertContains(ikiway_response, "Únete a")
        self.assertContains(ikiway_response, "<strong>Ikiway</strong>", html=True)
        self.assertContains(ikiway_response, "Datos Ikiway")
        self.assertNotContains(ikiway_response, "Compra más rápido")
        self.assertNotContains(other_response, "Registro exclusivo de esta tienda")
        self.assertContains(other_response, "Crea tu")

    def test_internal_user_cannot_use_storefront_login(self):
        internal_user = get_user_model().objects.get(email="vendedor@ikiway.cl")
        internal_user.set_password("ClaveInterna2026!")
        internal_user.save(update_fields=["password"])

        response = self.client.post(
            reverse("storefront_login", kwargs={"slug": self.company.slug}),
            {"email": internal_user.email, "password": "ClaveInterna2026!"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "El correo o la contraseña no son correctos.")
        self.assertContains(response, f'value="{internal_user.email}"')
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_customer_login_preserves_next_and_uses_public_account(self):
        user = get_user_model().objects.create_user(
            email="cliente.login@example.com",
            password="CompraSegura2026!",
            role=get_user_model().Role.CUSTOMER,
        )
        account_url = reverse("storefront_account", kwargs={"slug": self.company.slug})

        response = self.client.post(
            reverse("storefront_login", kwargs={"slug": self.company.slug}),
            {"email": user.email, "password": "CompraSegura2026!", "next": account_url},
        )

        self.assertRedirects(response, account_url)
        self.assertTrue(Customer.objects.filter(company=self.company, user=user).exists())

    def test_storefront_login_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)

        response = csrf_client.post(
            reverse("storefront_login", kwargs={"slug": self.company.slug}),
            {"email": "cliente@example.com", "password": "NoImporta123!"},
        )

        self.assertEqual(response.status_code, 403)

    def test_storefront_login_uses_defaults_and_has_no_dead_recovery_link(self):
        response = self.client.get(reverse("storefront_login", kwargs={"slug": self.company.slug}))

        self.assertContains(response, "Qué bueno verte")
        self.assertContains(response, "Revisa tus pedidos")
        self.assertContains(response, "Crear cuenta")
        self.assertContains(response, "data-password-toggle")
        self.assertNotContains(response, "Olvidaste tu contraseña")
        self.assertNotContains(response, ">MI CUENTA<")

    def test_storefront_login_custom_config_is_isolated_by_company(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.builder_sections = default_builder_sections(storefront)
        header = next(section for section in storefront.builder_sections if section["type"] == "header")
        header["config"]["account_access"] = {
            "title": "Acceso exclusivo Ikiway",
            "highlight_text": "Ikiway",
            "description": "Descripción privada de esta tienda.",
            "show_info_panel": True,
            "show_benefits": False,
            "registration_enabled": False,
            "login_title": "Ingresa a Ikiway",
            "login_subtitle": "Consulta tus compras.",
        }
        storefront.save(update_fields=["builder_sections"])
        other_company = Company.objects.create(name="Otra tienda", slug="otra-tienda-login")
        StorefrontSettings.objects.create(company=other_company, display_name=other_company.name, builder_enabled=True)

        ikiway_response = self.client.get(reverse("storefront_login", kwargs={"slug": self.company.slug}))
        other_response = self.client.get(reverse("storefront_login", kwargs={"slug": other_company.slug}))

        self.assertContains(ikiway_response, "Acceso exclusivo")
        self.assertContains(ikiway_response, "<strong>Ikiway</strong>", html=True)
        self.assertContains(ikiway_response, "Ingresa a Ikiway")
        self.assertNotContains(ikiway_response, "Revisa tus pedidos")
        self.assertNotContains(ikiway_response, ">Crear cuenta</a>")
        self.assertNotContains(other_response, "Acceso exclusivo Ikiway")
        self.assertContains(other_response, "Qué bueno verte")

    def test_storefront_account_only_lists_orders_from_current_store(self):
        user = get_user_model().objects.create_user(
            email="comprador@example.com",
            password="CompraSegura2026!",
            role=get_user_model().Role.CUSTOMER,
        )
        customer = Customer.objects.create(company=self.company, user=user, first_name="Cliente", email=user.email)
        visible_order = Order.objects.create(
            company=self.company,
            branch=self.company.branches.get(code="ONLINE"),
            customer=customer,
            channel=Order.Channel.ONLINE,
            total=24990,
        )
        other_company = Company.objects.create(name="Otra tienda", slug="otra-tienda")
        other_branch = Branch.objects.create(company=other_company, name="Online", code="ONLINE", is_online_store=True)
        other_customer = Customer.objects.create(company=other_company, user=user, first_name="Cliente", email=user.email)
        hidden_order = Order.objects.create(
            company=other_company,
            branch=other_branch,
            customer=other_customer,
            channel=Order.Channel.ONLINE,
            total=9990,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("storefront_account", kwargs={"slug": self.company.slug}))

        self.assertContains(response, f"Pedido #{visible_order.id}")
        self.assertNotContains(response, f"Pedido #{hidden_order.id}")

    def test_storefront_uses_ikiway_visual_tokens(self):
        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.builder_theme = {}
        storefront.save(update_fields=["builder_theme"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, "--ikiway-pink: #F45BB5")
        self.assertContains(response, "--ikiway-blue: #65C8ED")
        self.assertContains(response, "--ikiway-yellow: #FBE85C")
        self.assertContains(response, "--ikiway-purple: #C75ADB")
        self.assertContains(response, "--storefront-background: #FFF9F0")
        self.assertContains(response, "--ikiway-white: #FFFFFF")

        other_company = Company.objects.create(name="Tienda neutra", slug="tienda-neutra")
        other_storefront = StorefrontSettings.objects.create(company=other_company, display_name=other_company.name)
        self.assertEqual(other_storefront.get_builder_theme()["background"], "#FFFFFF")

    def test_product_detail_uses_public_dynamic_specs_without_inventory_fields(self):
        variant = self.stock_item.variant
        variant.product.specifications = {"Material": "Algodón", "Cuidados": "Lavado en frío"}
        variant.product.save(update_fields=["specifications", "updated_at"])
        variant.attributes = {"Conectividad": "USB-C", "Color": "Blanco"}
        variant.barcode = "INTERNAL-123"
        variant.save(update_fields=["attributes", "barcode", "updated_at"])

        detail_url = reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": variant.product.slug})
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'role="tablist"')
        self.assertContains(response, 'data-product-tab="description"')
        self.assertContains(response, 'data-product-tab="specifications"')
        self.assertContains(response, 'data-product-tab="reviews"')

        specs_response = self.client.get(
            detail_url,
            {"product_tab": "specifications"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(specs_response.status_code, 200)
        self.assertTrue(specs_response.json()["success"])
        specs_html = specs_response.json()["html"]
        self.assertIn("Conectividad", specs_html)
        self.assertIn("USB-C", specs_html)
        self.assertIn("Color", specs_html)
        self.assertIn("Material", specs_html)
        self.assertIn("Algodón", specs_html)
        self.assertIn("Cuidados", specs_html)
        self.assertNotIn("Stock minimo", specs_html)
        self.assertNotIn("INTERNAL-123", specs_html)
        self.assertNotIn("Canales de venta", specs_html)

        reviews_response = self.client.get(
            detail_url,
            {"product_tab": "reviews"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(reviews_response.status_code, 200)
        self.assertIn("Este producto aún no tiene reseñas", reviews_response.json()["html"])

    def test_product_detail_config_hides_optional_sections(self):
        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.product_detail_config = {
            "show_brand": False,
            "show_sku": False,
            "show_specifications": False,
            "show_related_products": False,
        }
        storefront.save(update_fields=["product_detail_config", "updated_at"])

        response = self.client.get(reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": self.stock_item.variant.product.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Especificaciones")
        self.assertNotContains(response, "También te puede interesar")
        self.assertNotContains(response, ">SKU<")

    def test_product_detail_rejects_hidden_and_other_company_products(self):
        hidden = Product.objects.create(company=self.company, name="Producto oculto", slug="producto-oculto", is_sellable_online=False)
        other_company = Company.objects.create(name="Empresa externa", slug="empresa-externa")
        foreign = Product.objects.create(company=other_company, name="Producto externo", slug="producto-externo")

        hidden_response = self.client.get(reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": hidden.slug}))
        foreign_response = self.client.get(reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": foreign.slug}))

        self.assertEqual(hidden_response.status_code, 404)
        self.assertEqual(foreign_response.status_code, 404)

    def test_product_detail_handles_single_and_multiple_real_images(self):
        product = self.stock_item.variant.product
        product.image = "product_images/main.png"
        product.save(update_fields=["image", "updated_at"])

        single_response = self.client.get(reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": product.slug}))
        self.assertEqual(len(single_response.context["gallery_images"]), 1)
        self.assertNotContains(single_response, "data-gallery-thumb")

        self.stock_item.variant.image = "product_variant_images/variant.png"
        self.stock_item.variant.save(update_fields=["image", "updated_at"])
        multiple_response = self.client.get(reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": product.slug}))
        self.assertEqual(len(multiple_response.context["gallery_images"]), 2)
        self.assertContains(multiple_response, "data-gallery-thumb", count=2)

    def test_product_detail_disables_purchase_when_out_of_stock(self):
        self.stock_item.quantity = 0
        self.stock_item.save(update_fields=["quantity", "updated_at"])

        response = self.client.get(reverse("storefront_product_detail", kwargs={"slug": self.company.slug, "product_slug": self.stock_item.variant.product.slug}))

        self.assertContains(response, "Producto agotado")
        self.assertNotContains(response, 'class="store-product-buy-form pd-modern-buy-form"')

    def test_only_customer_with_paid_product_order_can_publish_review(self):
        product = self.stock_item.variant.product
        detail_url = reverse(
            "storefront_product_detail",
            kwargs={"slug": self.company.slug, "product_slug": product.slug},
        )
        user = get_user_model().objects.create_user(
            email="compradora.resena@ikiway.cl",
            password="CompraSegura2026!",
            role=get_user_model().Role.CUSTOMER,
        )
        customer = Customer.objects.create(company=self.company, user=user, email=user.email, first_name="Camila")
        order = Order.objects.create(
            company=self.company,
            branch=self.stock_item.branch,
            customer=customer,
            channel=Order.Channel.ONLINE,
            status=Order.Status.PENDING,
            total=self.stock_item.variant.sale_price,
        )
        OrderLine.objects.create(
            order=order,
            variant=self.stock_item.variant,
            description=product.name,
            quantity=1,
            unit_price=self.stock_item.variant.sale_price,
            line_total=self.stock_item.variant.sale_price,
        )
        self.client.force_login(user)
        ajax_headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_ACCEPT": "application/json"}

        pending_response = self.client.get(detail_url, {"product_tab": "reviews"}, **ajax_headers)
        self.assertNotIn("pd-modern-review-form", pending_response.json()["html"])
        self.assertIn("cuando la compra de este producto esté pagada", pending_response.json()["html"])

        blocked_response = self.client.post(
            f"{detail_url}?product_tab=reviews",
            {"action": "review", "rating": "5", "title": "Excelente", "comment": "Muy bonito."},
        )
        self.assertEqual(blocked_response.status_code, 302)
        self.assertFalse(ProductReview.objects.filter(product=product, user=user).exists())

        order.status = Order.Status.PAID
        order.save(update_fields=["status", "updated_at"])
        paid_response = self.client.get(detail_url, {"product_tab": "reviews"}, **ajax_headers)
        self.assertIn("pd-modern-review-form", paid_response.json()["html"])

        published_response = self.client.post(
            f"{detail_url}?product_tab=reviews",
            {"action": "review", "rating": "5", "title": "Excelente", "comment": "Muy bonito."},
        )
        self.assertEqual(published_response.status_code, 302)
        self.assertTrue(ProductReview.objects.filter(product=product, user=user, rating=5).exists())

    def test_store_admin_cannot_publish_storefront_review(self):
        product = self.stock_item.variant.product
        detail_url = reverse(
            "storefront_product_detail",
            kwargs={"slug": self.company.slug, "product_slug": product.slug},
        )
        admin = get_user_model().objects.get(email="admin@ikiway.cl")
        self.client.force_login(admin)

        response = self.client.get(
            detail_url,
            {"product_tab": "reviews"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertNotIn("pd-modern-review-form", response.json()["html"])
        self.assertIn("solo pueden publicarlas clientes", response.json()["html"])

        post_response = self.client.post(
            f"{detail_url}?product_tab=reviews",
            {"action": "review", "rating": "5", "title": "Admin", "comment": "No corresponde."},
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertFalse(ProductReview.objects.filter(product=product, user=admin).exists())

    def test_product_detail_settings_saves_normalized_options(self):
        self.client.force_login(get_user_model().objects.get(email="admin@ikiway.cl"))

        response = self.client.post(reverse("tenant_product_detail_settings"), {
            "product_detail_layout": "classic",
            "gallery_size": "large",
            "thumbnail_position": "below",
            "show_breadcrumb": "on",
            "show_category": "on",
            "show_stock": "on",
            "show_description": "on",
            "show_related_products": "on",
            "related_title": "Completa tu colección",
            "related_limit": "8",
        })

        self.assertEqual(response.status_code, 302)
        config = StorefrontSettings.objects.get(company=self.company).product_detail_config
        self.assertEqual(config["thumbnail_position"], "below")
        self.assertEqual(config["related_title"], "Completa tu colección")
        self.assertEqual(config["related_limit"], 8)
        self.assertFalse(config["show_brand"])

        editor_response = self.client.get(reverse("tenant_product_detail_settings"))
        self.assertContains(editor_response, "Ficha de producto")
        self.assertContains(editor_response, 'name="gallery_size"')
        self.assertContains(editor_response, 'name="show_specifications"')
        self.assertContains(editor_response, "data-detail-preview")
        sample_stock_item = editor_response.context["sample_stock_item"]
        self.assertIsNotNone(sample_stock_item)
        self.assertContains(editor_response, sample_stock_item.variant.product.name)

    def test_ikiway_renders_configurable_commerce_footer(self):
        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="store-footer-commerce"')
        self.assertContains(response, 'style="--store-footer-background: #FBE85C;"')
        self.assertContains(response, "Kawaii, anime, papeleria y coleccionables")
        self.assertContains(response, 'aria-label="Visa"')
        self.assertContains(response, 'aria-label="Mastercard"')
        self.assertContains(response, 'alt="Mercado Pago"')
        self.assertContains(response, "/static/img/mercado-pago-logo.png")
        self.assertContains(response, "/static/img/ikiway-newsletter-bunny.png")
        self.assertContains(response, 'alt="Conejito Ikiway"')
        self.assertContains(response, 'class="store-footer-newsletter-cta has-illustration"')
        self.assertContains(response, '<button type="submit">Suscribirme</button>', html=True)
        self.assertContains(response, reverse("storefront_newsletter_subscribe", kwargs={"slug": self.company.slug}))
        self.assertNotContains(response, ">Contacto<")
        self.assertContains(response, 'class="store-footer-bottom"')
        self.assertNotContains(response, 'class="store-footer-legal"')
        self.assertContains(response, "Todos los derechos reservados")
        self.assertNotContains(response, "Webpay")
        self.assertNotContains(response, "Khipu")

    def test_footer_newsletter_uses_storefront_specific_uploaded_asset(self):
        storefront = StorefrontSettings.objects.get(company=self.company)
        StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.FOOTER_NEWSLETTER,
            image="storefront_builder/ikiway/footer_newsletter/conejito.png",
            alt_text="Conejito personalizado",
        )

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, "/media/storefront_builder/ikiway/footer_newsletter/conejito.png")
        self.assertContains(response, 'alt="Conejito Ikiway"')

    def test_storefront_newsletter_stores_one_normalized_email_per_company(self):
        url = reverse("storefront_newsletter_subscribe", kwargs={"slug": self.company.slug})

        first = self.client.post(url, {"email": " Cliente@Example.com "})
        second = self.client.post(url, {"email": "cliente@example.com"})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(NewsletterSubscriber.objects.filter(company=self.company).count(), 1)
        subscriber = NewsletterSubscriber.objects.get(company=self.company)
        self.assertEqual(subscriber.email, "cliente@example.com")
        self.assertTrue(subscriber.is_active)

    def test_footer_normalization_rejects_unknown_payments_and_unsafe_urls(self):
        from core.storefront_builder import normalize_footer_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        config = normalize_footer_config(
            storefront,
            {
                "payment_methods": ["visa", "webpay", "mercadopago"],
                "background_color": "yellow",
                "shopping_links": [
                    {"label": "Peligroso", "url": "javascript:alert(1)", "is_active": True},
                ],
            },
        )

        self.assertEqual(config["payment_methods"], ["visa", "mercadopago"])
        self.assertEqual(config["background_color"], "#FBE85C")
        self.assertEqual(config["shopping_links"][0]["url"], "")

        custom = normalize_footer_config(storefront, {"background_color": "#12abEF"})
        self.assertEqual(custom["background_color"], "#12ABEF")

        other_company = Company.objects.create(name="Tienda verde", slug="tienda-verde")
        other_storefront = StorefrontSettings.objects.create(
            company=other_company,
            display_name=other_company.name,
            builder_theme={"background": "#E8FFF0"},
        )
        other_config = normalize_footer_config(other_storefront, {})
        self.assertEqual(other_config["background_color"], "#FFFFFF")

    def test_old_storefront_without_footer_configuration_keeps_minimal_fallback(self):
        from core.storefront_builder import default_builder_sections

        company = Company.objects.create(name="Otra tienda", slug="otra-tienda")
        storefront = StorefrontSettings.objects.create(
            company=company,
            display_name=company.name,
            is_published=True,
            builder_enabled=True,
        )
        storefront.builder_sections = [
            section for section in default_builder_sections(storefront) if section["type"] != "footer"
        ]
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": company.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="store-footer"')
        self.assertNotContains(response, 'class="store-footer-commerce"')

    def test_configured_footer_can_be_disabled_without_rendering_a_fallback(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.builder_sections = default_builder_sections(storefront)
        footer = next(section for section in storefront.builder_sections if section["type"] == "footer")
        footer["is_active"] = False
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="store-footer-commerce"')
        self.assertNotContains(response, 'class="store-footer"')

    def test_builder_saves_complete_theme_and_shows_live_preview(self):
        self.client.force_login(get_user_model().objects.get(email="admin@ikiway.cl"))

        response = self.client.post(
            reverse("tenant_storefront_builder"),
            {
                "builder_enabled": "on",
                "theme_background": "#FFFFFF",
                "theme_surface": "#FFF1F7",
                "theme_text": "#17213A",
                "theme_muted": "#667085",
                "theme_primary": "#F45BB5",
                "theme_secondary": "#65C8ED",
                "theme_highlight": "#FBE85C",
                "theme_accent": "#C75ADB",
                "theme_pink_soft": "#FFF1F7",
                "theme_blue_soft": "#EFFAFF",
                "theme_radius": "22",
                "auth_show_info_panel": "on",
                "auth_show_benefits": "on",
                "auth_registration_enabled": "on",
                "auth_title": "Bienvenido a tu tienda",
                "auth_highlight_text": "tu tienda",
                "auth_description": "Consulta tus pedidos y favoritos.",
                "auth_login_title": "Acceso de clientes",
                "auth_login_subtitle": "Ingresa con tu correo.",
                "auth_benefit_0_title": "Tus pedidos",
                "auth_benefit_0_description": "Revisa cada compra.",
                "auth_benefit_1_title": "Tus favoritos",
                "auth_benefit_1_description": "Encuentralos facilmente.",
                "auth_benefit_2_title": "Novedades",
                "auth_benefit_2_description": "Conoce nuevas ofertas.",
                "auth_register_show_benefits": "on",
                "auth_register_title": "Crea tu perfil de compra",
                "auth_register_highlight_text": "perfil",
                "auth_register_description": "Compra más rápido en esta tienda.",
                "auth_register_form_title": "Tus datos de cliente",
                "auth_register_form_subtitle": "Completa el registro.",
                "auth_register_benefit_0_title": "Compra rápida",
                "auth_register_benefit_0_description": "Ahorra pasos.",
                "auth_register_benefit_1_title": "Favoritos guardados",
                "auth_register_benefit_1_description": "Encuéntralos después.",
                "auth_register_benefit_2_title": "Nuevos productos",
                "auth_register_benefit_2_description": "Recibe novedades.",
                "checkout_show_breadcrumb": "on",
                "checkout_show_sku": "on",
                "checkout_show_product_images": "on",
                "checkout_show_shipping_info": "on",
                "checkout_show_help": "on",
                "checkout_show_mascot": "on",
                "checkout_sticky_summary_desktop": "on",
                "checkout_pickup_enabled": "on",
                "checkout_delivery_enabled": "on",
                "checkout_title": "Completa tu pedido",
                "checkout_subtitle": "Datos seguros para tu compra.",
                "checkout_help_title": "¿Necesitas ayuda?",
                "checkout_help_text": "Escribenos y te ayudaremos.",
                "checkout_support_label": "Contactar soporte",
                "checkout_support_url": "mailto:contacto@ikiway.cl",
                "checkout_shipping_provider": "bluexpress",
                "checkout_shipping_provider_label": "Bluexpress",
                "checkout_pickup_line_1": "Bulnes 136, Local 16",
                "checkout_pickup_line_2": "Galeria Camara de Comercio",
                "checkout_pickup_city": "Talcahuano",
                "checkout_mascot_url": "/static/img/ikiway-checkout-bunny.png",
                "section_header_is_active": "on",
                "section_header_sort_order": "-10",
                "section_header_show_topbar": "on",
                "section_header_logo_url": "/static/img/ikiway-logo.png",
                "section_header_left_text": "Despachos a todo Chile",
                "section_header_center_text": "Retiro disponible en tienda - Bulnes 136 Local 22",
                "section_header_show_search": "on",
                "section_header_search_placeholder": "Buscar productos kawaii...",
                "section_header_show_favorites": "on",
                "section_header_show_account": "on",
                "section_header_show_cart": "on",
                "section_header_show_categories": "on",
                "section_header_show_socials": "on",
                "section_hero_is_active": "on",
                "section_hero_sort_order": "0",
                "section_benefits_is_active": "on",
                "section_benefits_sort_order": "5",
                "section_benefits_item_0_icon": "truck",
                "section_benefits_item_0_title": "Envios a todo Chile",
                "section_benefits_item_0_description": "Rapidos y seguros",
                "section_benefits_item_0_is_active": "on",
                "section_benefits_item_0_sort_order": "0",
                "section_categories_is_active": "on",
                "section_categories_sort_order": "10",
                "section_featured_products_is_active": "on",
                "section_featured_products_sort_order": "20",
                "section_catalog_is_active": "on",
                "section_catalog_sort_order": "30",
                "section_cta_is_active": "on",
                "section_cta_sort_order": "40",
            },
        )

        self.assertEqual(response.status_code, 302)
        storefront = StorefrontSettings.objects.get(company=self.company)
        self.assertEqual(storefront.builder_theme["primary"], "#F45BB5")
        self.assertEqual(storefront.builder_theme["secondary"], "#65C8ED")
        self.assertEqual(storefront.builder_theme["highlight"], "#FBE85C")
        self.assertEqual(storefront.builder_theme["accent"], "#C75ADB")
        self.assertEqual(storefront.builder_theme["background"], "#FFFFFF")
        self.assertEqual(storefront.checkout_form_config["checkout_layout"]["title"], "Completa tu pedido")
        self.assertEqual(storefront.shipping_config["provider"], "bluexpress")
        self.assertTrue(storefront.pickup_enabled)
        self.assertTrue(storefront.delivery_enabled)
        header = next(section for section in storefront.builder_sections if section["type"] == "header")
        benefits = next(section for section in storefront.builder_sections if section["type"] == "benefits")
        self.assertEqual(header["config"]["search_placeholder"], "Buscar productos kawaii...")
        self.assertEqual(header["config"]["logo_url"], "/static/img/ikiway-logo.png")
        self.assertEqual(header["config"]["account_access"]["title"], "Bienvenido a tu tienda")
        self.assertEqual(header["config"]["account_access"]["login_title"], "Acceso de clientes")
        self.assertTrue(header["config"]["account_access"]["registration_enabled"])
        self.assertEqual(header["config"]["account_access"]["register"]["title"], "Crea tu perfil de compra")
        self.assertEqual(header["config"]["account_access"]["register"]["form_title"], "Tus datos de cliente")
        self.assertTrue(header["config"]["account_access"]["register"]["show_benefits"])
        self.assertEqual(benefits["config"]["items"][0]["icon"], "truck")

        response = self.client.get(reverse("tenant_storefront_builder"))
        self.assertContains(response, "Aplicar identidad Ikiway")
        self.assertContains(response, 'data-preview-device="mobile"')
        self.assertContains(response, 'name="theme_secondary"')
        self.assertContains(response, "Color de fondo de la pagina")
        self.assertContains(response, "Define el color de fondo general de tu tienda online.")
        self.assertContains(response, 'name="section_header_show_search"')
        self.assertContains(response, 'name="auth_title"')
        self.assertContains(response, 'name="auth_register_title"')
        self.assertContains(response, 'data-auth-preview-mode="register"')
        self.assertContains(response, 'data-auth-preview')
        self.assertNotContains(response, "Mostrar favoritos")
        self.assertContains(response, 'name="section_catalog_product_card_style"')
        self.assertContains(response, 'name="section_catalog_show_category"')
        self.assertContains(response, 'name="section_catalog_show_favorites"')
        self.assertContains(response, 'name="section_catalog_show_exact_stock"')
        self.assertContains(response, 'data-preview-product-grid')
        self.assertContains(response, 'data-category-menu-input="icon"')
        self.assertContains(response, 'value="bunny"')
        self.assertContains(response, 'name="section_benefits_item_0_title"')
        self.assertContains(response, 'name="section_footer_newsletter_enabled"')
        self.assertContains(response, 'name="section_footer_background_color"')
        self.assertContains(response, 'data-footer-field="background-color"')
        self.assertContains(response, 'name="footer_newsletter_image"')
        self.assertContains(response, 'data-footer-newsletter-file-input')
        self.assertContains(response, 'name="section_footer_payment_mercadopago"')
        self.assertContains(response, 'data-preview-footer')
        self.assertContains(response, 'name="section_product_showcase_source"')
        self.assertContains(response, 'data-preview-showcase')
        self.assertContains(response, 'name="section_social_gallery_layout"')
        self.assertContains(response, 'name="social_gallery_images"')
        self.assertContains(response, 'data-preview-social')
        self.assertContains(response, 'name="section_store_presence_social_enabled"')
        self.assertContains(response, 'name="section_store_presence_latitude"')
        self.assertContains(response, 'name="section_store_presence_map_zoom"')
        self.assertContains(response, 'data-preview-presence')

    def test_builder_rejects_unsafe_page_background(self):
        from core.forms import StorefrontBuilderThemeForm

        storefront = StorefrontSettings.objects.get(company=self.company)
        form = StorefrontBuilderThemeForm(
            {
                "builder_enabled": "on",
                "theme_background": "red; background-image: url(evil)",
            },
            instance=storefront,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("theme_background", form.errors)

    def test_builder_serializes_footer_links_newsletter_and_payment_methods(self):
        from core.storefront_builder import serialize_builder_sections_from_post

        storefront = StorefrontSettings.objects.get(company=self.company)
        sections = serialize_builder_sections_from_post(
            {
                "section_footer_is_active": "on",
                "section_footer_sort_order": "999",
                "section_footer_description": "Descripcion propia",
                "section_footer_background_color": "#123456",
                "section_footer_show_socials": "on",
                "section_footer_newsletter_enabled": "on",
                "section_footer_newsletter_title": "Novedades",
                "section_footer_newsletter_text": "Noticias de la tienda",
                "section_footer_newsletter_placeholder": "correo@ejemplo.cl",
                "section_footer_newsletter_button": "Quiero recibirlas",
                "section_footer_newsletter_disclaimer": "Acepto comunicaciones.",
                "section_footer_newsletter_image": "javascript:alert(1)",
                "section_footer_newsletter_show_image": "on",
                "section_footer_payment_visa": "on",
                "section_footer_payment_mercadopago": "on",
                "section_footer_show_legal_bar": "on",
                "section_footer_shopping_links_0_label": "Ver catalogo",
                "section_footer_shopping_links_0_url": "products",
                "section_footer_shopping_links_0_is_active": "on",
            },
            storefront,
        )
        footer = next(section for section in sections if section["type"] == "footer")

        self.assertTrue(footer["is_active"])
        self.assertEqual(footer["config"]["description"], "Descripcion propia")
        self.assertEqual(footer["config"]["background_color"], "#123456")
        self.assertEqual(footer["config"]["newsletter_title"], "Novedades")
        self.assertEqual(footer["config"]["newsletter_image"], "")
        self.assertEqual(footer["config"]["payment_methods"], ["visa", "mercadopago"])
        self.assertEqual(footer["config"]["shopping_links"][0]["label"], "Ver catalogo")

    def test_product_showcase_normalization_limits_values_and_company_products(self):
        from core.storefront_builder import normalize_product_showcase_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        own_product = self.stock_item.variant.product
        other_company = Company.objects.create(name="Tienda ajena", slug="tienda-ajena")
        other_product = Product.objects.create(company=other_company, name="Producto ajeno", slug="producto-ajeno")

        config = normalize_product_showcase_config(
            storefront,
            {
                "source": "desconocido",
                "limit": "99",
                "desktop_columns": "9",
                "manual_product_ids": [str(other_product.id), str(own_product.id), str(own_product.id)],
                "view_all_url": "javascript:alert(1)",
            },
        )

        self.assertEqual(config["source"], "newest")
        self.assertEqual(config["limit"], 12)
        self.assertEqual(config["desktop_columns"], 4)
        self.assertEqual(config["manual_product_ids"], [own_product.id])
        self.assertEqual(config["view_all_url"], "")

    def test_newest_product_showcase_uses_online_catalog_and_deduplicates_products(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        product = Product.objects.create(
            company=self.company,
            category=self.stock_item.variant.product.category,
            name="Ultima novedad Ikiway",
            slug="ultima-novedad-ikiway",
        )
        variant = ProductVariant.objects.create(product=product, sku="IKI-NUEVO-TEST", sale_price=Decimal("12990"))
        StockItem.objects.create(branch=self.stock_item.branch, variant=variant, quantity=8)
        second_variant = ProductVariant.objects.create(product=product, sku="IKI-NUEVO-TEST-2", sale_price=Decimal("13990"))
        StockItem.objects.create(branch=self.stock_item.branch, variant=second_variant, quantity=5)
        storefront.builder_sections = default_builder_sections(storefront)
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        showcase = next(
            entry for entry in response.context["builder_sections"] if entry["section"].type == "product_showcase"
        )
        catalog = next(
            entry for entry in response.context["builder_sections"] if entry["section"].type == "catalog"
        )

        self.assertEqual(showcase["products"][0].variant.product_id, product.id)
        self.assertEqual([item.variant.product_id for item in showcase["products"]].count(product.id), 1)
        catalog_item = next(item for item in catalog["products"] if item.variant.product_id == product.id)
        self.assertEqual(len(catalog_item.storefront_variants), 2)
        self.assertEqual([item.variant.product_id for item in catalog["products"]].count(product.id), 1)
        self.assertNotContains(response, 'data-product-variant-select')
        self.assertContains(response, "2 variaciones disponibles")
        self.assertContains(response, "Ver opciones")
        self.assertContains(response, "Ultima novedad Ikiway")
        self.assertContains(response, "Ver todos los recién llegados")

    def test_manual_product_showcase_preserves_selection_order(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        selected = list(Product.objects.filter(company=self.company, is_sellable_online=True)[:2])
        sections = default_builder_sections(storefront)
        showcase = next(section for section in sections if section["type"] == "product_showcase")
        showcase["config"]["source"] = "manual"
        showcase["config"]["manual_product_ids"] = [selected[1].id, selected[0].id]
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        entry = next(
            item for item in response.context["builder_sections"] if item["section"].type == "product_showcase"
        )

        self.assertEqual(
            [item.variant.product_id for item in entry["products"]],
            [selected[1].id, selected[0].id],
        )

    def test_social_gallery_rejects_foreign_assets_and_unsafe_links(self):
        from core.storefront_builder import normalize_social_gallery_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        own_asset = StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.SOCIAL_GALLERY,
            image="storefront_builder/ikiway/social_gallery/uno.jpg",
        )
        other_company = Company.objects.create(name="Otra galeria", slug="otra-galeria")
        other_storefront = StorefrontSettings.objects.create(company=other_company, display_name="Otra galeria")
        foreign_asset = StorefrontBuilderAsset.objects.create(
            storefront=other_storefront,
            placement=StorefrontBuilderAsset.Placement.SOCIAL_GALLERY,
            image="storefront_builder/otra/social_gallery/dos.jpg",
        )

        config = normalize_social_gallery_config(
            storefront,
            {
                "layout": "diagonal",
                "profile_url": "javascript:alert(1)",
                "items": [
                    {"asset_id": own_asset.id, "link_url": "javascript:alert(2)"},
                    {"asset_id": foreign_asset.id, "link_url": "https://example.com/ajeno"},
                ],
            },
        )

        self.assertEqual(config["layout"], "carousel")
        self.assertEqual(config["profile_url"], "")
        self.assertEqual(config["items"], [{"asset_id": own_asset.id, "link_url": ""}])

    def test_social_gallery_is_hidden_until_an_active_image_exists(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.builder_sections = default_builder_sections(storefront)
        social_section = next(section for section in storefront.builder_sections if section["type"] == "social_gallery")
        social_section["is_active"] = True
        storefront.save(update_fields=["builder_sections"])

        empty_response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        self.assertNotContains(empty_response, 'class="store-social-gallery')

        StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.SOCIAL_GALLERY,
            image="storefront_builder/ikiway/social_gallery/publicacion.jpg",
            alt_text="Publicacion de Ikiway",
        )
        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, 'class="store-social-gallery is-carousel"')
        self.assertContains(response, 'alt="Publicacion de Ikiway"')
        self.assertContains(response, "Ver en Instagram")

    def test_store_presence_normalizes_coordinates_zoom_and_urls(self):
        from core.storefront_builder import normalize_store_presence_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        valid = normalize_store_presence_config(
            storefront,
            {
                "latitude": "-36.7139617",
                "longitude": "-73.1130808",
                "map_zoom": "18",
                "social_url": "https://www.instagram.com/ikiway.cl/",
            },
        )
        invalid = normalize_store_presence_config(
            storefront,
            {
                "latitude": "91",
                "longitude": "-181",
                "map_zoom": "30",
                "social_url": "javascript:alert(1)",
            },
        )

        self.assertEqual(valid["latitude"], -36.7139617)
        self.assertEqual(valid["longitude"], -73.1130808)
        self.assertEqual(valid["map_zoom"], 18)
        self.assertEqual(valid["social_url"], "https://www.instagram.com/ikiway.cl/")
        self.assertIsNone(invalid["latitude"])
        self.assertIsNone(invalid["longitude"])
        self.assertEqual(invalid["map_zoom"], 19)
        self.assertEqual(invalid["social_url"], "")

    def test_store_presence_is_multi_company_and_ikiway_defaults_are_exact(self):
        from core.storefront_builder import default_store_presence_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        ikiway = default_store_presence_config(storefront)
        other_company = Company.objects.create(name="Otra presencia", slug="otra-presencia")
        other_storefront = StorefrontSettings.objects.create(company=other_company, display_name="Otra presencia")
        other = default_store_presence_config(other_storefront)

        self.assertEqual(ikiway["social_handle"], "@ikiway.cl")
        self.assertEqual(ikiway["location_title"], "Bulnes 136, Local 16")
        self.assertEqual(ikiway["latitude"], -36.7139617)
        self.assertEqual(ikiway["longitude"], -73.1130808)
        self.assertEqual(ikiway["map_zoom"], 18)
        self.assertEqual(other["social_handle"], "")
        self.assertEqual(other["location_title"], "")
        self.assertIsNone(other["latitude"])
        self.assertFalse(other["location_enabled"])

    def test_store_presence_cards_can_be_enabled_independently_or_hidden(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        sections = default_builder_sections(storefront)
        presence = next(section for section in sections if section["type"] == "store_presence")
        presence["config"]["social_enabled"] = False
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])

        location_only = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        self.assertContains(location_only, 'class="store-presence-location')
        self.assertNotContains(location_only, 'class="store-presence-social"')

        presence["config"]["social_enabled"] = True
        presence["config"]["location_enabled"] = False
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])
        social_only = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        self.assertContains(social_only, 'class="store-presence-social"')
        self.assertNotContains(social_only, 'class="store-presence-location')

        presence["config"]["social_enabled"] = False
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])
        hidden = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        self.assertNotContains(hidden, 'class="store-presence')

    def test_store_presence_without_coordinates_renders_address_without_map(self):
        from core.storefront_builder import default_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        sections = default_builder_sections(storefront)
        presence = next(section for section in sections if section["type"] == "store_presence")
        presence["config"]["latitude"] = None
        presence["config"]["longitude"] = None
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, "Bulnes 136, Local 16")
        self.assertNotContains(response, 'class="store-presence-map"')
        self.assertNotContains(response, "google.com/maps/dir")

    def test_store_presence_builds_openstreetmap_and_google_directions_links(self):
        from core.storefront_builder import default_store_presence_config, store_presence_directions_url, store_presence_map_url

        storefront = StorefrontSettings.objects.get(company=self.company)
        config = default_store_presence_config(storefront)

        self.assertIn("openstreetmap.org/export/embed.html", store_presence_map_url(config))
        self.assertIn("marker=-36.7139617%2C-73.1130808", store_presence_map_url(config))
        self.assertEqual(
            store_presence_directions_url(config),
            "https://www.google.com/maps/dir/?api=1&destination=-36.7139617%2C-73.1130808",
        )

    def test_legacy_ikiway_social_gallery_is_preserved_but_deactivated(self):
        from core.storefront_builder import default_builder_sections, normalize_builder_sections

        storefront = StorefrontSettings.objects.get(company=self.company)
        legacy = [section for section in default_builder_sections(storefront) if section["type"] != "store_presence"]
        social = next(section for section in legacy if section["type"] == "social_gallery")
        social["is_active"] = True
        social["config"]["title"] = "Galeria conservada"
        storefront.builder_sections = legacy
        storefront.save(update_fields=["builder_sections"])

        normalized = normalize_builder_sections(storefront)
        normalized_social = next(section for section in normalized if section.type == "social_gallery")
        presence = next(section for section in normalized if section.type == "store_presence")

        self.assertFalse(normalized_social.is_active)
        self.assertEqual(normalized_social.config["title"], "Galeria conservada")
        self.assertTrue(presence.is_active)

    def test_category_menu_configuration_controls_icon_visibility_order_and_target(self):
        storefront = StorefrontSettings.objects.get(company=self.company)
        categories = list(Category.objects.filter(company=self.company, is_active=True).order_by("name")[:2])
        sections = storefront.builder_sections or []
        if not sections:
            from core.storefront_builder import default_builder_sections

            sections = default_builder_sections(storefront)
        header = next(section for section in sections if section["type"] == "header")
        header["config"]["category_items"] = [
            {"category_id": categories[0].id, "icon": "gamepad", "is_visible": True, "sort_order": 20},
            {"category_id": categories[1].id, "icon": "heart", "is_visible": False, "sort_order": 10},
        ]
        storefront.builder_sections = sections
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, f'data-category-icon="gamepad"')
        self.assertContains(response, f'?category={categories[0].slug}')
        self.assertContains(response, 'class="store-category-all is-active"')
        self.assertContains(response, ">Ver todos</span>")
        self.assertNotContains(response, ">Inicio</span>")
        self.assertNotContains(response, f'?category={categories[1].slug}#catalogo')
        self.assertNotContains(response, '#catalogo" data-store-category-link')
        html = response.content.decode()
        catalog_start = html.index('id="catalogo"')
        product_grid = html.index('class="store-builder-product-grid"', catalog_start)
        category_menu = html.index('class="store-category-nav"', catalog_start)
        self.assertLess(category_menu, product_grid)

    def test_product_card_config_is_safe_and_out_of_stock_products_are_visible(self):
        from core.storefront_builder import normalize_product_card_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        config = normalize_product_card_config(
            storefront,
            {
                "product_card_style": "unknown",
                "desktop_columns": "9",
                "mobile_columns": "0",
                "image_fit": "stretch",
                "show_description": "false",
                "show_exact_stock": "on",
            },
        )

        self.assertEqual(config["product_card_style"], "clean")
        self.assertEqual(config["desktop_columns"], 4)
        self.assertEqual(config["mobile_columns"], 2)
        self.assertEqual(config["image_fit"], "contain")
        self.assertFalse(config["show_description"])
        self.assertTrue(config["show_category"])
        self.assertFalse(config["show_favorites"])
        self.assertTrue(config["show_exact_stock"])

        self.stock_item.quantity = 0
        self.stock_item.save(update_fields=["quantity"])
        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, "Agotado")
        self.assertContains(response, "store-builder-product-form is-disabled")

        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.show_out_of_stock_products = False
        storefront.save(update_fields=["show_out_of_stock_products"])
        self._set_catalog_only_sections()

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        self.assertNotContains(response, "Cable USB-C 1m")
        self.assertNotContains(response, "Agotado")

    def test_storefront_catalog_uses_infinite_scroll_batches(self):
        storefront = self._set_catalog_only_sections(initial_items=4, items_per_page=4)
        online_branch = Branch.objects.get(company=self.company, code="ONLINE")
        category = Category.objects.create(company=self.company, name="Scroll", slug="scroll")
        for index in range(8):
            product = Product.objects.create(
                company=self.company,
                category=category,
                name=f"Producto Scroll {index:02d}",
                slug=f"producto-scroll-{index:02d}",
                is_active=True,
                is_sellable_online=True,
            )
            variant = ProductVariant.objects.create(product=product, sku=f"SCROLL-{index:02d}", sale_price=Decimal("1000"))
            StockItem.objects.create(branch=online_branch, variant=variant, quantity=Decimal("5"))

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}), {"category": "scroll"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Producto Scroll 00")
        self.assertContains(response, "Producto Scroll 03")
        self.assertNotContains(response, "Producto Scroll 04")
        self.assertContains(response, 'data-store-catalog-loader')
        self.assertContains(response, 'data-offset="4"')

        response = self.client.get(
            reverse("storefront_catalog_items", kwargs={"slug": self.company.slug}),
            {"category": "scroll", "offset": "4"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("Producto Scroll 04", payload["html"])
        self.assertIn("Producto Scroll 07", payload["html"])
        self.assertEqual(payload["next_offset"], 8)
        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["total"], 8)

        storefront.refresh_from_db()
        self.assertTrue(storefront.builder_sections)

    def test_storefront_customer_can_toggle_favorite_without_touching_tpv_favorites(self):
        self._enable_storefront_favorites()
        product = self.stock_item.variant.product
        original_quick_access = product.is_quick_access
        favorite_url = reverse(
            "storefront_toggle_favorite",
            kwargs={"slug": self.company.slug, "product_id": product.id},
        )

        response = self.client.post(favorite_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"is_favorite": True, "count": 1})
        favorite = StorefrontFavorite.objects.get(company=self.company, product=product)
        self.assertIsNone(favorite.user_id)
        self.assertTrue(favorite.session_key)
        product.refresh_from_db()
        self.assertEqual(product.is_quick_access, original_quick_access)

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))
        self.assertContains(response, f'data-product-id="{product.id}"')
        self.assertContains(response, "store-product-favorite is-active")
        self.assertContains(response, product.category.name)
        self.assertContains(response, f"{self.stock_item.quantity:.0f} disponibles")

        response = self.client.post(favorite_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertJSONEqual(response.content, {"is_favorite": False, "count": 0})
        self.assertFalse(StorefrontFavorite.objects.filter(company=self.company, product=product).exists())

    def test_anonymous_storefront_favorites_are_kept_when_customer_logs_in(self):
        self._enable_storefront_favorites()
        product = self.stock_item.variant.product
        favorite_url = reverse(
            "storefront_toggle_favorite",
            kwargs={"slug": self.company.slug, "product_id": product.id},
        )
        self.client.post(favorite_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        customer_user = get_user_model().objects.create_user(
            email="cliente.storefront@ikiway.cl",
            password="Cliente12345!",
        )
        self.client.force_login(customer_user)
        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "store-product-favorite is-active")
        self.assertTrue(
            StorefrontFavorite.objects.filter(
                company=self.company,
                product=product,
                user=customer_user,
            ).exists()
        )

    def test_builder_serializes_category_menu_controls(self):
        from core.storefront_builder import serialize_builder_sections_from_post

        storefront = StorefrontSettings.objects.get(company=self.company)
        category = Category.objects.filter(company=self.company, is_active=True).order_by("name").first()

        sections = serialize_builder_sections_from_post(
            {
                f"category_nav_{category.id}_icon": "notebook",
                f"category_nav_{category.id}_is_visible": "on",
                f"category_nav_{category.id}_sort_order": "7",
            },
            storefront,
        )

        header = next(section for section in sections if section["type"] == "header")
        saved = next(item for item in header["config"]["category_items"] if item["category_id"] == category.id)
        self.assertEqual(saved["icon"], "notebook")
        self.assertTrue(saved["is_visible"])
        self.assertEqual(saved["sort_order"], 7)

    def test_storefront_header_uses_real_search_categories_and_cart(self):
        self.client.post(
            reverse("storefront_cart", kwargs={"slug": self.company.slug}),
            {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "2"},
        )

        response = self.client.get(
            reverse("storefront_home", kwargs={"slug": self.company.slug}),
            {"q": "Cable"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Cable"')
        self.assertContains(response, "Buscar productos kawaii...")
        self.assertContains(response, "Tecnologia")
        self.assertContains(response, "Carrito")
        self.assertContains(response, "<span>2</span>", html=True)

    def test_legacy_hero_config_remains_compatible(self):
        storefront = StorefrontSettings.objects.get(company=self.company)
        storefront.builder_sections = [
            {
                "type": "hero",
                "label": "Portada",
                "sort_order": 0,
                "is_active": True,
                "config": {"title": "Portada anterior", "subtitle": "Contenido compatible", "image_url": ""},
            }
        ]
        storefront.save(update_fields=["builder_sections"])

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, "Portada anterior")
        self.assertContains(response, "Contenido compatible")
        self.assertNotContains(response, "data-store-hero-next")

    def test_multiple_hero_assets_enable_accessible_carousel_controls(self):
        storefront = StorefrontSettings.objects.get(company=self.company)
        StorefrontBuilderAsset.objects.create(storefront=storefront, image="storefront_builder/test/uno.jpg", sort_order=0)
        StorefrontBuilderAsset.objects.create(storefront=storefront, image="storefront_builder/test/dos.jpg", sort_order=10)

        response = self.client.get(reverse("storefront_home", kwargs={"slug": self.company.slug}))

        self.assertContains(response, 'data-store-hero-previous')
        self.assertContains(response, 'aria-label="Banner siguiente"')
        self.assertContains(response, 'aria-roledescription="carrusel"')

    def test_other_company_uses_neutral_builder_defaults(self):
        other = Company.objects.create(name="Otra tienda", slug="otra-tienda")
        storefront = StorefrontSettings.objects.create(company=other, display_name="Otra tienda", builder_theme={})

        self.assertEqual(storefront.get_builder_theme()["primary"], "#1D4FE8")
        self.assertNotEqual(storefront.get_builder_theme()["primary"], "#F45BB5")

    def test_cart_and_checkout_create_online_order(self):
        add_response = self.client.post(
            reverse("storefront_cart", kwargs={"slug": self.company.slug}),
            {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "2"},
        )

        self.assertEqual(add_response.status_code, 302)
        checkout_response = self.client.post(
            reverse("storefront_checkout", kwargs={"slug": self.company.slug}),
            {
                "first_name": "Cliente",
                "last_name": "Online",
                "email": "cliente.online@example.com",
                "phone": "+56922222222",
                "tax_id": "78.315.818-3",
                "delivery_method": "pickup",
                "address": "",
                "notes": "Retiro en tienda",
            },
        )

        order = Order.objects.get(channel=Order.Channel.ONLINE, customer__email="cliente.online@example.com")
        self.assertEqual(checkout_response.status_code, 302)
        self.assertEqual(checkout_response.headers["Location"], reverse("storefront_order_success", kwargs={"slug": self.company.slug, "order_id": order.id}))
        self.assertEqual(order.lines.count(), 1)
        self.assertTrue(Payment.objects.filter(order=order, status=Payment.Status.PENDING).exists())
        self.assertTrue(Shipment.objects.filter(order=order, method=Shipment.Method.PICKUP).exists())

    def test_checkout_delivery_validates_region_commune_and_address(self):
        from core.forms import StorefrontCheckoutForm

        storefront = StorefrontSettings.objects.get(company=self.company)
        base_data = {
            "first_name": "Cliente",
            "last_name": "Online",
            "email": "cliente@example.com",
            "phone": "+56922222222",
            "tax_id": "78.315.818-3",
            "delivery_method": "delivery",
            "address": "Bulnes 100",
        }
        missing_region = StorefrontCheckoutForm(base_data, storefront=storefront)
        self.assertFalse(missing_region.is_valid())
        self.assertIn("region", missing_region.errors)
        self.assertIn("comuna", missing_region.errors)

        wrong_commune = StorefrontCheckoutForm(
            {**base_data, "region": "08", "comuna": "Santiago"},
            storefront=storefront,
        )
        self.assertFalse(wrong_commune.is_valid())
        self.assertIn("comuna", wrong_commune.errors)

    def test_checkout_delivery_creates_shipment_with_territorial_address(self):
        self.client.post(
            reverse("storefront_cart", kwargs={"slug": self.company.slug}),
            {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "1"},
        )
        response = self.client.post(
            reverse("storefront_checkout", kwargs={"slug": self.company.slug}),
            {
                "first_name": "Cliente",
                "last_name": "Delivery",
                "email": "delivery@example.com",
                "phone": "+56922222222",
                "tax_id": "78.315.818-3",
                "delivery_method": "delivery",
                "region": "08",
                "comuna": "Talcahuano",
                "address": "Bulnes 136 Local 16",
                "notes": "Galeria Camara de Comercio",
            },
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(customer__email="delivery@example.com")
        self.assertEqual(order.total, self.stock_item.variant.sale_price + Decimal("3990"))
        self.assertEqual(order.shipment.method, Shipment.Method.DELIVERY)
        self.assertIn("Talcahuano", order.shipment.address)
        self.assertIn("Biobío", order.shipment.address)

    def test_checkout_uses_builder_defaults_and_local_commune_catalog(self):
        from core.storefront_builder import normalize_checkout_config

        storefront = StorefrontSettings.objects.get(company=self.company)
        config = normalize_checkout_config(storefront)
        self.assertTrue(config["show_mascot"])
        self.assertEqual(config["shipping_provider"], "bluexpress")
        self.assertEqual(config["pickup_line_1"], "Bulnes 136, Local 16")

        self.client.post(
            reverse("storefront_cart", kwargs={"slug": self.company.slug}),
            {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "1"},
        )
        response = self.client.get(reverse("storefront_checkout", kwargs={"slug": self.company.slug}))
        self.assertContains(response, "checkout-communes-data")
        self.assertContains(response, "Talcahuano")
        self.assertContains(response, "ikiway-checkout-bunny.png", count=1)
        self.assertContains(response, "Editar carrito")

    def test_cart_ajax_recalculates_coupon_and_renders_product(self):
        cart_url = reverse("storefront_cart", kwargs={"slug": self.company.slug})
        self.client.post(cart_url, {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "2"})
        Promotion.objects.create(
            company=self.company,
            name="Prueba diez",
            code="PRUEBA10",
            discount_type=Promotion.DiscountType.PERCENT,
            value=10,
        )

        response = self.client.post(
            cart_url,
            {"action": "apply_coupon", "coupon_code": "prueba10"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(Decimal(payload["cart"]["discount"]), self.stock_item.variant.sale_price * Decimal("2") * Decimal("0.10"))
        self.assertContains(response, "Cable USB-C 1m")
        self.assertContains(response, "Codigo PRUEBA10 aplicado")

    def test_cart_ajax_cannot_update_variant_from_another_company(self):
        other_company = Company.objects.create(name="Otra tienda", slug="otra-tienda")
        other_branch = Branch.objects.create(company=other_company, name="Online", code="OTHER", is_online_store=True)
        other_product = Product.objects.create(company=other_company, name="Producto privado", slug="producto-privado", is_sellable_online=True)
        other_variant = ProductVariant.objects.create(product=other_product, name="Unico", sku="OTHER-001", sale_price=1000)
        StockItem.objects.create(branch=other_branch, variant=other_variant, quantity=4)
        cart_url = reverse("storefront_cart", kwargs={"slug": self.company.slug})

        response = self.client.post(
            cart_url,
            {"action": "update", "variant_id": other_variant.id, "quantity": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_cart_ajax_remove_and_clear_empty_the_session_cart(self):
        cart_url = reverse("storefront_cart", kwargs={"slug": self.company.slug})
        self.client.post(cart_url, {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "1"})
        headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_ACCEPT": "application/json"}

        cart_response = self.client.get(cart_url)
        self.assertContains(cart_response, "data-cart-clear-modal")
        self.assertContains(cart_response, "Si, vaciar carrito")
        self.assertContains(cart_response, "Eliminar Cable USB-C 1m del carrito")

        remove_response = self.client.post(
            cart_url,
            {"action": "remove", "variant_id": self.stock_item.variant_id},
            **headers,
        )
        self.assertEqual(remove_response.status_code, 200)
        self.assertEqual(remove_response.json()["cart"]["item_count"], 0)

        self.client.post(cart_url, {"action": "add", "stock_item_id": self.stock_item.id, "quantity": "1"})
        clear_response = self.client.post(cart_url, {"action": "clear"}, **headers)
        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(clear_response.json()["cart"]["item_count"], 0)

    @override_settings(ALLOWED_HOSTS=["demo.ikiway.test"])
    def test_storefront_can_load_by_custom_domain(self):
        StorefrontSettings.objects.filter(company=self.company).update(custom_domain="demo.ikiway.test")

        response = self.client.get("/", HTTP_HOST="demo.ikiway.test")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cable USB-C 1m")


class LoginRoutingTests(TestCase):
    def setUp(self):
        call_command("seed_initial_data", stdout=StringIO())

    def test_store_admin_login_redirect_goes_to_tenant_dashboard(self):
        user = get_user_model().objects.get(email="admin@ikiway.cl")
        self.client.force_login(user)

        response = self.client.get(reverse("login_redirect"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard/")

    def test_superuser_login_redirect_goes_to_saas_admin(self):
        user = get_user_model().objects.create_superuser(email="biobiohost@gmail.com", password="Admin12345")
        self.client.force_login(user)

        response = self.client.get(reverse("login_redirect"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/saas-admin/")

    def test_store_superuser_login_redirect_still_goes_to_tenant_dashboard(self):
        user = get_user_model().objects.get(email="admin@ikiway.cl")
        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(user)

        response = self.client.get(reverse("login_redirect"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard/")

    def test_django_admin_index_redirects_store_staff_to_dashboard(self):
        user = get_user_model().objects.get(email="admin@ikiway.cl")
        self.client.force_login(user)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("tenant_dashboard"))

    def test_django_admin_index_redirects_store_superuser_to_dashboard(self):
        user = get_user_model().objects.get(email="admin@ikiway.cl")
        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(user)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("tenant_dashboard"))

    def test_tenant_dashboard_loads(self):
        user = get_user_model().objects.get(email="admin@ikiway.cl")
        self.client.force_login(user)

        response = self.client.get(reverse("tenant_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Panel de tienda")
