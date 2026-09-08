from decimal import Decimal
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.messages import get_messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from cashier.models import CashMovement, CashSession
from catalog.models import Category, Product, ProductVariant
from common.currency import format_clp, round_chilean_cash, round_peso, to_decimal
from inventory.models import InventoryMovement, StockItem
from invoicing.models import TaxDocument
from orders.models import Order, OrderLine
from payments.models import Payment
from pos.models import PosConfiguration, PosTicket, default_cash_quick_amounts
from pos.printing import build_printer_payload
from sii.models import DteStatus, DteType
from sii.services import emitir_boleta, emitir_factura
from sii.services.exceptions import SiiError
from sii.services.ted import extract_ted_xml, render_pdf417_data_uri
from tenancy.models import Branch


CART_SESSION_KEY = "pos_cart"
TEMP_PRODUCTS_SESSION_KEY = "pos_temp_products"
TEMP_CART_PREFIX = "temp:"



@login_required
def terminal(request):
    cash_session = _get_open_cash_session(request.user, request.current_company)
    pos_config = _get_pos_configuration(cash_session)
    cart = _get_cart(request)

    if request.method == "POST":
        action = request.POST.get("action")
        checkout_receipt = None
        if action == "checkout":
            checkout_receipt = _checkout(request, cash_session)
        elif action == "open_cash":
            _open_cash_session(request, request.current_company)
        elif action == "add":
            _add_to_cart(request, cash_session)
        elif action == "add_temp_product":
            _add_temp_product(request, cash_session)
        elif action == "add_existing_temp_product":
            _add_existing_temp_product(request, cash_session)
        elif action == "remove_temp_product":
            _remove_temp_product(request, cash_session)
        elif action == "update":
            _update_cart(request)
        elif action == "discount_line":
            _discount_cart_line(request)
        elif action == "clear":
            request.session[CART_SESSION_KEY] = {}
            messages.info(request, "Carrito limpiado.")
        elif action == "void_sale":
            _void_sale(request, cash_session)
        elif action == "close_cash":
            _close_cash_session(request, cash_session)
        if _is_ajax_cart_request(request):
            return _cart_ajax_response(request, cash_session)
        if _is_ajax_modal_request(request):
            return _modal_ajax_response(request, cash_session, checkout_receipt=checkout_receipt)
        return redirect("pos:terminal")

    query = request.GET.get("q", "").strip()
    category_slug = request.GET.get("category", "").strip()
    view_mode = request.GET.get("view", "products").strip()
    if view_mode not in {"products", "sales", "quick"}:
        view_mode = "products"
    product_stock_items = list(_search_stock(cash_session, query, category_slug))
    categories = _get_categories(cash_session)
    temp_products = _get_temp_products(request, cash_session)
    cart_lines, totals = _build_cart_lines(cart, cash_session, temp_products=temp_products)
    quick_access_items = _get_quick_access_items(cash_session)
    discount_blocked_by_catalog = _has_catalog_discount_lock(cart_lines)
    _apply_available_stock(product_stock_items, cart)
    products = _group_stock_products(product_stock_items)
    _apply_available_stock(quick_access_items, cart)

    return render(
        request,
        "pos/terminal.html",
        {
            "cash_session": cash_session,
            "query": query,
            "category_slug": category_slug,
            "view_mode": view_mode,
            "categories": categories,
            "products": products,
            "temp_products": temp_products,
            "cart_lines": cart_lines,
            "totals": totals,
            "payment_methods": Payment.Method.choices,
            "cash_quick_amounts": _get_cash_quick_amounts(cash_session),
            "latest_sales": _get_latest_sales(cash_session),
            "quick_access_items": quick_access_items,
            "discount_blocked_by_catalog": discount_blocked_by_catalog,
            "discount_supervisor_settings": _discount_supervisor_settings(pos_config),
            "cash_summary": _build_cash_summary(cash_session),
            "close_cash_summary": _build_close_cash_summary(cash_session),
            "close_cash_sales": _get_close_cash_sales(cash_session),
            "open_cash_branches": _open_cash_branches(request.current_company),
            "pos_config": pos_config,
            "show_product_images": pos_config.show_product_images if pos_config else True,
            "can_return_to_admin": _can_return_to_admin(request.user),
            "admin_return_url": _admin_return_url(request.user),
            "user_initials": _user_initials(request.user),
        },
    )


def _is_ajax_cart_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest" and request.POST.get("action") in {
        "add",
        "add_existing_temp_product",
        "update",
        "discount_line",
        "clear",
    }


def _is_ajax_modal_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest" and request.POST.get("action") in {
        "close_cash",
        "void_sale",
        "checkout",
    }


def _modal_ajax_response(request, cash_session=None, checkout_receipt=None):
    message_list = [{"text": str(message), "tags": message.tags} for message in get_messages(request)]
    has_error = any("error" in message["tags"] for message in message_list)
    payload = {
        "ok": not has_error,
        "messages": message_list,
        "redirect_url": request.path,
    }
    if checkout_receipt and cash_session:
        temp_products = _get_temp_products(request, cash_session)
        cart_lines, totals = _build_cart_lines(_get_cart(request), cash_session, temp_products=temp_products)
        pos_config = _get_pos_configuration(cash_session)
        context = {
            "cash_session": cash_session,
            "cart_lines": cart_lines,
            "totals": totals,
            "payment_methods": Payment.Method.choices,
            "cash_quick_amounts": _get_cash_quick_amounts(cash_session),
            "discount_blocked_by_catalog": _has_catalog_discount_lock(cart_lines),
            "discount_supervisor_settings": _discount_supervisor_settings(pos_config),
            "show_product_images": pos_config.show_product_images if pos_config else True,
        }
        payload.update(
            {
                "checkout_receipt": checkout_receipt,
                "cart_count": len(cart_lines),
                "stock_updates": _stock_updates(cash_session, _get_cart(request)),
                "cart_html": render_to_string("pos/_cart_panel.html", context, request=request),
                "payment_html": render_to_string("pos/_payment_modal.html", context, request=request),
            }
        )
    return JsonResponse(payload)


def _cart_ajax_response(request, cash_session):
    temp_products = _get_temp_products(request, cash_session)
    cart_lines, totals = _build_cart_lines(_get_cart(request), cash_session, temp_products=temp_products)
    pos_config = _get_pos_configuration(cash_session)
    context = {
        "cash_session": cash_session,
        "cart_lines": cart_lines,
        "totals": totals,
        "payment_methods": Payment.Method.choices,
        "cash_quick_amounts": _get_cash_quick_amounts(cash_session),
        "discount_blocked_by_catalog": _has_catalog_discount_lock(cart_lines),
        "discount_supervisor_settings": _discount_supervisor_settings(pos_config),
        "show_product_images": pos_config.show_product_images if pos_config else True,
    }
    message_list = [{"text": str(message), "tags": message.tags} for message in get_messages(request)]
    return JsonResponse(
        {
            "ok": not any("error" in message["tags"] for message in message_list),
            "messages": message_list,
            "cart_count": len(cart_lines),
            "stock_updates": _stock_updates(cash_session, _get_cart(request)),
            "cart_html": render_to_string("pos/_cart_panel.html", context, request=request),
            "payment_html": render_to_string("pos/_payment_modal.html", context, request=request),
        }
    )


def _has_catalog_discount_lock(cart_lines):
    return any(not line.get("allows_additional_discount", True) for line in cart_lines)


def _discount_supervisor_settings(pos_config):
    if not pos_config:
        return {
            "required": True,
            "threshold_percent": Decimal("50"),
        }
    return {
        "required": pos_config.require_supervisor_discount_code,
        "threshold_percent": pos_config.supervisor_discount_threshold_percent,
    }


def _cart_quantity_by_variant(cart):
    quantities = {}
    for key, item in (cart or {}).items():
        key = str(key)
        if key.startswith(TEMP_CART_PREFIX):
            continue
        try:
            variant_id = int(key)
        except (TypeError, ValueError):
            continue
        quantities[variant_id] = quantities.get(variant_id, Decimal("0")) + _to_decimal(item.get("quantity", "0"))
    return quantities


def _available_stock_quantity(stock_item, cart_quantities):
    return max(stock_item.quantity - cart_quantities.get(stock_item.variant_id, Decimal("0")), Decimal("0"))


def _stock_status(quantity):
    if quantity <= 0:
        return "out"
    if quantity <= 5:
        return "low"
    return "ok"


def _stock_label(quantity):
    if quantity <= 0:
        return "Sin stock"
    if quantity <= 5:
        return f"Stock bajo: {int(round_peso(quantity))}"
    return f"Stock: {int(round_peso(quantity))} unidades"


def _apply_available_stock(stock_items, cart):
    cart_quantities = _cart_quantity_by_variant(cart)
    for item in stock_items:
        available = _available_stock_quantity(item, cart_quantities)
        item.available_quantity = available
        item.stock_status = _stock_status(available)
    return stock_items


def _stock_updates(cash_session, cart):
    if not cash_session:
        return []
    cart_quantities = _cart_quantity_by_variant(cart)
    updates = []
    for item in _quick_access_base_queryset(cash_session):
        available = _available_stock_quantity(item, cart_quantities)
        status = _stock_status(available)
        updates.append(
            {
                "stock_item_id": item.id,
                "quantity": int(round_peso(available)),
                "status": status,
                "label": _stock_label(available),
                "button_disabled": available <= 0,
            }
        )
    return updates


def _get_open_cash_session(user, company=None):
    queryset = (
        CashSession.objects.select_related("branch", "branch__company", "opened_by")
        .filter(status=CashSession.Status.OPEN, opened_by=user)
    )
    if company:
        queryset = queryset.filter(branch__company=company)
    return queryset.order_by("-opened_at").first()


def _get_categories(cash_session):
    if not cash_session:
        return Category.objects.none()
    return Category.objects.filter(company=cash_session.branch.company, is_active=True).order_by("name")


def _get_cash_quick_amounts(cash_session):
    if not cash_session:
        return default_cash_quick_amounts()
    try:
        return cash_session.branch.pos_configuration.get_cash_quick_amounts()
    except PosConfiguration.DoesNotExist:
        return default_cash_quick_amounts()


def _get_pos_configuration(cash_session):
    if not cash_session:
        return None
    try:
        return cash_session.branch.pos_configuration
    except PosConfiguration.DoesNotExist:
        return None


def _get_latest_sales(cash_session):
    if not cash_session:
        return Order.objects.none()
    return (
        Order.objects.select_related("pos_ticket", "created_by")
        .prefetch_related("payments", "lines")
        .filter(cash_session=cash_session, channel=Order.Channel.POS)
        .exclude(status=Order.Status.DRAFT)
        .order_by("-created_at")[:7]
    )


def _get_close_cash_sales(cash_session):
    if not cash_session:
        return Order.objects.none()
    return (
        Order.objects.select_related("pos_ticket", "created_by")
        .prefetch_related("payments", "lines")
        .filter(cash_session=cash_session, channel=Order.Channel.POS)
        .exclude(status=Order.Status.DRAFT)
        .order_by("-created_at")[:25]
    )


def _build_close_cash_summary(cash_session):
    if not cash_session:
        return {
            'sales_count': 0,
            'sales_total': Decimal('0'),
            'expected_cash_amount': Decimal('0'),
            'opening_amount': Decimal('0'),
            'payment_rows': [],
        }

    orders = Order.objects.filter(
        cash_session=cash_session,
        channel=Order.Channel.POS,
        status=Order.Status.PAID,
    )
    payments_by_method = {
        row['method']: row
        for row in Payment.objects.filter(order__in=orders, status=Payment.Status.PAID)
        .values('method')
        .annotate(total=Sum('amount'), count=Count('order_id', distinct=True))
    }
    payment_rows = []
    for method, label in Payment.Method.choices:
        if method not in {Payment.Method.CASH, Payment.Method.CARD, Payment.Method.TRANSFER}:
            continue
        row = payments_by_method.get(method, {})
        payment_rows.append(
            {
                'method': method,
                'label': label,
                'count': row.get('count', 0),
                'total': row.get('total') or Decimal('0'),
            }
        )

    return {
        'sales_count': orders.count(),
        'sales_total': orders.aggregate(total=Sum('total'))['total'] or Decimal('0'),
        'expected_cash_amount': cash_session.expected_cash_amount,
        'opening_amount': cash_session.opening_amount,
        'payment_rows': payment_rows,
    }


def _build_cash_summary(cash_session):
    if not cash_session:
        return {
            'date': timezone.localdate(),
            'sales_count': 0,
            'sales_total': Decimal('0'),
            'expected_cash_amount': Decimal('0'),
            'opening_amount': Decimal('0'),
            'payment_rows': [],
        }

    today = timezone.localdate()
    orders = Order.objects.filter(
        cash_session=cash_session,
        channel=Order.Channel.POS,
        status=Order.Status.PAID,
        created_at__date=today,
    )
    payments_by_method = {
        row['method']: row
        for row in Payment.objects.filter(order__in=orders, status=Payment.Status.PAID)
        .values('method')
        .annotate(total=Sum('amount'), count=Count('id'))
    }
    payment_rows = []
    for method, label in Payment.Method.choices:
        if method not in {Payment.Method.CASH, Payment.Method.CARD, Payment.Method.TRANSFER}:
            continue
        row = payments_by_method.get(method, {})
        payment_rows.append(
            {
                'method': method,
                'label': label,
                'count': row.get('count', 0),
                'total': row.get('total') or Decimal('0'),
            }
        )

    return {
        'date': today,
        'sales_count': orders.count(),
        'sales_total': orders.aggregate(total=Sum('total'))['total'] or Decimal('0'),
        'expected_cash_amount': cash_session.expected_cash_amount,
        'opening_amount': cash_session.opening_amount,
        'payment_rows': payment_rows,
    }


def _can_return_to_admin(user):
    return bool(user and user.is_authenticated and (user.is_superuser or user.role in {"owner", "admin"}))


def _admin_return_url(user):
    if user and user.is_authenticated and user.is_superuser:
        return "/saas-admin/"
    return "/dashboard/"


def _user_initials(user):
    if not user or not user.is_authenticated:
        return "TP"
    full_name = user.get_full_name().strip()
    if full_name:
        return "".join(part[:1] for part in full_name.split()[:2]).upper()
    return user.email[:2].upper()


def _open_cash_branches(company):
    if not company:
        return Branch.objects.none()
    return Branch.objects.filter(company=company, is_active=True).order_by('is_online_store', 'name')


@transaction.atomic
def _open_cash_session(request, company=None):
    if _get_open_cash_session(request.user, company):
        messages.warning(request, 'Ya tienes una caja abierta.')
        return

    if not company:
        messages.error(request, 'Tu usuario no tiene una tienda asignada.')
        return

    branch = _open_cash_branches(company).filter(id=request.POST.get('branch_id')).first()
    if not branch:
        messages.error(request, 'Sucursal invalida para abrir caja.')
        return

    try:
        pos_config = branch.pos_configuration
    except PosConfiguration.DoesNotExist:
        pos_config = None
    if pos_config and not pos_config.allow_cash_opening:
        messages.error(request, 'La apertura de caja esta desactivada para esta terminal TPV.')
        return

    opening_amount = round_peso(request.POST.get('opening_amount', '0'))
    CashSession.objects.create(
        branch=branch,
        opened_by=request.user,
        opening_amount=opening_amount,
        expected_cash_amount=opening_amount,
        notes=request.POST.get('notes', '').strip(),
    )
    messages.success(request, f'Caja abierta en {branch.name}.')


def _temp_bucket_key(cash_session):
    return str(cash_session.id) if cash_session else ''


def _get_all_temp_products(request):
    return request.session.get(TEMP_PRODUCTS_SESSION_KEY, {})


def _get_temp_products(request, cash_session):
    bucket_key = _temp_bucket_key(cash_session)
    if not bucket_key:
        return []
    return list(_get_all_temp_products(request).get(bucket_key, []))


def _save_temp_products(request, cash_session, temp_products):
    bucket_key = _temp_bucket_key(cash_session)
    if not bucket_key:
        return
    all_products = _get_all_temp_products(request)
    all_products[bucket_key] = temp_products
    request.session[TEMP_PRODUCTS_SESSION_KEY] = all_products
    request.session.modified = True


def _add_temp_product(request, cash_session):
    if not cash_session:
        messages.error(request, 'Abre una caja antes de crear productos temporales.')
        return

    name = request.POST.get('temp_name', '').strip()[:120]
    unit_price = round_peso(request.POST.get('temp_price', '0'))
    quantity = _to_decimal(request.POST.get('temp_quantity', '1'))
    if not name:
        messages.error(request, 'El producto temporal necesita nombre.')
        return
    if unit_price <= 0 or quantity <= 0:
        messages.error(request, 'Precio y cantidad deben ser mayores a cero.')
        return

    temp_products = _get_temp_products(request, cash_session)
    next_id = str(max([int(item.get('id', 0)) for item in temp_products] or [0]) + 1)
    temp_products.append({'id': next_id, 'name': name, 'unit_price': str(unit_price)})
    _save_temp_products(request, cash_session, temp_products)

    cart = _get_cart(request)
    cart[f'{TEMP_CART_PREFIX}{next_id}'] = {'quantity': str(quantity)}
    _save_cart(request, cart)
    messages.success(request, 'Producto temporal agregado al carrito.')


def _add_existing_temp_product(request, cash_session):
    if not cash_session:
        messages.error(request, 'Abre una caja antes de vender productos temporales.')
        return

    temp_id = request.POST.get('temp_id', '').strip()
    temp_product = _temp_products_by_id(_get_temp_products(request, cash_session)).get(temp_id)
    if not temp_product:
        messages.error(request, 'Producto temporal no disponible en esta caja.')
        return

    cart = _get_cart(request)
    cart_key = f'{TEMP_CART_PREFIX}{temp_id}'
    current_quantity = _to_decimal(cart.get(cart_key, {}).get('quantity', '0'))
    cart[cart_key] = {**cart.get(cart_key, {}), 'quantity': str(current_quantity + Decimal('1'))}
    _save_cart(request, cart)
    messages.success(request, 'Producto temporal agregado al carrito.')


def _remove_temp_product(request, cash_session):
    temp_id = request.POST.get('temp_id', '').strip()
    temp_products = [item for item in _get_temp_products(request, cash_session) if str(item.get('id')) != temp_id]
    _save_temp_products(request, cash_session, temp_products)
    cart = _get_cart(request)
    cart.pop(f'{TEMP_CART_PREFIX}{temp_id}', None)
    _save_cart(request, cart)
    messages.info(request, 'Producto temporal eliminado.')


def _temp_products_by_id(temp_products):
    return {str(item.get('id')): item for item in temp_products}


def _temporary_variant(company):
    product, _ = Product.objects.get_or_create(
        company=company,
        slug='pos-temporal',
        defaults={
            'name': 'Producto temporal POS',
            'product_type': Product.TYPE_SIMPLE,
            'is_active': False,
            'is_sellable_pos': False,
            'is_sellable_online': False,
        },
    )
    variant, _ = ProductVariant.objects.get_or_create(
        product=product,
        sku=f'TEMP-POS-{company.id}',
        defaults={
            'name': 'Producto temporal',
            'sale_price': Decimal('0'),
            'cost_price': Decimal('0'),
            'tax_rate': Decimal('19'),
            'is_active': False,
        },
    )
    return variant


def _search_stock(cash_session, query, category_slug=""):
    if not cash_session:
        return StockItem.objects.none()

    queryset = (
        StockItem.objects.select_related("variant", "variant__product", "variant__product__brand")
        .filter(
            branch=cash_session.branch,
            variant__is_active=True,
            variant__product__is_active=True,
            variant__product__is_sellable_pos=True,
            variant__product__deleted_at__isnull=True,
        )
        .order_by("variant__product__name", "variant__sku")
    )
    if query:
        queryset = queryset.filter(
            Q(variant__product__name__icontains=query)
            | Q(variant__sku__icontains=query)
            | Q(variant__barcode__icontains=query)
            | Q(variant__name__icontains=query)
        )
    if category_slug:
        queryset = queryset.filter(variant__product__category__slug=category_slug)
    return queryset[:200]


def _group_stock_products(stock_items, limit=24):
    groups = {}
    for stock_item in stock_items:
        product = stock_item.variant.product
        group = groups.setdefault(
            product.id,
            SimpleNamespace(product=product, variants=[], selected_item=None, variant_count=0),
        )
        group.variants.append(stock_item)
    product_groups = []
    for group in groups.values():
        group.selected_item = next(
            (item for item in group.variants if item.available_quantity > 0),
            group.variants[0],
        )
        group.variant_count = len(group.variants)
        product_groups.append(group)
    return product_groups[:limit]


def _quick_access_base_queryset(cash_session):
    if not cash_session:
        return StockItem.objects.none()
    return StockItem.objects.select_related("variant", "variant__product", "variant__product__brand").filter(
        branch=cash_session.branch,
        quantity__gt=0,
        variant__is_active=True,
        variant__product__is_active=True,
        variant__product__is_sellable_pos=True,
        variant__product__deleted_at__isnull=True,
    )


def _sold_quantity_by_variant(cash_session):
    if not cash_session:
        return {}
    rows = (
        OrderLine.objects.filter(
            order__company=cash_session.branch.company,
            order__branch=cash_session.branch,
            order__channel=Order.Channel.POS,
            order__status=Order.Status.PAID,
        )
        .values("variant_id")
        .annotate(sold_quantity=Sum("quantity"))
        .order_by("-sold_quantity")
    )
    return {row["variant_id"]: row["sold_quantity"] or Decimal("0") for row in rows}


def _get_quick_access_items(cash_session, limit=12):
    if not cash_session:
        return []
    sold_by_variant = _sold_quantity_by_variant(cash_session)
    items = list(
        _quick_access_base_queryset(cash_session)
        .filter(variant__product__is_quick_access=True)
        .order_by("variant__product__name", "variant__sku")[:200]
    )
    items.sort(key=lambda item: (-sold_by_variant.get(item.variant_id, Decimal("0")), item.variant.product.name.lower(), item.variant.sku.lower()))
    for item in items[:limit]:
        item.quick_sold_quantity = sold_by_variant.get(item.variant_id, Decimal("0"))
        item.quick_is_manual = True
    return items[:limit]


def _get_cart(request):
    return request.session.get(CART_SESSION_KEY, {})


def _save_cart(request, cart):
    request.session[CART_SESSION_KEY] = cart
    request.session.modified = True


def _add_to_cart(request, cash_session):
    if not cash_session:
        messages.error(request, "No hay caja abierta para vender.")
        return

    stock_item_id = request.POST.get("stock_item_id")
    try:
        stock_item = StockItem.objects.select_related("variant").get(id=stock_item_id, branch=cash_session.branch)
    except StockItem.DoesNotExist:
        messages.error(request, "Producto no disponible en esta sucursal.")
        return

    if stock_item.quantity <= 0:
        messages.error(request, "Producto sin stock disponible.")
        return

    cart = _get_cart(request)
    key = str(stock_item.variant_id)
    current_quantity = Decimal(cart.get(key, {}).get("quantity", "0"))
    next_quantity = current_quantity + Decimal("1")
    if next_quantity > stock_item.quantity:
        messages.error(request, "No hay stock suficiente para agregar otra unidad.")
        return

    cart[key] = {**cart.get(key, {}), "quantity": str(next_quantity)}
    _save_cart(request, cart)
    messages.success(request, "Producto agregado al carrito.")


def _update_cart(request):
    cart_key = request.POST.get('cart_key') or request.POST.get('variant_id')
    delta = request.POST.get('delta')

    cart = _get_cart(request)
    key = str(cart_key)
    if delta and key in cart:
        quantity = _to_decimal(cart[key].get('quantity', '0')) + _to_decimal(delta)
    else:
        quantity = _to_decimal(request.POST.get('quantity', '0'))

    if quantity <= 0:
        cart.pop(key, None)
    elif key in cart:
        cart[key]['quantity'] = str(quantity)
    _save_cart(request, cart)


def _discount_cart_line(request):
    cart_key = str(request.POST.get("cart_key") or "")
    discount_type = request.POST.get("line_discount_type", "none")
    discount_value = request.POST.get("line_discount_value", "0")
    if discount_type not in {"none", "percent", "amount"}:
        discount_type = "none"

    cart = _get_cart(request)
    if cart_key in cart:
        if discount_type != "none" and not _cart_key_allows_additional_discount(request, cart_key):
            messages.error(request, "Este producto ya tiene descuento de catalogo y no permite descuento adicional.")
            return
        cart[cart_key]["discount_type"] = discount_type
        cart[cart_key]["discount_value"] = discount_value or "0"
        _save_cart(request, cart)
        messages.success(request, "Descuento aplicado al producto.")
    else:
        messages.error(request, "No se encontro el producto en el carrito.")


def _cart_key_allows_additional_discount(request, cart_key):
    if str(cart_key).startswith(TEMP_CART_PREFIX):
        return True
    try:
        variant = ProductVariant.objects.get(id=cart_key)
    except (ProductVariant.DoesNotExist, ValueError, TypeError):
        return True
    return (not variant.has_pos_discount) or variant.allow_pos_discount_on_discount


def _discount_total(subtotal, discount_type, discount_value):
    value = to_decimal(discount_value or '0')
    minimum_line_total = Decimal('10')
    max_discount = max(subtotal - minimum_line_total, Decimal('0'))
    if subtotal <= minimum_line_total or value <= 0:
        return Decimal('0')
    if discount_type == 'percent':
        percent = min(value, Decimal('100'))
        return min(round_peso(subtotal * percent / Decimal('100')), max_discount)
    if discount_type == 'amount':
        return min(round_peso(value), max_discount)
    return Decimal('0')


def _build_cart_lines(cart, cash_session, *, temp_products=None, line_discounts=None, sale_discount_type='none', sale_discount_value='0'):
    if not cart:
        return [], _empty_totals()
    if not cash_session:
        return [], _empty_totals()

    temp_by_id = _temp_products_by_id(temp_products or [])
    stock_keys = [key for key in cart.keys() if not str(key).startswith(TEMP_CART_PREFIX)]
    stock_items = {
        str(item.variant_id): item
        for item in StockItem.objects.select_related('variant', 'variant__product').filter(
            branch=cash_session.branch,
            variant_id__in=stock_keys,
        )
    }
    line_discounts = line_discounts or {}
    lines = []
    subtotal = Decimal('0')
    for cart_key, item in cart.items():
        key = str(cart_key)
        quantity = _to_decimal(item.get('quantity', '0'))
        if quantity <= 0:
            continue

        if key.startswith(TEMP_CART_PREFIX):
            temp_id = key.removeprefix(TEMP_CART_PREFIX)
            temp_product = temp_by_id.get(temp_id)
            if not temp_product:
                continue
            unit_price = round_peso(temp_product.get('unit_price', '0'))
            if unit_price <= 0:
                continue
            line_subtotal = round_peso(quantity * unit_price)
            discount_data = line_discounts.get(key) or item
            line_discount = _discount_total(line_subtotal, discount_data.get('discount_type', 'none'), discount_data.get('discount_value', '0'))
            line_total = max(line_subtotal - line_discount, Decimal('0'))
            subtotal += line_subtotal
            lines.append(
                {
                    'cart_key': key,
                    'stock_item': None,
                    'variant': None,
                    'description': temp_product.get('name') or 'Producto temporal',
                    'quantity': quantity,
                    'regular_unit_price': unit_price,
                    'unit_price': unit_price,
                    'has_catalog_discount': False,
                    'catalog_discount_amount': Decimal('0'),
                    'allows_additional_discount': True,
                    'line_subtotal': line_subtotal,
                    'discount_type': discount_data.get('discount_type', 'none'),
                    'discount_value': discount_data.get('discount_value', '0'),
                    'discount_amount': line_discount,
                    'sale_discount_amount': Decimal('0'),
                    'line_total': line_total,
                    'available': quantity,
                    'is_temporary': True,
                }
            )
            continue

        stock_item = stock_items.get(key)
        if not stock_item:
            continue
        regular_unit_price = stock_item.variant.sale_price
        unit_price = stock_item.variant.effective_pos_price
        has_catalog_discount = stock_item.variant.has_pos_discount
        allows_additional_discount = (not has_catalog_discount) or stock_item.variant.allow_pos_discount_on_discount
        line_subtotal = round_peso(quantity * unit_price)
        discount_data = line_discounts.get(key) or item
        if not allows_additional_discount:
            discount_data = {'discount_type': 'none', 'discount_value': '0'}
        line_discount = _discount_total(line_subtotal, discount_data.get('discount_type', 'none'), discount_data.get('discount_value', '0'))
        line_total = max(line_subtotal - line_discount, Decimal('0'))
        subtotal += line_subtotal
        lines.append(
            {
                'cart_key': key,
                'stock_item': stock_item,
                'variant': stock_item.variant,
                'description': str(stock_item.variant),
                'quantity': quantity,
                'regular_unit_price': regular_unit_price,
                'unit_price': unit_price,
                'has_catalog_discount': has_catalog_discount,
                'catalog_discount_amount': stock_item.variant.pos_catalog_discount_amount,
                'allows_additional_discount': allows_additional_discount,
                'line_subtotal': line_subtotal,
                'discount_type': discount_data.get('discount_type', 'none'),
                'discount_value': discount_data.get('discount_value', '0'),
                'discount_amount': line_discount,
                'sale_discount_amount': Decimal('0'),
                'line_total': line_total,
                'available': stock_item.quantity,
                'is_temporary': False,
            }
        )

    subtotal = round_peso(subtotal)
    product_discount_total = round_peso(sum((line['discount_amount'] for line in lines), Decimal('0')))
    subtotal_after_product_discounts = max(subtotal - product_discount_total, Decimal('0'))
    sale_discount_base = round_peso(sum((line['line_total'] for line in lines if line.get('allows_additional_discount', True)), Decimal('0')))
    sale_discount_total = _discount_total(sale_discount_base, sale_discount_type, sale_discount_value)
    if sale_discount_total:
        _apply_sale_discount_to_lines(lines, sale_discount_base, sale_discount_total)

    discount_total = product_discount_total + sale_discount_total
    discounted_subtotal = max(subtotal - discount_total, Decimal('0'))
    total = round_chilean_cash(discounted_subtotal)
    tax_total = round_peso(total * Decimal('19') / Decimal('119'))
    rounding_adjustment = total - discounted_subtotal
    return lines, {
        'subtotal': subtotal,
        'product_discount_total': product_discount_total,
        'sale_discount_total': sale_discount_total,
        'discount_total': discount_total,
        'discounted_subtotal': discounted_subtotal,
        'tax_total': tax_total,
        'total': total,
        'rounding_adjustment': rounding_adjustment,
    }


def _apply_sale_discount_to_lines(lines, base_total, sale_discount_total):
    eligible_lines = [line for line in lines if line['line_total'] > 0 and line.get('allows_additional_discount', True)]
    if not eligible_lines or base_total <= 0 or sale_discount_total <= 0:
        return

    remaining_discount = sale_discount_total
    for index, line in enumerate(eligible_lines):
        if index == len(eligible_lines) - 1:
            line_discount = min(remaining_discount, line['line_total'])
        else:
            line_discount = round_peso(sale_discount_total * line['line_total'] / base_total)
            line_discount = min(line_discount, line['line_total'], remaining_discount)
        line['sale_discount_amount'] = line_discount
        line['discount_amount'] += line_discount
        line['line_total'] = max(line['line_total'] - line_discount, Decimal('0'))
        remaining_discount -= line_discount
        if remaining_discount <= 0:
            break

def _line_discounts_from_request(request):
    discounts = {}
    keys = request.POST.getlist('discount_cart_key')
    types = request.POST.getlist('line_discount_type')
    values = request.POST.getlist('line_discount_value')
    for key, discount_type, discount_value in zip(keys, types, values):
        if discount_type not in {'none', 'percent', 'amount'}:
            discount_type = 'none'
        discounts[str(key)] = {
            'discount_type': discount_type,
            'discount_value': discount_value or '0',
        }
    return discounts


def _is_discount_attempt(discount_type, discount_value):
    return discount_type in {'percent', 'amount'} and to_decimal(discount_value or '0') > 0


def _discount_supervisor_required(pos_config, totals):
    if not pos_config or pos_config.require_supervisor_discount_code:
        threshold = pos_config.supervisor_discount_threshold_percent if pos_config else Decimal("50")
        subtotal = totals.get("subtotal") or Decimal("0")
        discount_total = totals.get("discount_total") or Decimal("0")
        if subtotal <= 0 or discount_total <= 0:
            return False
        discount_percent = (discount_total * Decimal("100")) / subtotal
        return discount_percent >= threshold
    return False


def _discount_supervisor_error(request, cash_session, totals):
    pos_config = _get_pos_configuration(cash_session)
    if not _discount_supervisor_required(pos_config, totals):
        return ""
    expected_code = pos_config.supervisor_discount_code if pos_config else "1234"
    supervisor_code = request.POST.get("supervisor_discount_code", "").strip()
    if supervisor_code != expected_code:
        if supervisor_code:
            return "Codigo supervisor incorrecto. Intentalo nuevamente."
        threshold = pos_config.supervisor_discount_threshold_percent if pos_config else Decimal("50")
        threshold_text = f"{threshold:.2f}".rstrip("0").rstrip(".")
        return f"Este descuento requiere codigo supervisor porque alcanza o supera el {threshold_text}%."
    return ""


def _discount_block_error(cart_lines, requested_line_discounts, sale_discount_type, sale_discount_value):
    locked_lines = [line for line in cart_lines if not line.get('allows_additional_discount', True)]
    if not locked_lines:
        return ''
    if _is_discount_attempt(sale_discount_type, sale_discount_value):
        return 'No se puede aplicar descuento general porque el carrito contiene productos con oferta TPV que no permiten descuento adicional.'
    for line in locked_lines:
        discount_data = requested_line_discounts.get(line['cart_key']) or {}
        if _is_discount_attempt(discount_data.get('discount_type'), discount_data.get('discount_value')):
            return f"No se puede aplicar otro descuento a {line['description']} porque ya tiene oferta TPV."
    return ''


@transaction.atomic
def _checkout(request, cash_session):
    if not cash_session:
        messages.error(request, "No hay caja abierta para cerrar la venta.")
        return

    cart = _get_cart(request)
    temp_products = _get_temp_products(request, cash_session)
    requested_line_discounts = _line_discounts_from_request(request)
    sale_discount_type = request.POST.get('sale_discount_type', 'none')
    sale_discount_value = request.POST.get('sale_discount_value', '0')
    cart_lines, totals = _build_cart_lines(
        cart,
        cash_session,
        temp_products=temp_products,
        line_discounts=requested_line_discounts,
        sale_discount_type=sale_discount_type,
        sale_discount_value=sale_discount_value,
    )
    if not cart_lines:
        messages.error(request, "El carrito esta vacio.")
        return
    discount_error = _discount_block_error(cart_lines, requested_line_discounts, sale_discount_type, sale_discount_value)
    if discount_error:
        messages.error(request, discount_error)
        return
    supervisor_discount_error = _discount_supervisor_error(request, cash_session, totals)
    if supervisor_discount_error:
        messages.error(request, supervisor_discount_error)
        return

    payment_entries, amount_received, change_due = _build_payment_entries(request, totals["total"])
    if not payment_entries:
        messages.error(request, "Selecciona al menos un medio de pago valido.")
        return
    if amount_received < totals["total"]:
        messages.error(request, "El monto recibido no cubre el total de la venta.")
        return
    if change_due and all(entry["method"] != Payment.Method.CASH for entry in payment_entries):
        messages.error(request, "El vuelto solo puede generarse cuando hay pago en efectivo.")
        return
    cash_tendered = sum((entry["tendered_amount"] for entry in payment_entries if entry["method"] == Payment.Method.CASH), Decimal("0"))
    if change_due > cash_tendered:
        messages.error(request, "El vuelto no puede superar el efectivo recibido.")
        return
    remaining_change = change_due
    normalized_payments = []
    for entry in payment_entries:
        applied_amount = entry["tendered_amount"]
        entry_change = Decimal("0")
        if entry["method"] == Payment.Method.CASH and remaining_change:
            entry_change = min(remaining_change, applied_amount)
            applied_amount -= entry_change
            remaining_change -= entry_change
        if applied_amount > 0:
            normalized_payments.append(
                {
                    "method": entry["method"],
                    "amount": applied_amount,
                    "tendered_amount": entry["tendered_amount"],
                    "change_due": entry_change,
                }
            )

    for line in cart_lines:
        if not line.get('is_temporary') and line['quantity'] > line['available']:
            messages.error(request, f"Stock insuficiente para {line['variant']}.")
            return

    company = cash_session.branch.company
    order = Order.objects.create(
        company=company,
        branch=cash_session.branch,
        cash_session=cash_session,
        channel=Order.Channel.POS,
        status=Order.Status.PAID,
        subtotal=totals["subtotal"],
        discount_total=totals['discount_total'],
        tax_total=totals["tax_total"],
        total=totals["total"],
        created_by=request.user,
        notes="Venta cerrada desde TPV.",
    )

    receipt_lines = []
    temp_variant = _temporary_variant(company) if any(line.get('is_temporary') for line in cart_lines) else None
    for line in cart_lines:
        line_discount = line['discount_amount']
        discounted_line_total = line['line_total']

        if line.get('is_temporary'):
            variant = temp_variant
            sku = 'TEMP'
            name = line['description']
        else:
            stock_item = StockItem.objects.select_for_update().get(id=line['stock_item'].id)
            stock_item.quantity = stock_item.quantity - line['quantity']
            stock_item.save(update_fields=['quantity', 'updated_at'])
            variant = line['variant']
            sku = variant.sku
            name = str(variant)
            InventoryMovement.objects.create(
                stock_item=stock_item,
                movement_type=InventoryMovement.MovementType.SALE,
                quantity=-line['quantity'],
                unit_cost=stock_item.average_cost,
                reference=f'order:{order.id}',
                notes='Rebaja automatica por venta TPV.',
                created_by=request.user,
            )

        OrderLine.objects.create(
            order=order,
            variant=variant,
            description=line['description'],
            quantity=line['quantity'],
            unit_price=line['unit_price'],
            discount_amount=line_discount,
            tax_amount=round_peso(discounted_line_total * Decimal('19') / Decimal('119')),
            line_total=discounted_line_total,
        )
        receipt_lines.append(
            {
                'sku': sku,
                'name': name,
                'quantity': str(line['quantity']),
                'unit_price': str(line['unit_price']),
                'discount_amount': str(line_discount),
                'line_total': str(discounted_line_total),
            }
        )

    payment_payload = []
    for payment in normalized_payments:
        Payment.objects.create(
            order=order,
            method=payment["method"],
            status=Payment.Status.PAID,
            amount=payment["amount"],
            raw_response={
                "source": "pos",
                "amount_received": str(payment["tendered_amount"]),
                "change_due": str(payment["change_due"]),
            },
        )
        payment_payload.append(
            {
                "method": payment["method"],
                "amount": str(payment["amount"]),
                "amount_received": str(payment["tendered_amount"]),
                "change_due": str(payment["change_due"]),
            }
        )

    cash_payment_total = sum((payment["amount"] for payment in normalized_payments if payment["method"] == Payment.Method.CASH), Decimal("0"))
    if cash_payment_total:
        cash_session.expected_cash_amount += cash_payment_total
        cash_session.save(update_fields=["expected_cash_amount", "updated_at"])
        CashMovement.objects.create(
            session=cash_session,
            movement_type=CashMovement.MovementType.INCOME,
            amount=cash_payment_total,
            reason=f"Venta TPV orden #{order.id}",
            created_by=request.user,
        )

    ticket_number = f"POS-{order.id:06d}"
    PosTicket.objects.create(
        order=order,
        ticket_number=ticket_number,
        receipt_payload={
            "ticket_number": ticket_number,
            "payment_method": normalized_payments[0]["method"],
            "payments": payment_payload,
            "total": str(totals["total"]),
            "amount_received": str(amount_received),
            "change_due": str(change_due),
            "lines": receipt_lines,
        },
    )
    TaxDocument.objects.create(
        order=order,
        document_type=TaxDocument.DocumentType.RECEIPT,
        status=TaxDocument.Status.ISSUED,
        folio=ticket_number,
        payload={"source": "pos", "ticket_number": ticket_number},
    )
    document_choice = request.POST.get("tax_document_choice", "boleta")
    if document_choice in {"boleta", "factura"}:
        transaction.on_commit(lambda: _emit_sii_document(request, order, document_choice))

    request.session[CART_SESSION_KEY] = {}
    change_message = f" Vuelto: {format_clp(change_due)}." if change_due else ""
    messages.success(request, f"Venta #{order.id} cerrada por {format_clp(order.total)}.{change_message} Ticket {ticket_number}.")
    document_labels = {
        "boleta": "Boleta electronica afecta",
        "factura": "Factura electronica afecta",
        "none": "Comprobante de venta",
    }
    ticket_url = reverse("pos:ticket", args=[order.id])
    return {
        "order_id": order.id,
        "ticket_number": ticket_number,
        "ticket_url": ticket_url,
        "print_url": f"{ticket_url}?print=1",
        "document_label": document_labels.get(document_choice, "Comprobante de venta"),
        "total": format_clp(order.total),
        "change_due": format_clp(change_due),
        "has_change": bool(change_due),
    }


def _emit_sii_document(request, order, document_choice):
    customer_data = _build_sii_customer_data(request)
    try:
        if document_choice == "factura":
            document = emitir_factura(order, user=request.user, request=request, customer_data=customer_data)
        else:
            document = emitir_boleta(order, user=request.user, request=request, customer_data=customer_data)
    except SiiError as exc:
        messages.warning(request, f"Venta cerrada, pero el DTE no pudo iniciarse: {exc}")
        return None

    if document.status == DteStatus.SIGNED:
        messages.success(request, f"DTE {document.get_document_type_display()} folio {document.folio} generado y firmado.")
    elif document.status in {DteStatus.SENT, DteStatus.ACCEPTED, DteStatus.ACCEPTED_WITH_REPAIRS}:
        messages.success(request, f"DTE {document.get_document_type_display()} folio {document.folio} emitido.")
    else:
        messages.warning(request, f"DTE {document.get_document_type_display()} folio {document.folio} quedo pendiente: {document.error_message or document.get_status_display()}.")
    return document


def _build_sii_customer_data(request):
    return {
        "rut": request.POST.get("customer_rut", "").strip(),
        "legal_name": request.POST.get("customer_legal_name", "").strip(),
        "business_activity": request.POST.get("customer_business_activity", "").strip(),
        "address": request.POST.get("customer_address", "").strip(),
        "comuna": request.POST.get("customer_comuna", "").strip(),
        "city": request.POST.get("customer_city", "").strip(),
        "email": request.POST.get("customer_email", "").strip(),
    }


def _build_payment_entries(request, total):
    allowed_methods = {Payment.Method.CASH, Payment.Method.CARD, Payment.Method.TRANSFER}
    selected_methods = [method for method in request.POST.getlist("payment_methods") if method in allowed_methods]
    if not selected_methods:
        legacy_method = request.POST.get("payment_method", Payment.Method.CASH)
        if legacy_method in Payment.Method.values:
            selected_methods = [legacy_method]

    entries = []
    for method in dict.fromkeys(selected_methods):
        if method not in allowed_methods:
            continue
        amount_key = f"amount_received_{method}"
        fallback_amount = request.POST.get("amount_received") if method == Payment.Method.CASH else total
        tendered_amount = round_peso(to_decimal(request.POST.get(amount_key) or fallback_amount or "0"))
        if tendered_amount > 0:
            entries.append({"method": method, "tendered_amount": tendered_amount})

    amount_received = sum((entry["tendered_amount"] for entry in entries), Decimal("0"))
    change_due = max(amount_received - total, Decimal("0"))
    return entries, amount_received, change_due


@transaction.atomic
def _void_sale(request, cash_session):
    if not cash_session:
        messages.error(request, "No hay caja abierta para anular la venta.")
        return
    return_reason = request.POST.get("return_reason", "").strip()
    if not return_reason:
        messages.error(request, "Debes ingresar un motivo para registrar la devolucion.")
        return

    pos_config = _get_pos_configuration(cash_session)
    if not pos_config or pos_config.require_supervisor_return_code:
        expected_code = pos_config.supervisor_return_code if pos_config else "1234"
        supervisor_code = request.POST.get("supervisor_return_code", "").strip()
        if supervisor_code != expected_code:
            messages.error(request, "Se requiere codigo supervisor de 4 digitos para registrar la devolucion.")
            return

    order = (
        Order.objects.select_for_update()
        .select_related("cash_session")
        .prefetch_related("lines", "payments")
        .filter(id=request.POST.get("order_id"), cash_session=cash_session, channel=Order.Channel.POS)
        .first()
    )
    if not order:
        messages.error(request, "Venta no encontrada para esta caja.")
        return
    if order.status == Order.Status.CANCELLED:
        messages.info(request, f"La venta #{order.id} ya estaba anulada.")
        return
    if order.status != Order.Status.PAID:
        messages.error(request, "Solo se pueden anular ventas pagadas.")
        return

    for line in order.lines.select_related("variant"):
        stock_item = StockItem.objects.select_for_update().filter(branch=order.branch, variant=line.variant).first()
        if not stock_item:
            continue
        stock_item.quantity += line.quantity
        stock_item.save(update_fields=["quantity", "updated_at"])
        InventoryMovement.objects.create(
            stock_item=stock_item,
            movement_type=InventoryMovement.MovementType.ADJUSTMENT,
            quantity=line.quantity,
            unit_cost=stock_item.average_cost,
            reference=f"void_order:{order.id}",
            notes=f"Reposicion automatica por devolucion TPV. Motivo: {return_reason}",
            created_by=request.user,
        )

    cash_total = sum((payment.amount for payment in order.payments.filter(method=Payment.Method.CASH, status=Payment.Status.PAID)), Decimal("0"))
    if cash_total:
        cash_session.expected_cash_amount -= cash_total
        cash_session.save(update_fields=["expected_cash_amount", "updated_at"])
        CashMovement.objects.create(
            session=cash_session,
            movement_type=CashMovement.MovementType.EXPENSE,
            amount=cash_total,
            reason=f"Devolucion TPV orden #{order.id}: {return_reason}"[:180],
            created_by=request.user,
        )

    order.payments.filter(status=Payment.Status.PAID).update(status=Payment.Status.REFUNDED)
    if hasattr(order, "tax_document"):
        order.tax_document.status = TaxDocument.Status.VOID
        payload = order.tax_document.payload or {}
        payload["void_reason"] = return_reason
        payload["void_source"] = "pos_return"
        order.tax_document.payload = payload
        order.tax_document.save(update_fields=["status", "payload", "updated_at"])
    order.status = Order.Status.CANCELLED
    order.notes = f"{order.notes}\nDevolucion/anulacion registrada desde TPV. Motivo: {return_reason}".strip()
    order.save(update_fields=["status", "notes", "updated_at"])
    messages.success(request, f"Devolucion de venta #{order.id} registrada correctamente.")


@transaction.atomic
def _close_cash_session(request, cash_session):
    if not cash_session:
        messages.error(request, "No hay caja abierta para cerrar.")
        return

    counted_amount = round_peso(request.POST.get("counted_cash_amount", "0"))
    difference = counted_amount - cash_session.expected_cash_amount
    difference_type = _cash_difference_type(difference)
    supervisor_code = request.POST.get("supervisor_close_code", "").strip()
    pos_config = _get_pos_configuration(cash_session)
    requires_supervisor = difference != 0 and (not pos_config or pos_config.require_supervisor_close_code)
    supervisor_approved = False
    supervisor_at = None
    supervisor_by = None

    if requires_supervisor:
        expected_code = pos_config.supervisor_close_code if pos_config else "1234"
        if supervisor_code != expected_code:
            messages.error(
                request,
                f"Diferencia de caja {format_clp(difference)}. Se requiere codigo supervisor de 4 digitos para cerrar.",
            )
            return
        supervisor_approved = True
        supervisor_at = timezone.now()
        supervisor_by = request.user

    cash_session.counted_cash_amount = counted_amount
    cash_session.closing_difference_amount = difference
    cash_session.closing_difference_type = difference_type
    cash_session.supervisor_override_required = requires_supervisor
    cash_session.supervisor_override_approved = supervisor_approved
    cash_session.supervisor_override_at = supervisor_at
    cash_session.supervisor_override_by = supervisor_by
    cash_session.closed_by = request.user
    cash_session.closed_at = timezone.now()
    cash_session.status = CashSession.Status.CLOSED
    close_note = request.POST.get("close_notes", "").strip()
    if close_note:
        cash_session.notes = close_note
    cash_session.save(
        update_fields=[
            "counted_cash_amount",
            "closing_difference_amount",
            "closing_difference_type",
            "supervisor_override_required",
            "supervisor_override_approved",
            "supervisor_override_at",
            "supervisor_override_by",
            "closed_by",
            "closed_at",
            "status",
            "notes",
            "updated_at",
        ]
    )
    request.session[CART_SESSION_KEY] = {}
    if requires_supervisor:
        messages.warning(request, f"Caja cerrada con autorizacion supervisor. Diferencia: {format_clp(difference)}.")
    elif difference:
        messages.warning(request, f"Caja cerrada con diferencia. Diferencia: {format_clp(difference)}.")
    else:
        messages.success(request, "Caja cerrada sin diferencias.")


def _cash_difference_type(difference):
    if difference < 0:
        return CashSession.DifferenceType.SHORT
    if difference > 0:
        return CashSession.DifferenceType.OVER
    return CashSession.DifferenceType.BALANCED


@login_required
def logout_terminal(request):
    cash_session = _get_open_cash_session(request.user, request.current_company)
    if cash_session:
        messages.error(request, "Debes cerrar y cuadrar la caja antes de cerrar sesion.")
        return redirect("pos:terminal")
    logout(request)
    return redirect("home")


@login_required
def ticket(request, order_id):
    orders = (
        Order.objects.select_related("branch", "branch__company", "pos_ticket")
        .prefetch_related("lines", "payments")
        .filter(channel=Order.Channel.POS)
    )
    if not request.user.is_superuser:
        orders = orders.filter(company=request.current_company) if request.current_company else orders.none()
    order = get_object_or_404(
        orders,
        id=order_id,
    )
    try:
        config = order.branch.pos_configuration
    except PosConfiguration.DoesNotExist:
        config = None
    paper_width = config.receipt_paper_width if config else PosConfiguration.ReceiptPaperWidth.MM_80
    electronic_document = _ticket_electronic_document(order)
    use_printer_agent = bool(config and config.print_method == PosConfiguration.PrintMethod.AGENT)
    printer_payload = None
    printer_error = ""
    if use_printer_agent:
        try:
            printer_payload = build_printer_payload(order, config, electronic_document)
        except ValueError as exc:
            printer_error = str(exc)
    return render(
        request,
        "pos/ticket.html",
        {
            "order": order,
            "ticket": order.pos_ticket,
            "config": config,
            "auto_print": request.GET.get("print") == "1" or bool(config and config.auto_print_receipt),
            "paper_width": paper_width,
            "paper_class": f"paper-{paper_width}",
            "electronic_document": electronic_document,
            "use_printer_agent": use_printer_agent,
            "printer_payload": printer_payload,
            "printer_error": printer_error,
        },
    )


def _ticket_electronic_document(order):
    valid_statuses = {
        DteStatus.SIGNED,
        DteStatus.SENT,
        DteStatus.ACCEPTED,
        DteStatus.ACCEPTED_WITH_REPAIRS,
    }
    document = order.sii_documents.filter(status__in=valid_statuses).order_by("-created_at").first()
    if not document:
        document = order.sii_documents.order_by("-created_at").first()

    try:
        settings = order.company.sii_settings
    except order.company.__class__.sii_settings.RelatedObjectDoesNotExist:
        settings = None

    type_labels = {
        DteType.BOLETA_ELECTRONICA: "Boleta electronica afecta",
        DteType.BOLETA_EXENTA: "Boleta electronica exenta",
        DteType.FACTURA_ELECTRONICA: "Factura electronica afecta",
        DteType.FACTURA_EXENTA: "Factura electronica exenta",
        DteType.GUIA_DESPACHO: "Guia de despacho electronica",
        DteType.NOTA_DEBITO: "Nota de debito electronica",
        DteType.NOTA_CREDITO: "Nota de credito electronica",
    }
    address_parts = []
    if settings:
        for value in (settings.address, settings.comuna, settings.city):
            clean_value = str(value or "").strip()
            if clean_value and clean_value.casefold() not in {part.casefold() for part in address_parts}:
                address_parts.append(clean_value)
    if not address_parts:
        address_parts = [part for part in (order.branch.address, order.branch.city) if part]

    ted_xml = extract_ted_xml(document) if document else ""
    resolution_date = settings.sii_resolution_date.strftime("%d/%m/%Y") if settings and settings.sii_resolution_date else ""
    return {
        "document": document,
        "is_electronic": bool(document and document.folio),
        "legal_name": (settings.legal_name if settings else "") or order.company.legal_name or order.company.name,
        "rut": (settings.rut if settings else "") or order.company.tax_id,
        "business_activity": settings.business_activity if settings else "",
        "address": ", ".join(address_parts),
        "type_label": type_labels.get(document.document_type, document.get_document_type_display()) if document else "Comprobante de venta",
        "folio": document.folio if document and document.folio else order.pos_ticket.ticket_number,
        "issued_at": (document.issued_at if document and document.issued_at else order.created_at),
        "ted_xml": ted_xml,
        "pdf417_data_uri": render_pdf417_data_uri(ted_xml),
        "resolution_number": settings.sii_resolution_number if settings else "",
        "resolution_date": resolution_date,
        "environment": settings.get_environment_display() if settings else "",
        "is_certification": bool(settings and settings.environment == "certification"),
        "legal_footer": settings.legal_footer if settings else "",
    }


def _to_decimal(value):
    return to_decimal(value).quantize(Decimal("0.001"))


def _empty_totals():
    return {
        "subtotal": Decimal("0"),
        "product_discount_total": Decimal("0"),
        "sale_discount_total": Decimal("0"),
        "discount_total": Decimal("0"),
        "discounted_subtotal": Decimal("0"),
        "tax_total": Decimal("0"),
        "total": Decimal("0"),
        "rounding_adjustment": Decimal("0"),
    }
