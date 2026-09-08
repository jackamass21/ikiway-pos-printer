from datetime import timedelta
import json
from decimal import Decimal

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, Permission
from django.db import models, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.crypto import get_random_string

from accounts.decorators import saas_owner_required
from accounts.utils import dashboard_url_for_user
from catalog.models import Category, Product
from core.forms import (
    SaaSManualClientForm,
    SaaSClientForm,
    SaaSPlanForm,
    SaaSStorefrontSettingsForm,
    SaaSSubscriptionForm,
    SaaSUserPasswordResetForm,
    SiiApiConfigurationForm,
    SiiCafUploadForm,
    SiiCertificateUploadForm,
    SiiCompanySettingsForm,
    StorefrontProductDetailSettingsForm,
    StorefrontBuilderThemeForm,
    StorefrontSiteSettingsForm,
    StoreOnboardingForm,
    SupportMessageForm,
    TenantSupportMessageForm,
    TenantSupportTicketForm,
)
from core.storefront_builder import (
    CATEGORY_MENU_ICON_CHOICES,
    PRODUCT_CARD_DESKTOP_COLUMN_CHOICES,
    PRODUCT_CARD_IMAGE_FIT_CHOICES,
    PRODUCT_CARD_MOBILE_COLUMN_CHOICES,
    PRODUCT_CARD_STYLE_CHOICES,
    footer_newsletter_asset,
    hero_asset_editor_items,
    navigation_category_editor_items,
    normalize_cart_config,
    normalize_checkout_config,
    normalize_account_access_config,
    normalize_builder_sections,
    normalize_product_detail_config,
    serialize_builder_sections_from_post,
    social_asset_editor_items,
    store_presence_social_asset,
)
from payments.subscription_gateways import SubscriptionGatewayError, confirm_subscription_payment, create_subscription_checkout
from pos.models import PosConfiguration
from orders.models import Order
from inventory.models import StockItem
from sii.models import SiiApiConfiguration, SiiCompanySettings, SiiDocument
from sii.services.exceptions import SiiError
from sii.services.company_sync import sync_company_settings_from_api
from sii.services.external_api import SiiExternalApiClient
from sii.services.pdf import build_boleta_pdf
from sii.services import tax_dashboard
from tenancy.access import MODULE_CHOICES, is_tenant_owner, sanitized_module_access
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


def home(request):
    host = request.get_host().split(":")[0].lower()
    if StorefrontSettings.objects.filter(custom_domain=host, is_published=True, company__is_active=True).exists():
        from core.storefront import storefront_home

        return storefront_home(request)
    return redirect("storefront_home", slug="ikiway")


@login_required
def login_redirect(request):
    return redirect(dashboard_url_for_user(request.user))


@login_required
def tenant_logout(request):
    if request.method != "POST":
        return redirect("tenant_dashboard")
    logout(request)
    return redirect("admin:login")


@login_required
def tenant_dashboard(request):
    company = request.current_company
    if dashboard_url_for_user(request.user) == "/saas-admin/":
        return redirect("saas_admin_dashboard")
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    active_subscription = next((subscription for subscription in company.subscriptions.select_related("plan").all() if subscription.is_active), None)
    online_branch = company.branches.filter(is_online_store=True, is_active=True).first()
    context = {
        "company": company,
        "subscription": active_subscription,
        "branches": company.branches.filter(is_active=True).order_by("name"),
        "orders_today": Order.objects.filter(company=company).count(),
        "online_orders": Order.objects.filter(company=company, channel=Order.Channel.ONLINE).count(),
        "pos_orders": Order.objects.filter(company=company, channel=Order.Channel.POS).count(),
        "low_stock_count": StockItem.objects.filter(branch__company=company, quantity__lte=models.F("minimum_quantity")).count(),
        "storefront_url": reverse("storefront_home", kwargs={"slug": company.slug}) if online_branch else "",
    }
    return render(request, "core/tenant_dashboard.html", context)


@login_required
def sii_configuration_manager(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")
    if not (request.user.is_superuser or request.user.role in {"owner", "admin"}):
        messages.error(request, "No tienes permiso para configurar SII.")
        return redirect("tenant_dashboard")

    if request.method == "POST":
        response, context = _handle_sii_panel_post(request, company, "sii_configuration_manager")
        if response:
            return response
    else:
        context = _build_sii_panel_context(company)
    return render(request, "core/sii_configuration.html", context)


def _build_sii_panel_context(company, *, api_form=None, settings_form=None, certificate_form=None, caf_form=None):
    sii_settings = SiiCompanySettings.objects.filter(company=company).first()
    api_config = SiiApiConfiguration.objects.filter(company=company).first()
    return {
        "company": company,
        "sii_settings": sii_settings,
        "api_config": api_config,
        "api_form": api_form or SiiApiConfigurationForm(instance=api_config, company=company),
        "settings_form": settings_form or SiiCompanySettingsForm(instance=sii_settings, company=company),
        "certificate_form": certificate_form or SiiCertificateUploadForm(),
        "caf_form": caf_form or SiiCafUploadForm(),
        "dashboard": tax_dashboard(company),
        "documents": SiiDocument.objects.filter(company=company).select_related("branch", "order").order_by("-created_at")[:10],
    }


def _handle_sii_panel_post(request, company, redirect_name, **redirect_kwargs):
    sii_settings = SiiCompanySettings.objects.filter(company=company).first()
    api_config = SiiApiConfiguration.objects.filter(company=company).first()
    api_form = SiiApiConfigurationForm(instance=api_config, company=company)
    settings_form = SiiCompanySettingsForm(instance=sii_settings, company=company)
    certificate_form = SiiCertificateUploadForm()
    caf_form = SiiCafUploadForm()
    action = request.POST.get("action")

    if action == "save_api_configuration":
        api_form = SiiApiConfigurationForm(request.POST, instance=api_config, company=company)
        if api_form.is_valid():
            api_form.save()
            messages.success(request, "Configuracion de la API guardada. El token quedo cifrado.")
            return redirect(redirect_name, **redirect_kwargs), None
        messages.error(request, "Revisa la URL y el token de la API.")
    elif action == "test_api_connection":
        try:
            remote_company = SiiExternalApiClient(company).get_company()
        except SiiError as exc:
            messages.error(request, f"No pudimos conectar con la API: {exc}")
        else:
            messages.success(request, f"Conexion correcta con la API para {remote_company.get('legalName') or remote_company.get('fantasyName') or company.name}.")
        return redirect(redirect_name, **redirect_kwargs), None
    elif action == "sync_company_from_api":
        try:
            remote_company = SiiExternalApiClient(company).get_company()
            synced_settings = sync_company_settings_from_api(company, remote_company)
        except SiiError as exc:
            messages.error(request, f"No pudimos sincronizar la empresa desde la API: {exc}")
        else:
            messages.success(request, f"Empresa sincronizada desde la API: {synced_settings.legal_name} ({synced_settings.rut}).")
        return redirect(redirect_name, **redirect_kwargs), None
    elif action == "save_settings":
        settings_form = SiiCompanySettingsForm(request.POST, request.FILES, instance=sii_settings, company=company)
        if settings_form.is_valid():
            sii_settings = settings_form.save()
            try:
                SiiExternalApiClient(company).upsert_company(sii_settings)
            except SiiError as exc:
                messages.error(request, f"Datos guardados en Ikiway, pero no se pudieron sincronizar con la API: {exc}")
            else:
                messages.success(request, "Empresa tributaria guardada y sincronizada con la API.")
            return redirect(redirect_name, **redirect_kwargs), None
    elif action == "upload_certificate":
        certificate_form = SiiCertificateUploadForm(request.POST, request.FILES)
        if certificate_form.is_valid():
            try:
                SiiExternalApiClient(company).upload_certificate(
                    certificate_form.cleaned_data["certificate"],
                    password=certificate_form.cleaned_data["password"],
                    name=certificate_form.cleaned_data["name"],
                    active=certificate_form.cleaned_data["is_active"],
                )
            except SiiError as exc:
                certificate_form.add_error(None, str(exc))
                messages.error(request, f"La API rechazo el certificado: {exc}")
            else:
                messages.success(request, "Certificado enviado correctamente a la API.")
                return redirect(redirect_name, **redirect_kwargs), None
        else:
            messages.error(request, "Revisa el archivo y los datos del certificado.")
    elif action == "upload_caf":
        caf_form = SiiCafUploadForm(request.POST, request.FILES)
        if caf_form.is_valid():
            try:
                SiiExternalApiClient(company).upload_caf(
                    caf_form.cleaned_data["caf_file"],
                    name=caf_form.cleaned_data["name"],
                    active=caf_form.cleaned_data["is_active"],
                )
                messages.success(request, "CAF enviado correctamente a la API.")
                return redirect(redirect_name, **redirect_kwargs), None
            except SiiError as exc:
                caf_form.add_error("caf_file", str(exc))
                messages.error(request, f"La API rechazo el CAF: {exc}")
    else:
        messages.error(request, "Accion no reconocida.")

    return None, _build_sii_panel_context(company, api_form=api_form, settings_form=settings_form, certificate_form=certificate_form, caf_form=caf_form)


def _document_xml_response(document):
    xml = document.xml_signed or document.xml_unsigned
    if not xml:
        raise Http404("El documento aun no tiene XML generado.")
    case = (document.sii_response or {}).get("certification_case")
    folio = document.folio or document.id
    response = HttpResponse(xml.encode("ISO-8859-1"), content_type="application/xml; charset=ISO-8859-1")
    filename = f"boleta_certificacion_{case.lower().replace('-', '_')}_folio_{folio}.xml" if case else f"dte_{document.document_type}_folio_{folio}.xml"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _document_boleta_pdf_response(document):
    pdf = build_boleta_pdf(document)
    case = (document.sii_response or {}).get("certification_case")
    folio = document.folio or document.id
    response = HttpResponse(pdf, content_type="application/pdf")
    filename = f"boleta_certificacion_{case.lower().replace('-', '_')}_folio_{folio}.pdf" if case else f"boleta_electronica_folio_{folio}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def sii_document_xml_download(request, document_id):
    company = request.current_company
    document = get_object_or_404(SiiDocument, id=document_id, company=company)
    return _document_xml_response(document)


@login_required
def sii_document_boleta_download(request, document_id):
    company = request.current_company
    document = get_object_or_404(SiiDocument, id=document_id, company=company)
    return _document_boleta_pdf_response(document)


@login_required
def tenant_account_manager(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "update_profile":
                _update_current_user_profile(request)
                messages.success(request, "Tu cuenta fue actualizada.")
            elif action == "change_password":
                _change_current_user_password(request)
                messages.success(request, "Tu clave fue actualizada.")
            else:
                messages.error(request, "Accion no reconocida.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("tenant_account_manager")

    return render(request, "core/account_manager.html", {"company": company})


@login_required
def tenant_support_center(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")
    membership = _tenant_membership_for_user(company, request.user)
    if not is_tenant_owner(request.user, membership):
        messages.error(request, "Solo el dueño de la tienda puede acceder al soporte.")
        return redirect("tenant_dashboard")

    support_form = TenantSupportTicketForm()
    if request.method == "POST":
        support_form = TenantSupportTicketForm(request.POST)
        if support_form.is_valid():
            ticket = support_form.save(commit=False)
            ticket.company = company
            ticket.opened_by = request.user
            ticket.last_message_at = timezone.now()
            ticket.save()
            SupportMessage.objects.create(
                ticket=ticket,
                sender=request.user,
                body=support_form.cleaned_data["initial_message"],
                is_internal=False,
            )
            return redirect(f'{reverse("tenant_support_center")}?created={ticket.id}')
        messages.error(request, "Revisa los datos del ticket antes de enviarlo.")

    support_tickets = _client_support_tickets_queryset().filter(company=company)
    created_ticket = None
    created_ticket_id = request.GET.get("created", "")
    if created_ticket_id.isdigit():
        created_ticket = support_tickets.filter(id=int(created_ticket_id)).first()

    return render(
        request,
        "core/support_center.html",
        {
            "company": company,
            "support_form": support_form,
            "support_tickets": support_tickets,
            "created_ticket": created_ticket,
        },
    )


@login_required
def tenant_support_ticket_detail(request, ticket_id):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")
    membership = _tenant_membership_for_user(company, request.user)
    if not is_tenant_owner(request.user, membership):
        messages.error(request, "Solo el dueño de la tienda puede revisar tickets de soporte.")
        return redirect("tenant_dashboard")

    ticket = get_object_or_404(
        _client_support_tickets_queryset().select_related("company", "opened_by", "assigned_to"),
        id=ticket_id,
        company=company,
    )
    if request.method == "POST":
        form = TenantSupportMessageForm(request.POST)
        if form.is_valid():
            SupportMessage.objects.create(
                ticket=ticket,
                sender=request.user,
                body=form.cleaned_data["body"],
                is_internal=False,
            )
            ticket.last_message_at = timezone.now()
            if ticket.status in {SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED}:
                ticket.status = SupportTicket.Status.OPEN
            ticket.save(update_fields=["last_message_at", "status", "updated_at"])
            messages.success(request, "Respuesta enviada al equipo de soporte.")
            return redirect("tenant_support_ticket_detail", ticket_id=ticket.id)
    else:
        form = TenantSupportMessageForm()

    return render(
        request,
        "core/support_ticket_detail.html",
        {
            "company": company,
            "ticket": ticket,
            "messages_list": ticket.messages.filter(is_internal=False).select_related("sender"),
            "form": form,
        },
    )

@login_required
def tenant_user_manager(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    membership = _tenant_membership_for_user(company, request.user)
    if not is_tenant_owner(request.user, membership):
        messages.error(request, "Solo el dueño de la tienda puede administrar usuarios.")
        return redirect("tenant_dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create_user":
                _create_tenant_user_from_request(request, company)
                messages.success(request, "Usuario creado y asignado a la tienda.")
            elif action == "update_user":
                _update_tenant_user_from_request(request, company)
                messages.success(request, "Usuario de la tienda actualizado.")
            else:
                messages.error(request, "Accion no reconocida.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("tenant_user_manager")

    memberships = (
        TenantMembership.objects.filter(company=company)
        .select_related("user")
        .order_by("role", "user__email")
    )
    user_role_choices = [
        choice for choice in get_user_model().Role.choices
        if choice[0] != get_user_model().Role.OWNER
    ]
    return render(
        request,
        "core/user_manager.html",
        {
            "company": company,
            "membership": membership,
            "memberships": memberships,
            "module_choices": MODULE_CHOICES,
            "user_role_choices": user_role_choices,
        },
    )


def _tenant_membership_for_user(company, user):
    if not user.is_authenticated:
        return None
    return TenantMembership.objects.filter(company=company, user=user).first()


def _clean_text(request, field_name, max_length=150):
    return request.POST.get(field_name, "").strip()[:max_length]


def _clean_email(request):
    email = request.POST.get("email", "").strip().lower()
    if not email:
        raise ValueError("El correo es obligatorio.")
    return email


def _validate_unique_email(email, exclude_user=None):
    user_model = get_user_model()
    query = user_model.objects.filter(email=email)
    if exclude_user:
        query = query.exclude(id=exclude_user.id)
    if query.exists():
        raise ValueError("Ese correo ya esta usado por otro usuario.")


def _update_current_user_profile(request):
    user = request.user
    email = _clean_email(request)
    _validate_unique_email(email, exclude_user=user)
    user.email = email
    user.first_name = _clean_text(request, "first_name", 150)
    user.last_name = _clean_text(request, "last_name", 150)
    user.phone = _clean_text(request, "phone", 32)
    user.save(update_fields=["email", "first_name", "last_name", "phone"])


def _change_current_user_password(request):
    current_password = request.POST.get("current_password", "")
    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")
    if not request.user.check_password(current_password):
        raise ValueError("La clave actual no es correcta.")
    if len(password) < 8:
        raise ValueError("La nueva clave debe tener al menos 8 caracteres.")
    if password != password_confirm:
        raise ValueError("La confirmacion de clave no coincide.")
    request.user.set_password(password)
    request.user.save(update_fields=["password"])
    update_session_auth_hash(request, request.user)


def _create_tenant_user_from_request(request, company):
    user_model = get_user_model()
    email = _clean_email(request)
    _validate_unique_email(email)
    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")
    if len(password) < 8:
        raise ValueError("La clave debe tener al menos 8 caracteres.")
    if password != password_confirm:
        raise ValueError("La confirmacion de clave no coincide.")

    user_role = _valid_choice(request.POST.get("user_role"), user_model.Role.choices, user_model.Role.SELLER)
    if user_role == user_model.Role.OWNER:
        raise ValueError("No puedes crear otra cuenta propietaria.")
    module_access = sanitized_module_access(request.POST.getlist("modules"))
    is_active = request.POST.get("is_active") == "on"
    with transaction.atomic():
        user = user_model.objects.create_user(
            email=email,
            password=password,
            first_name=_clean_text(request, "first_name", 150),
            last_name=_clean_text(request, "last_name", 150),
            phone=_clean_text(request, "phone", 32),
            role=user_role,
            is_staff=True,
            is_active=is_active,
        )
        _sync_user_role_group(user, user_role)
        TenantMembership.objects.create(
            company=company,
            user=user,
            role=TenantMembership.Role.MEMBER,
            module_access=module_access,
            is_active=is_active,
        )


def _update_tenant_user_from_request(request, company):
    membership = get_object_or_404(
        TenantMembership.objects.select_related("user"),
        company=company,
        user_id=request.POST.get("user_id"),
    )
    user = membership.user
    if membership.role == TenantMembership.Role.OWNER:
        raise ValueError("La cuenta propietaria se administra desde Mi cuenta.")
    is_active = request.POST.get("is_active") == "on"

    email = _clean_email(request)
    _validate_unique_email(email, exclude_user=user)
    user_model = get_user_model()
    user_role = _valid_choice(request.POST.get("user_role"), user_model.Role.choices, user.role)
    if user_role == user_model.Role.OWNER:
        raise ValueError("No puedes asignar el rol de dueño.")
    module_access = sanitized_module_access(request.POST.getlist("modules"))
    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")
    if password:
        if len(password) < 8:
            raise ValueError("La nueva clave debe tener al menos 8 caracteres.")
        if password != password_confirm:
            raise ValueError("La confirmacion de clave no coincide.")

    with transaction.atomic():
        user.email = email
        user.first_name = _clean_text(request, "first_name", 150)
        user.last_name = _clean_text(request, "last_name", 150)
        user.phone = _clean_text(request, "phone", 32)
        user.role = user_role
        user.is_staff = True
        user.is_active = is_active
        if password:
            user.set_password(password)
        user.save()
        _sync_user_role_group(user, user_role)
        membership.module_access = module_access
        membership.is_active = is_active
        membership.save(update_fields=["module_access", "is_active", "updated_at"])


def _valid_choice(value, choices, default):
    valid_values = {choice_value for choice_value, _ in choices}
    return value if value in valid_values else default


def _sync_user_role_group(user, role):
    group_names = {
        get_user_model().Role.OWNER: "Duenos",
        get_user_model().Role.ADMIN: "Administradores",
        get_user_model().Role.SELLER: "Vendedores",
        get_user_model().Role.WAREHOUSE: "Bodega",
        get_user_model().Role.ACCOUNTANT: "Contabilidad",
    }
    group_name = group_names.get(role)
    user.groups.remove(*Group.objects.filter(name__in=group_names.values()))
    if not group_name:
        return
    group, _ = Group.objects.get_or_create(name=group_name)
    user.groups.add(group)


@login_required
def tenant_storefront_builder(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    storefront, _ = StorefrontSettings.objects.get_or_create(company=company, defaults={"display_name": company.name})
    if request.method == "POST":
        form = StorefrontBuilderThemeForm(request.POST, instance=storefront)
        if form.is_valid():
            storefront = form.save()
            _save_storefront_builder_assets(request, storefront)
            storefront.builder_sections = serialize_builder_sections_from_post(request.POST, storefront)
            storefront.cart_config = normalize_cart_config(
                storefront,
                {
                    "show_breadcrumb": "cart_show_breadcrumb" in request.POST,
                    "show_sku": "cart_show_sku" in request.POST,
                    "show_stock_status": "cart_show_stock_status" in request.POST,
                    "show_related_products": "cart_show_related_products" in request.POST,
                    "show_discount_code": "cart_show_discount_code" in request.POST,
                    "show_payment_methods": "cart_show_payment_methods" in request.POST,
                    "show_continue_shopping": "cart_show_continue_shopping" in request.POST,
                    "sticky_summary_desktop": "cart_sticky_summary_desktop" in request.POST,
                    "related_title": request.POST.get("cart_related_title", ""),
                    "related_limit": request.POST.get("cart_related_limit", "4"),
                },
            )
            update_fields = ["builder_sections", "cart_config", "updated_at"]
            if any(key.startswith("checkout_") for key in request.POST):
                checkout_layout = normalize_checkout_config(storefront, {
                    "show_breadcrumb": "checkout_show_breadcrumb" in request.POST,
                    "show_sku": "checkout_show_sku" in request.POST,
                    "show_product_images": "checkout_show_product_images" in request.POST,
                    "show_shipping_info": "checkout_show_shipping_info" in request.POST,
                    "show_help": "checkout_show_help" in request.POST,
                    "show_mascot": "checkout_show_mascot" in request.POST,
                    "sticky_summary_desktop": "checkout_sticky_summary_desktop" in request.POST,
                    "title": request.POST.get("checkout_title", ""),
                    "subtitle": request.POST.get("checkout_subtitle", ""),
                    "help_title": request.POST.get("checkout_help_title", ""),
                    "help_text": request.POST.get("checkout_help_text", ""),
                    "support_url": request.POST.get("checkout_support_url", ""),
                    "support_label": request.POST.get("checkout_support_label", ""),
                    "shipping_provider": request.POST.get("checkout_shipping_provider", ""),
                    "shipping_provider_label": request.POST.get("checkout_shipping_provider_label", ""),
                    "pickup_line_1": request.POST.get("checkout_pickup_line_1", ""),
                    "pickup_line_2": request.POST.get("checkout_pickup_line_2", ""),
                    "pickup_city": request.POST.get("checkout_pickup_city", ""),
                    "mascot_url": request.POST.get("checkout_mascot_url", ""),
                })
                storefront.checkout_form_config = {
                    **(storefront.checkout_form_config or {}),
                    "checkout_layout": checkout_layout,
                }
                storefront.shipping_config = {
                    **(storefront.shipping_config or {}),
                    "provider": checkout_layout["shipping_provider"],
                    "provider_label": checkout_layout["shipping_provider_label"],
                }
                storefront.pickup_enabled = "checkout_pickup_enabled" in request.POST
                storefront.delivery_enabled = "checkout_delivery_enabled" in request.POST
                update_fields.extend(["checkout_form_config", "shipping_config", "pickup_enabled", "delivery_enabled"])
            storefront.save(update_fields=update_fields)
            messages.success(request, "Diseno ecommerce actualizado.")
            return redirect("tenant_storefront_builder")
        messages.error(request, "No pudimos guardar el builder. Revisa los campos.")
    else:
        form = StorefrontBuilderThemeForm(instance=storefront)

    preview_products = (
        StockItem.objects.select_related("variant__product", "variant__product__category")
        .filter(
            branch__company=company,
            branch__is_online_store=True,
            variant__product__is_sellable_online=True,
            variant__product__is_active=True,
            variant__product__deleted_at__isnull=True,
        )
        .order_by("-variant__product__created_at", "variant__product__name")[:4]
    )
    showcase_products = (
        company.products.filter(is_active=True, is_sellable_online=True, deleted_at__isnull=True)
        .order_by("name")
    )
    normalized_sections = normalize_builder_sections(storefront)
    catalog_card_config = next(
        (section.config for section in normalized_sections if section.type == "catalog"),
        {},
    )

    return render(
        request,
        "core/storefront/builder_editor.html",
        {
            "company": company,
            "storefront": storefront,
            "form": form,
            "sections": normalized_sections,
            "catalog_card_config": catalog_card_config,
            "cart_config": normalize_cart_config(storefront, storefront.cart_config),
            "checkout_config": normalize_checkout_config(storefront),
            "account_access_config": normalize_account_access_config(storefront),
            "hero_assets": hero_asset_editor_items(storefront),
            "preview_products": preview_products,
            "showcase_products": showcase_products,
            "social_assets": social_asset_editor_items(storefront),
            "store_presence_asset": store_presence_social_asset(storefront),
            "footer_newsletter_asset": footer_newsletter_asset(storefront),
            "navigation_category_items": navigation_category_editor_items(storefront),
            "category_icon_choices": CATEGORY_MENU_ICON_CHOICES,
            "product_card_style_choices": PRODUCT_CARD_STYLE_CHOICES,
            "product_card_desktop_column_choices": PRODUCT_CARD_DESKTOP_COLUMN_CHOICES,
            "product_card_mobile_column_choices": PRODUCT_CARD_MOBILE_COLUMN_CHOICES,
            "product_card_image_fit_choices": PRODUCT_CARD_IMAGE_FIT_CHOICES,
            "storefront_url": reverse("storefront_home", kwargs={"slug": company.slug}),
        },
    )


@login_required
def tenant_product_detail_settings(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    storefront, _ = StorefrontSettings.objects.get_or_create(company=company, defaults={"display_name": company.name})
    if request.method == "POST":
        form = StorefrontProductDetailSettingsForm(request.POST, instance=storefront)
        if form.is_valid():
            form.save()
            messages.success(request, "Configuracion del detalle de producto actualizada.")
            return redirect("tenant_product_detail_settings")
        messages.error(request, "No pudimos guardar la configuracion. Revisa el formato seleccionado.")
    else:
        form = StorefrontProductDetailSettingsForm(instance=storefront)

    sample_product = (
        StockItem.objects.select_related("variant__product", "variant__product__category", "variant__product__brand")
        .filter(
            branch__company=company,
            branch__is_online_store=True,
            variant__is_active=True,
            variant__product__is_active=True,
            variant__product__is_sellable_online=True,
        )
        .order_by("variant__product__name")
        .first()
    )
    sample_url = ""
    if sample_product:
        sample_url = reverse("storefront_product_detail", kwargs={"slug": company.slug, "product_slug": sample_product.variant.product.slug})

    return render(
        request,
        "core/storefront/product_detail_settings.html",
        {
            "company": company,
            "storefront": storefront,
            "form": form,
            "detail_config": normalize_product_detail_config(storefront, storefront.product_detail_config),
            "sample_stock_item": sample_product,
            "sample_product_url": sample_url,
            "storefront_url": reverse("storefront_home", kwargs={"slug": company.slug}),
        },
    )


@login_required
def tenant_ecommerce_site_settings(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    storefront, _ = StorefrontSettings.objects.get_or_create(company=company, defaults={"display_name": company.name})
    if request.method == "POST":
        form = StorefrontSiteSettingsForm(request.POST, instance=storefront)
        if form.is_valid():
            form.save()
            messages.success(request, "Configuracion del sitio ecommerce actualizada.")
            return redirect("tenant_ecommerce_site_settings")
        messages.error(request, "No pudimos guardar la configuracion del sitio. Revisa los campos.")
    else:
        form = StorefrontSiteSettingsForm(instance=storefront)

    return render(
        request,
        "core/storefront/site_settings.html",
        {
            "company": company,
            "storefront": storefront,
            "form": form,
            "storefront_url": reverse("storefront_home", kwargs={"slug": company.slug}),
        },
    )


@login_required
def sales_channels_manager(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")

    storefront, _ = StorefrontSettings.objects.get_or_create(company=company, defaults={"display_name": company.name})
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "update_pos_channel":
                _update_pos_channel_from_request(request, company)
                messages.success(request, "Canal TPV actualizado.")
            elif action == "update_ecommerce_channel":
                _update_ecommerce_channel_from_request(request, company, storefront)
                messages.success(request, "Canal ecommerce actualizado.")
            elif action == "update_manual_channel":
                _update_manual_channel_from_request(request, company)
                messages.success(request, "Canal manual actualizado.")
            else:
                messages.error(request, "Accion no reconocida.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("sales_channels_manager")

    physical_branches = Branch.objects.filter(company=company, is_online_store=False).order_by("name")
    online_branches = Branch.objects.filter(company=company, is_online_store=True).order_by("name")
    primary_physical_branch = physical_branches.first()
    online_branch = online_branches.first()
    pos_config = None
    if primary_physical_branch:
        pos_config, _ = PosConfiguration.objects.get_or_create(branch=primary_physical_branch)

    pos_products = Product.objects.filter(company=company, is_active=True, is_sellable_pos=True, deleted_at__isnull=True).count()
    ecommerce_products = Product.objects.filter(company=company, is_active=True, is_sellable_online=True, deleted_at__isnull=True).count()
    total_products = Product.objects.filter(company=company, is_active=True, deleted_at__isnull=True).count()
    pos_orders = Order.objects.filter(company=company, channel=Order.Channel.POS).count()
    ecommerce_orders = Order.objects.filter(company=company, channel=Order.Channel.ONLINE).count()
    return render(
        request,
        "core/sales_channels.html",
        {
            "company": company,
            "storefront": storefront,
            "physical_branches": physical_branches,
            "online_branches": online_branches,
            "primary_physical_branch": primary_physical_branch,
            "online_branch": online_branch,
            "pos_config": pos_config,
            "pos_products": pos_products,
            "ecommerce_products": ecommerce_products,
            "total_products": total_products,
            "pos_orders": pos_orders,
            "ecommerce_orders": ecommerce_orders,
            "manual_enabled": bool(company.contact_phone),
            "receipt_width_choices": PosConfiguration.ReceiptPaperWidth.choices,
        },
    )


@login_required
def pos_configuration_manager(request):
    company = request.current_company
    if not company:
        messages.error(request, "Tu usuario no tiene una tienda asignada.")
        return redirect("home")
    if not (request.user.is_superuser or request.user.role in {"owner", "admin"}):
        messages.error(request, "No tienes permiso para configurar el TPV.")
        return redirect("pos:terminal")

    branches = Branch.objects.filter(company=company, is_online_store=False).order_by("name")
    branch = branches.filter(id=request.POST.get("branch_id") or request.GET.get("branch")).first() or branches.first()
    if not branch:
        messages.error(request, "Crea una sucursal fisica antes de configurar el TPV.")
        return redirect("sales_channels_manager")

    config, _ = PosConfiguration.objects.get_or_create(branch=branch)
    if request.method == "POST":
        try:
            config = _update_pos_configuration_from_request(request, config)
            messages.success(request, "Configuracion del TPV actualizada.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect(f"{reverse('pos_configuration_manager')}?branch={config.branch_id}")

    return render(
        request,
        "core/pos_configuration.html",
        {
            "company": company,
            "branches": branches,
            "branch": branch,
            "config": config,
            "receipt_width_choices": PosConfiguration.ReceiptPaperWidth.choices,
            "cash_quick_amounts": ", ".join(str(amount) for amount in config.get_cash_quick_amounts()),
        },
    )


def _update_pos_channel_from_request(request, company):
    branch = Branch.objects.filter(company=company, is_online_store=False, id=request.POST.get("branch_id")).first()
    if not branch:
        raise ValueError("Selecciona una sucursal fisica valida.")
    branch.is_active = request.POST.get("is_active") == "on"
    branch.save(update_fields=["is_active", "updated_at"])
    config, _ = PosConfiguration.objects.get_or_create(branch=branch)
    receipt_width = request.POST.get("receipt_paper_width", PosConfiguration.ReceiptPaperWidth.MM_80)
    if receipt_width not in dict(PosConfiguration.ReceiptPaperWidth.choices):
        raise ValueError("Formato de ticket invalido.")
    config.receipt_paper_width = receipt_width
    config.cash_quick_amounts = _parse_quick_amounts(request.POST.get("cash_quick_amounts", ""))
    config.save(update_fields=["receipt_paper_width", "cash_quick_amounts", "updated_at"])


def _update_pos_configuration_from_request(request, config):
    branch = Branch.objects.filter(company=config.branch.company, is_online_store=False, id=request.POST.get("branch_id")).first()
    if not branch:
        raise ValueError("Selecciona una sucursal TPV valida.")
    if branch != config.branch:
        config, _ = PosConfiguration.objects.get_or_create(branch=branch)

    receipt_width = request.POST.get("receipt_paper_width", PosConfiguration.ReceiptPaperWidth.MM_80)
    if receipt_width not in dict(PosConfiguration.ReceiptPaperWidth.choices):
        raise ValueError("Formato de ticket invalido.")

    config.branch.is_active = request.POST.get("branch_is_active") == "on"
    config.branch.save(update_fields=["is_active", "updated_at"])
    config.receipt_paper_width = receipt_width
    config.cash_quick_amounts = _parse_quick_amounts(request.POST.get("cash_quick_amounts", ""))
    config.primary_color = _hex_color_from_request(request, "primary_color", "#2457f5")
    config.secondary_color = _hex_color_from_request(request, "secondary_color", config.primary_color)
    config.accent_color = _hex_color_from_request(request, "accent_color", "#16a34a")
    config.allow_cash_opening = request.POST.get("allow_cash_opening") == "on"
    config.show_product_images = request.POST.get("show_product_images") == "on"
    config.require_supervisor_close_code = request.POST.get("require_supervisor_close_code") == "on"
    config.require_supervisor_return_code = request.POST.get("require_supervisor_return_code") == "on"
    config.require_supervisor_discount_code = request.POST.get("require_supervisor_discount_code") == "on"
    supervisor_close_code = request.POST.get("supervisor_close_code", "").strip()
    supervisor_return_code = request.POST.get("supervisor_return_code", "").strip()
    supervisor_discount_code = request.POST.get("supervisor_discount_code", "").strip()
    if not (supervisor_close_code.isdigit() and len(supervisor_close_code) == 4):
        raise ValueError("El codigo de supervisor para cierre debe tener 4 digitos.")
    if not (supervisor_return_code.isdigit() and len(supervisor_return_code) == 4):
        raise ValueError("El codigo de supervisor para devoluciones debe tener 4 digitos.")
    if not (supervisor_discount_code.isdigit() and len(supervisor_discount_code) == 4):
        raise ValueError("El codigo de supervisor para descuentos debe tener 4 digitos.")
    try:
        threshold = Decimal(str(request.POST.get("supervisor_discount_threshold_percent", "50")).replace(",", "."))
    except Exception as exc:
        raise ValueError("El umbral de descuento debe ser un numero valido.") from exc
    if threshold < 0 or threshold > 100:
        raise ValueError("El umbral de descuento debe estar entre 0 y 100%.")
    config.supervisor_close_code = supervisor_close_code
    config.supervisor_return_code = supervisor_return_code
    config.supervisor_discount_code = supervisor_discount_code
    config.supervisor_discount_threshold_percent = threshold
    config.ticket_header = request.POST.get("ticket_header", "").strip()[:160]
    config.ticket_footer = request.POST.get("ticket_footer", "").strip()[:220]
    print_method = request.POST.get("print_method", config.print_method)
    if print_method not in dict(PosConfiguration.PrintMethod.choices):
        raise ValueError("Metodo de impresion invalido.")
    config.print_method = print_method
    config.printer_name = request.POST.get("printer_name", "").strip()[:120]
    try:
        config.print_copies = max(1, min(5, int(request.POST.get("print_copies", "1"))))
    except (TypeError, ValueError) as exc:
        raise ValueError("Las copias de impresion deben ser un numero entre 1 y 5.") from exc
    config.auto_print_receipt = request.POST.get("auto_print_receipt") == "on"
    config.show_change_on_receipt = request.POST.get("show_change_on_receipt") == "on"
    config.save(
        update_fields=[
            "receipt_paper_width",
            "cash_quick_amounts",
            "primary_color",
            "secondary_color",
            "accent_color",
            "allow_cash_opening",
            "show_product_images",
            "require_supervisor_close_code",
            "supervisor_close_code",
            "require_supervisor_return_code",
            "supervisor_return_code",
            "require_supervisor_discount_code",
            "supervisor_discount_code",
            "supervisor_discount_threshold_percent",
            "ticket_header",
            "ticket_footer",
            "printer_name",
            "print_method",
            "print_copies",
            "auto_print_receipt",
            "show_change_on_receipt",
            "updated_at",
        ]
    )
    return config


def _update_ecommerce_channel_from_request(request, company, storefront):
    branch = Branch.objects.filter(company=company, is_online_store=True, id=request.POST.get("branch_id")).first()
    if not branch:
        raise ValueError("Selecciona una sucursal online valida.")
    branch.is_active = request.POST.get("branch_is_active") == "on"
    branch.save(update_fields=["is_active", "updated_at"])
    storefront.is_published = request.POST.get("is_published") == "on"
    storefront.pickup_enabled = request.POST.get("pickup_enabled") == "on"
    storefront.delivery_enabled = request.POST.get("delivery_enabled") == "on"
    storefront.default_shipping_price = _decimal_from_request(request, "default_shipping_price")
    storefront.save(update_fields=["is_published", "pickup_enabled", "delivery_enabled", "default_shipping_price", "updated_at"])


def _update_manual_channel_from_request(request, company):
    company.contact_phone = request.POST.get("contact_phone", "").strip()
    company.contact_email = request.POST.get("contact_email", "").strip()
    company.save(update_fields=["contact_phone", "contact_email", "updated_at"])


def _parse_quick_amounts(raw_value):
    amounts = []
    for chunk in raw_value.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            amount = int(chunk)
        except ValueError:
            raise ValueError("Los montos rapidos deben ser numeros separados por coma.")
        if amount > 0:
            amounts.append(amount)
    return amounts or [1000, 2000, 5000, 10000, 20000]


def _decimal_from_request(request, field_name):
    raw_value = request.POST.get(field_name, "0")
    try:
        value = Decimal(str(raw_value or "0"))
    except Exception as exc:
        raise ValueError(f"{field_name} debe ser numerico.") from exc
    if value < 0:
        raise ValueError(f"{field_name} debe ser mayor o igual a 0.")
    return value


def _hex_color_from_request(request, field_name, default):
    value = request.POST.get(field_name, default).strip()
    if len(value) != 7 or not value.startswith("#") or any(char not in "0123456789abcdefABCDEF" for char in value[1:]):
        raise ValueError("Los colores del TPV deben estar en formato hexadecimal, por ejemplo #2457f5.")
    return value.lower()


def _save_storefront_builder_assets(request, storefront):
    existing_assets = storefront.builder_assets.filter(placement=StorefrontBuilderAsset.Placement.HERO)
    delete_ids = {int(asset_id) for asset_id in request.POST.getlist("delete_hero_asset_ids") if str(asset_id).isdigit()}
    if delete_ids:
        existing_assets.filter(id__in=delete_ids).delete()

    for asset in existing_assets.exclude(id__in=delete_ids):
        asset.alt_text = request.POST.get(f"hero_asset_alt_{asset.id}", "").strip()
        try:
            asset.sort_order = int(request.POST.get(f"hero_asset_sort_{asset.id}", asset.sort_order))
        except (TypeError, ValueError):
            pass
        asset.is_active = request.POST.get(f"hero_asset_active_{asset.id}") == "on"
        asset.save(update_fields=["alt_text", "sort_order", "is_active", "updated_at"])

    next_order = (existing_assets.exclude(id__in=delete_ids).order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 10
    uploaded_files = request.FILES.getlist("hero_images")
    for image in uploaded_files:
        if not _builder_image_is_valid(request, image):
            continue
        StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.HERO,
            image=image,
            alt_text=request.POST.get("section_hero_title", "").strip() or storefront.display_name or storefront.company.name,
            sort_order=next_order,
        )
        next_order += 10

    social_assets = storefront.builder_assets.filter(placement=StorefrontBuilderAsset.Placement.SOCIAL_GALLERY)
    delete_social_ids = {
        int(asset_id) for asset_id in request.POST.getlist("delete_social_asset_ids") if str(asset_id).isdigit()
    }
    if delete_social_ids:
        social_assets.filter(id__in=delete_social_ids).delete()

    for asset in social_assets.exclude(id__in=delete_social_ids):
        asset.alt_text = request.POST.get(f"social_asset_alt_{asset.id}", "").strip()
        asset.sort_order = _safe_int(request.POST.get(f"social_asset_sort_{asset.id}"), asset.sort_order)
        asset.is_active = request.POST.get(f"social_asset_active_{asset.id}") == "on"
        asset.save(update_fields=["alt_text", "sort_order", "is_active", "updated_at"])

    next_social_order = (
        social_assets.exclude(id__in=delete_social_ids).order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
    ) + 10
    for image in request.FILES.getlist("social_gallery_images"):
        if not _builder_image_is_valid(request, image):
            continue
        StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.SOCIAL_GALLERY,
            image=image,
            alt_text=storefront.display_name or storefront.company.name,
            sort_order=next_social_order,
        )
        next_social_order += 10

    presence_assets = storefront.builder_assets.filter(
        placement=StorefrontBuilderAsset.Placement.STORE_PRESENCE_SOCIAL
    )
    delete_presence_ids = {
        int(asset_id)
        for asset_id in request.POST.getlist("delete_store_presence_asset_ids")
        if str(asset_id).isdigit()
    }
    if delete_presence_ids:
        presence_assets.filter(id__in=delete_presence_ids).delete()
    for asset in presence_assets.exclude(id__in=delete_presence_ids):
        asset.alt_text = request.POST.get(
            f"store_presence_asset_alt_{asset.id}", asset.alt_text
        ).strip()
        asset.save(update_fields=["alt_text", "updated_at"])

    presence_image = request.FILES.get("store_presence_social_image")
    if presence_image and _builder_image_is_valid(request, presence_image):
        presence_assets.exclude(id__in=delete_presence_ids).update(is_active=False)
        StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.STORE_PRESENCE_SOCIAL,
            image=presence_image,
            alt_text=request.POST.get("section_store_presence_social_handle", "").strip()
            or storefront.display_name
            or storefront.company.name,
        )

    newsletter_assets = storefront.builder_assets.filter(
        placement=StorefrontBuilderAsset.Placement.FOOTER_NEWSLETTER
    )
    delete_newsletter_ids = {
        int(asset_id)
        for asset_id in request.POST.getlist("delete_footer_newsletter_asset_ids")
        if str(asset_id).isdigit()
    }
    if delete_newsletter_ids:
        newsletter_assets.filter(id__in=delete_newsletter_ids).delete()

    newsletter_image = request.FILES.get("footer_newsletter_image")
    if newsletter_image and _builder_image_is_valid(request, newsletter_image):
        newsletter_assets.exclude(id__in=delete_newsletter_ids).update(is_active=False)
        StorefrontBuilderAsset.objects.create(
            storefront=storefront,
            placement=StorefrontBuilderAsset.Placement.FOOTER_NEWSLETTER,
            image=newsletter_image,
            alt_text=request.POST.get("section_footer_newsletter_image_alt", "").strip()
            or storefront.display_name
            or storefront.company.name,
        )


def _builder_image_is_valid(request, image):
    if getattr(image, "size", 0) > 5 * 1024 * 1024:
        messages.warning(request, f"{image.name} supera 5MB y no fue subida.")
        return False
    if getattr(image, "content_type", "") not in {"image/jpeg", "image/png", "image/webp"}:
        messages.warning(request, f"{image.name} no es una imagen JPG, PNG o WEBP valida.")
        return False
    extension = str(getattr(image, "name", "")).lower().rsplit(".", 1)[-1]
    if extension not in {"jpg", "jpeg", "png", "webp"}:
        messages.warning(request, f"{image.name} no tiene una extension de imagen permitida.")
        return False
    return True


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def onboarding(request):
    plan = get_object_or_404(Plan, slug=request.GET.get("plan", ""), is_active=True)
    initial = {"plan": plan}

    if request.method == "POST":
        form = StoreOnboardingForm(request.POST)
        if form.is_valid():
            try:
                checkout_url = start_subscription_checkout(request, form)
            except SubscriptionGatewayError as exc:
                messages.error(request, str(exc))
            else:
                return redirect(checkout_url)
    else:
        form = StoreOnboardingForm(initial=initial)

    return render(request, "core/onboarding.html", {"form": form, "plan": plan})


def onboarding_success(request):
    result = request.session.pop("onboarding_result", None)
    if not result:
        return redirect("home")

    onboarding_record = get_object_or_404(
        StoreOnboarding.objects.select_related("company", "plan", "subscription"),
        id=result["onboarding_id"],
    )
    return render(
        request,
        "core/onboarding_success.html",
        {
            "onboarding": onboarding_record,
            "owner_email": result["owner_email"],
            "owner_password": result["owner_password"],
            "is_new_user": result["is_new_user"],
        },
    )


@transaction.atomic
def start_subscription_checkout(request, form):
    onboarding_record = form.save_onboarding()
    onboarding_record.status = StoreOnboarding.Status.PAYMENT_PENDING
    onboarding_record.save(update_fields=["status", "updated_at"])
    payment = SubscriptionPayment.objects.create(
        onboarding=onboarding_record,
        provider=form.cleaned_data["payment_provider"],
        amount=onboarding_record.plan.price,
        currency="CLP",
    )
    checkout = create_subscription_checkout(request, payment)
    payment.provider = checkout.provider
    payment.external_id = checkout.external_id
    payment.external_reference = checkout.external_reference
    payment.checkout_url = checkout.checkout_url
    payment.raw_request = checkout.raw_request or {}
    payment.raw_response = checkout.raw_response or {}
    payment.save(
        update_fields=[
            "provider",
            "external_id",
            "external_reference",
            "checkout_url",
            "raw_request",
            "raw_response",
            "updated_at",
        ]
    )
    return checkout.checkout_url


@csrf_exempt
def subscription_payment_return(request, payment_id):
    payment = get_object_or_404(
        SubscriptionPayment.objects.select_related("onboarding", "onboarding__plan", "subscription"),
        id=payment_id,
    )
    if payment.status == SubscriptionPayment.Status.PAID and payment.subscription:
        return _store_onboarding_session(request, payment.onboarding, "", False)

    confirmation = confirm_subscription_payment(payment, request)
    payment.status = confirmation["status"]
    payment.raw_response = {**payment.raw_response, "confirmation": confirmation.get("raw_response", {})}
    if confirmation["paid"]:
        payment.confirmed_at = timezone.now()
        onboarding_record, owner_password, is_new_user = activate_store_onboarding(payment.onboarding, payment)
        return _store_onboarding_session(request, onboarding_record, owner_password, is_new_user)

    payment.save(update_fields=["status", "raw_response", "updated_at"])
    messages.error(request, "No pudimos confirmar el pago de la suscripcion. Puedes intentarlo nuevamente.")
    return redirect(f"{reverse('onboarding')}?plan={payment.onboarding.plan.slug}")


@csrf_exempt
def subscription_payment_webhook(request, provider):
    if provider != SubscriptionPayment.Provider.MERCADOPAGO:
        return JsonResponse({"status": "ignored"})

    payment_id = request.GET.get("data.id") or request.GET.get("id")
    if not payment_id and request.body:
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        payment_id = str(payload.get("data", {}).get("id") or payload.get("id") or "")
    if not payment_id or not settings.MERCADOPAGO_ACCESS_TOKEN:
        return JsonResponse({"status": "ignored"})

    response = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {settings.MERCADOPAGO_ACCESS_TOKEN}"},
        timeout=15,
    )
    data = response.json() if response.content else {}
    external_reference = data.get("external_reference", "")
    if not external_reference.startswith("subscription-payment-"):
        return JsonResponse({"status": "ignored", "provider_status": data.get("status", "")})

    local_payment_id = external_reference.removeprefix("subscription-payment-")
    payment = get_object_or_404(
        SubscriptionPayment.objects.select_related("onboarding", "onboarding__plan", "subscription"),
        id=local_payment_id,
    )
    payment.raw_response = {**payment.raw_response, "webhook": data}
    if data.get("status") == "approved":
        payment.status = SubscriptionPayment.Status.PAID
        payment.confirmed_at = timezone.now()
        activate_store_onboarding(payment.onboarding, payment)
    else:
        payment.status = SubscriptionPayment.Status.PENDING
        payment.save(update_fields=["status", "raw_response", "updated_at"])
    return JsonResponse({"status": "ok"})


def subscription_blocked(request):
    return render(request, "core/subscription_blocked.html")


def _store_onboarding_session(request, onboarding_record, owner_password, is_new_user):
    request.session["onboarding_result"] = {
        "onboarding_id": onboarding_record.id,
        "owner_email": onboarding_record.owner_email,
        "owner_password": owner_password,
        "is_new_user": is_new_user,
    }
    messages.success(request, "Pago confirmado. Tienda activada.")
    return redirect("onboarding_success")


@transaction.atomic
def activate_store_onboarding(onboarding_record, payment):
    if onboarding_record.status == StoreOnboarding.Status.ACTIVATED and onboarding_record.subscription:
        payment.status = SubscriptionPayment.Status.PAID
        payment.subscription = onboarding_record.subscription
        payment.confirmed_at = payment.confirmed_at or timezone.now()
        payment.save(update_fields=["status", "subscription", "confirmed_at", "raw_response", "updated_at"])
        return onboarding_record, "", False

    plan = onboarding_record.plan
    user_model = get_user_model()
    owner_password = ""

    company = Company.objects.create(
        name=onboarding_record.store_name,
        slug=onboarding_record.desired_slug,
        legal_name=onboarding_record.legal_name,
        tax_id=onboarding_record.tax_id,
        contact_email=onboarding_record.owner_email,
        contact_phone=onboarding_record.owner_phone,
        is_active=True,
    )
    physical_branch = Branch.objects.create(
        company=company,
        name="Tienda Principal",
        code="STORE-001",
        address=onboarding_record.store_address,
        city=onboarding_record.store_city,
        is_online_store=False,
        is_active=True,
    )
    Branch.objects.create(
        company=company,
        name="Tienda Online",
        code="ONLINE",
        address="Inventario ecommerce",
        city=onboarding_record.store_city,
        is_online_store=True,
        is_active=True,
    )
    StorefrontSettings.objects.create(
        company=company,
        display_name=company.name,
        tagline="Compra online y retira o recibe tu pedido.",
        pickup_enabled=True,
        delivery_enabled=True,
        is_published=True,
    )
    PosConfiguration.objects.get_or_create(branch=physical_branch)

    owner = user_model.objects.filter(email=onboarding_record.owner_email.lower()).first()
    is_new_user = owner is None
    if is_new_user:
        owner_password = get_random_string(12)
        first_name, last_name = _split_owner_name(onboarding_record.owner_name)
        owner = user_model.objects.create_user(
            email=onboarding_record.owner_email.lower(),
            password=owner_password,
            first_name=first_name,
            last_name=last_name,
            phone=onboarding_record.owner_phone,
            role=user_model.Role.OWNER,
            is_staff=True,
        )
    admin_group, _ = Group.objects.get_or_create(name="Administradores")
    if not admin_group.permissions.exists():
        admin_group.permissions.set(Permission.objects.all())
    owner.groups.add(admin_group)

    TenantMembership.objects.create(company=company, user=owner, role=TenantMembership.Role.OWNER)

    now = timezone.now()
    period_days = 365 if plan.billing_period == Plan.BillingPeriod.YEARLY else 30
    subscription = Subscription.objects.create(
        company=company,
        plan=plan,
        status=Subscription.Status.ACTIVE,
        payment_mode=payment.provider,
        started_at=now,
        current_period_start=now,
        current_period_end=now + timedelta(days=period_days),
        external_reference=payment.external_reference,
    )

    onboarding_record.company = company
    onboarding_record.owner = owner
    onboarding_record.subscription = subscription
    onboarding_record.status = StoreOnboarding.Status.ACTIVATED
    onboarding_record.simulated_payment_reference = subscription.external_reference
    onboarding_record.save(
        update_fields=[
            "company",
            "owner",
            "subscription",
            "status",
            "simulated_payment_reference",
            "updated_at",
        ]
    )
    payment.status = SubscriptionPayment.Status.PAID
    payment.subscription = subscription
    payment.confirmed_at = payment.confirmed_at or now
    payment.save(update_fields=["status", "subscription", "confirmed_at", "raw_response", "updated_at"])
    return onboarding_record, owner_password, is_new_user


def _split_owner_name(full_name):
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _client_support_tickets_queryset():
    return (
        SupportTicket.objects.filter(
            opened_by__tenant_memberships__company=models.F("company"),
            opened_by__tenant_memberships__is_active=True,
        )
        .select_related("company", "opened_by", "assigned_to")
        .distinct()
    )


@saas_owner_required
def saas_admin_dashboard(request):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    active_clients = Company.objects.filter(
        is_active=True,
        subscriptions__status__in=[Subscription.Status.TRIALING, Subscription.Status.ACTIVE],
    ).prefetch_related("subscriptions__plan").distinct()
    paid_payments = SubscriptionPayment.objects.filter(status=SubscriptionPayment.Status.PAID)
    failed_payments = SubscriptionPayment.objects.filter(status=SubscriptionPayment.Status.FAILED)
    pending_onboardings = StoreOnboarding.objects.filter(status__in=[StoreOnboarding.Status.STARTED, StoreOnboarding.Status.PAYMENT_PENDING])
    open_tickets = _client_support_tickets_queryset().exclude(status__in=[SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED])

    context = {
        "metrics": {
            "active_clients": active_clients.count(),
            "total_clients": Company.objects.count(),
            "mrr": _calculate_mrr(active_clients),
            "month_revenue": paid_payments.filter(confirmed_at__gte=month_start).aggregate(total=Coalesce(Sum("amount"), Decimal("0")))["total"],
            "failed_payments": failed_payments.count(),
            "pending_onboardings": pending_onboardings.count(),
            "open_tickets": open_tickets.count(),
            "new_clients": Company.objects.filter(created_at__gte=month_start).count(),
        },
        "clients": Company.objects.prefetch_related("subscriptions", "memberships").order_by("-created_at")[:8],
        "plans": Plan.objects.annotate(subscription_count=Count("subscriptions", filter=Q(subscriptions__status=Subscription.Status.ACTIVE))).order_by("sort_order", "price"),
        "recent_payments": SubscriptionPayment.objects.select_related("onboarding", "subscription", "subscription__company").order_by("-created_at")[:8],
        "alerts": _build_saas_alerts(failed_payments, pending_onboardings, open_tickets),
        "tickets": open_tickets.select_related("company", "assigned_to").order_by("-last_message_at", "-created_at")[:8],
        "messages_list": SupportMessage.objects.select_related("ticket", "ticket__company", "sender").order_by("-created_at")[:8],
    }
    return render(request, "core/saas_admin/dashboard.html", context)


@saas_owner_required
def saas_admin_plans(request):
    plans = Plan.objects.annotate(subscription_count=Count("subscriptions", filter=Q(subscriptions__status=Subscription.Status.ACTIVE))).order_by("sort_order", "price")
    return render(request, "core/saas_admin/plans.html", {"plans": plans})


@saas_owner_required
def saas_admin_plan_edit(request, plan_id):
    plan = get_object_or_404(Plan, id=plan_id)
    if request.method == "POST":
        form = SaaSPlanForm(request.POST, instance=plan)
        if form.is_valid():
            form.save()
            messages.success(request, "Plan actualizado. El home ya muestra la nueva informacion.")
            return redirect("saas_admin_plans")
        messages.error(request, "No se pudo actualizar el plan. Revisa los datos.")
    else:
        form = SaaSPlanForm(instance=plan)
    return render(request, "core/saas_admin/plan_form.html", {"form": form, "plan": plan})


@saas_owner_required
def saas_admin_clients(request):
    clients = (
        Company.objects.prefetch_related("subscriptions", "memberships", "branches")
        .annotate(
            ticket_count=Count("support_tickets", distinct=True),
            sii_caf_count=Count("sii_cafs", distinct=True),
            sii_certificate_count=Count("sii_certificates", distinct=True),
            sii_document_count=Count("sii_documents", distinct=True),
        )
        .order_by("-created_at")
    )
    return render(request, "core/saas_admin/clients.html", {"clients": clients})


@saas_owner_required
def saas_admin_client_create(request):
    generated_password = ""
    if request.method == "POST":
        form = SaaSManualClientForm(request.POST)
        if form.is_valid():
            company, generated_password = _create_manual_saas_client(request, form.cleaned_data)
            if generated_password:
                messages.success(request, f"Cliente creado. Owner: {form.cleaned_data['owner_email']} / clave: {generated_password}")
            else:
                messages.success(request, f"Cliente creado y owner existente asignado: {form.cleaned_data['owner_email']}.")
            return redirect("saas_admin_client_detail", company_id=company.id)
        messages.error(request, "No se pudo crear el cliente. Revisa los datos.")
    else:
        form = SaaSManualClientForm()
    return render(request, "core/saas_admin/client_create.html", {"form": form, "generated_password": generated_password})


@transaction.atomic
def _create_manual_saas_client(request, data):
    user_model = get_user_model()
    owner_password = data.get("owner_password") or get_random_string(12)
    contact_email = data.get("contact_email") or data["owner_email"]
    contact_phone = data.get("contact_phone") or data.get("owner_phone", "")
    company = Company.objects.create(
        name=data["store_name"],
        slug=data["desired_slug"],
        legal_name=data.get("legal_name", ""),
        tax_id=data.get("tax_id", ""),
        contact_email=contact_email,
        contact_phone=contact_phone,
        is_active=data["status"] in {Subscription.Status.TRIALING, Subscription.Status.ACTIVE},
    )
    physical_branch = Branch.objects.create(
        company=company,
        name=data["physical_branch_name"],
        code="STORE-001",
        address=data.get("store_address", ""),
        city=data.get("store_city", ""),
        is_online_store=False,
        is_active=True,
    )
    Branch.objects.create(
        company=company,
        name="Tienda Online",
        code="ONLINE",
        address="Inventario ecommerce",
        city=data.get("store_city", ""),
        is_online_store=True,
        is_active=True,
    )
    StorefrontSettings.objects.create(
        company=company,
        display_name=company.name,
        tagline="Compra online y retira o recibe tu pedido.",
        pickup_enabled=True,
        delivery_enabled=True,
        is_published=data.get("is_published", False),
    )
    PosConfiguration.objects.get_or_create(branch=physical_branch)

    owner = user_model.objects.filter(email=data["owner_email"]).first()
    visible_password = ""
    if owner:
        if data.get("owner_password"):
            owner.set_password(owner_password)
            owner.save(update_fields=["password"])
            visible_password = owner_password
    else:
        first_name, last_name = _split_owner_name(data.get("owner_name", ""))
        owner = user_model.objects.create_user(
            email=data["owner_email"],
            password=owner_password,
            first_name=first_name,
            last_name=last_name,
            phone=data.get("owner_phone", ""),
            role=user_model.Role.OWNER,
            is_staff=True,
        )
        visible_password = owner_password

    admin_group, _ = Group.objects.get_or_create(name="Administradores")
    if not admin_group.permissions.exists():
        admin_group.permissions.set(Permission.objects.all())
    owner.groups.add(admin_group)
    TenantMembership.objects.create(company=company, user=owner, role=TenantMembership.Role.OWNER, is_active=True)

    now = timezone.now()
    period_end = now + timedelta(days=30 * data["months"])
    Subscription.objects.create(
        company=company,
        plan=data["plan"],
        status=data["status"],
        payment_mode=Subscription.PaymentMode.SIMULATED,
        started_at=now,
        current_period_start=now,
        current_period_end=period_end,
        external_reference=f"manual-client-{request.user.id}-{now:%Y%m%d%H%M%S}",
    )
    StoreOnboarding.objects.create(
        plan=data["plan"],
        store_name=data["store_name"],
        legal_name=data.get("legal_name", ""),
        tax_id=data.get("tax_id", ""),
        owner_name=data.get("owner_name", ""),
        owner_email=data["owner_email"],
        owner_phone=data.get("owner_phone", ""),
        store_address=data.get("store_address", ""),
        store_city=data.get("store_city", ""),
        desired_slug=data["desired_slug"],
        notes=data.get("notes", ""),
        status=StoreOnboarding.Status.ACTIVATED,
        company=company,
        owner=owner,
        subscription=company.subscriptions.order_by("-created_at").first(),
        simulated_payment_reference=f"manual-client-{request.user.id}-{now:%Y%m%d%H%M%S}",
    )
    return company, visible_password


@saas_owner_required
def saas_admin_client_detail(request, company_id):
    company = get_object_or_404(
        Company.objects.prefetch_related("subscriptions__plan", "branches", "memberships__user", "support_tickets"),
        id=company_id,
    )
    storefront, _ = StorefrontSettings.objects.get_or_create(company=company, defaults={"display_name": company.name})
    subscription = company.subscriptions.select_related("plan").order_by("-created_at").first()
    subscription_form = SaaSSubscriptionForm(initial={"plan": subscription.plan if subscription else None, "status": subscription.status if subscription else Subscription.Status.ACTIVE})
    password_form = SaaSUserPasswordResetForm(company=company)
    sii_context = _build_sii_panel_context(company)
    return render(
        request,
        "core/saas_admin/client_detail.html",
        {
            "client": company,
            "storefront": storefront,
            "subscription": subscription,
            "subscription_form": subscription_form,
            "password_form": password_form,
            "memberships": company.memberships.select_related("user").order_by("user__email"),
            "tickets": _client_support_tickets_queryset().filter(company=company)[:8],
            "sii_dashboard": sii_context["dashboard"],
            "sii_settings": sii_context["sii_settings"],
            "sii_api_config": sii_context["api_config"],
        },
    )


@saas_owner_required
def saas_admin_client_sii(request, company_id):
    company = get_object_or_404(Company, id=company_id)
    if request.method == "POST":
        response, context = _handle_sii_panel_post(request, company, "saas_admin_client_sii", company_id=company.id)
        if response:
            return response
    else:
        context = _build_sii_panel_context(company)
    context["client"] = company
    return render(request, "core/saas_admin/client_sii.html", context)


@saas_owner_required
def saas_admin_sii_document_xml_download(request, company_id, document_id):
    company = get_object_or_404(Company, id=company_id)
    document = get_object_or_404(SiiDocument, id=document_id, company=company)
    return _document_xml_response(document)


@saas_owner_required
def saas_admin_sii_document_boleta_download(request, company_id, document_id):
    company = get_object_or_404(Company, id=company_id)
    document = get_object_or_404(SiiDocument, id=document_id, company=company)
    return _document_boleta_pdf_response(document)


@saas_owner_required
def saas_admin_client_edit(request, company_id):
    company = get_object_or_404(Company, id=company_id)
    storefront, _ = StorefrontSettings.objects.get_or_create(company=company, defaults={"display_name": company.name})
    if request.method == "POST":
        client_form = SaaSClientForm(request.POST, instance=company)
        storefront_form = SaaSStorefrontSettingsForm(request.POST, instance=storefront)
        if client_form.is_valid() and storefront_form.is_valid():
            client_form.save()
            storefront_form.save()
            messages.success(request, "Cliente actualizado.")
            return redirect("saas_admin_client_detail", company_id=company.id)
    else:
        client_form = SaaSClientForm(instance=company)
        storefront_form = SaaSStorefrontSettingsForm(instance=storefront)
    return render(
        request,
        "core/saas_admin/client_edit.html",
        {"client": company, "client_form": client_form, "storefront_form": storefront_form},
    )


@saas_owner_required
def saas_admin_client_assign_plan(request, company_id):
    company = get_object_or_404(Company, id=company_id)
    if request.method != "POST":
        return redirect("saas_admin_client_detail", company_id=company.id)
    form = SaaSSubscriptionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "No se pudo asignar el plan. Revisa los datos.")
        return redirect("saas_admin_client_detail", company_id=company.id)

    now = timezone.now()
    period_end = now + timedelta(days=30 * form.cleaned_data["months"])
    current = company.subscriptions.order_by("-created_at").first()
    if current:
        current.plan = form.cleaned_data["plan"]
        current.status = form.cleaned_data["status"]
        current.payment_mode = Subscription.PaymentMode.SIMULATED
        current.current_period_start = now
        current.current_period_end = period_end
        current.cancelled_at = None
        current.external_reference = f"manual-saas-admin-{request.user.id}-{now:%Y%m%d%H%M%S}"
        current.save()
    else:
        Subscription.objects.create(
            company=company,
            plan=form.cleaned_data["plan"],
            status=form.cleaned_data["status"],
            payment_mode=Subscription.PaymentMode.SIMULATED,
            started_at=now,
            current_period_start=now,
            current_period_end=period_end,
            external_reference=f"manual-saas-admin-{request.user.id}-{now:%Y%m%d%H%M%S}",
        )
    company.is_active = form.cleaned_data["status"] in {Subscription.Status.TRIALING, Subscription.Status.ACTIVE}
    company.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Plan asignado manualmente.")
    return redirect("saas_admin_client_detail", company_id=company.id)


@saas_owner_required
def saas_admin_client_set_status(request, company_id, action):
    company = get_object_or_404(Company, id=company_id)
    subscription = company.subscriptions.order_by("-created_at").first()
    if action == "suspend":
        company.is_active = True
        if subscription:
            subscription.status = Subscription.Status.PAST_DUE
            subscription.save(update_fields=["status", "updated_at"])
        messages.warning(request, "Cliente suspendido por suscripcion inactiva.")
    elif action == "deactivate":
        company.is_active = False
        if subscription:
            subscription.status = Subscription.Status.CANCELLED
            subscription.cancelled_at = timezone.now()
            subscription.save(update_fields=["status", "cancelled_at", "updated_at"])
        messages.warning(request, "Cliente desactivado.")
    elif action == "reactivate":
        company.is_active = True
        if subscription:
            subscription.status = Subscription.Status.ACTIVE
            if not subscription.current_period_end or subscription.current_period_end < timezone.now():
                subscription.current_period_end = timezone.now() + timedelta(days=30)
            subscription.cancelled_at = None
            subscription.save(update_fields=["status", "current_period_end", "cancelled_at", "updated_at"])
        messages.success(request, "Cliente reactivado.")
    else:
        messages.error(request, "Accion no valida.")
        return redirect("saas_admin_client_detail", company_id=company.id)
    company.save(update_fields=["is_active", "updated_at"])
    return redirect("saas_admin_client_detail", company_id=company.id)


@saas_owner_required
def saas_admin_client_reset_user_password(request, company_id):
    company = get_object_or_404(Company, id=company_id)
    if request.method != "POST":
        return redirect("saas_admin_client_detail", company_id=company.id)
    form = SaaSUserPasswordResetForm(request.POST, company=company)
    if not form.is_valid():
        messages.error(request, "No se pudo cambiar la clave. Revisa el usuario y la confirmacion.")
        return redirect("saas_admin_client_detail", company_id=company.id)
    user = form.cleaned_data["user"]
    user.set_password(form.cleaned_data["password"])
    user.save(update_fields=["password"])
    messages.success(request, f"Clave actualizada para {user.email}.")
    return redirect("saas_admin_client_detail", company_id=company.id)


@saas_owner_required
def saas_admin_tickets(request):
    tickets = _client_support_tickets_queryset()
    return render(request, "core/saas_admin/tickets.html", {"tickets": tickets})

@saas_owner_required
def saas_admin_ticket_detail(request, ticket_id):
    ticket = get_object_or_404(_client_support_tickets_queryset(), id=ticket_id)
    if request.method == "POST":
        form = SupportMessageForm(request.POST)
        status = request.POST.get("status")
        priority = request.POST.get("priority")
        if form.is_valid():
            SupportMessage.objects.create(ticket=ticket, sender=request.user, **form.cleaned_data)
            ticket.last_message_at = timezone.now()
            if status in SupportTicket.Status.values:
                ticket.status = status
            if priority in SupportTicket.Priority.values:
                ticket.priority = priority
            ticket.save(update_fields=["last_message_at", "status", "priority", "updated_at"])
            messages.success(request, "Mensaje agregado.")
            return redirect("saas_admin_ticket_detail", ticket_id=ticket.id)
    else:
        form = SupportMessageForm()

    return render(
        request,
        "core/saas_admin/ticket_detail.html",
        {
            "ticket": ticket,
            "messages_list": ticket.messages.select_related("sender").all(),
            "form": form,
            "statuses": SupportTicket.Status.choices,
            "priorities": SupportTicket.Priority.choices,
        },
    )


def _calculate_mrr(active_clients):
    mrr = Decimal("0")
    for company in active_clients:
        subscription = next((sub for sub in company.subscriptions.all() if sub.is_active), None)
        if not subscription:
            continue
        if subscription.plan.billing_period == Plan.BillingPeriod.YEARLY:
            mrr += subscription.plan.price / Decimal("12")
        else:
            mrr += subscription.plan.price
    return mrr


def _build_saas_alerts(failed_payments, pending_onboardings, open_tickets):
    alerts = []
    for payment in failed_payments.select_related("onboarding").order_by("-created_at")[:4]:
        alerts.append({"kind": "Pago fallido", "title": payment.onboarding.store_name, "detail": payment.get_provider_display(), "created_at": payment.created_at})
    for onboarding_record in pending_onboardings.select_related("plan").order_by("-created_at")[:4]:
        alerts.append({"kind": "Alta pendiente", "title": onboarding_record.store_name, "detail": onboarding_record.plan.name, "created_at": onboarding_record.created_at})
    for ticket in open_tickets.select_related("company").order_by("-last_message_at", "-created_at")[:4]:
        alerts.append({"kind": "Soporte", "title": ticket.subject, "detail": ticket.company.name, "created_at": ticket.last_message_at or ticket.created_at})
    return sorted(alerts, key=lambda item: item["created_at"], reverse=True)[:10]
