import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, request, session, url_for
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

from models import Coupon, GoKwikCheckoutSession, Order, OrderItem, ProductVariant, Setting, User, db
from routes.main import _validate_coupon, calculate_shipping


gokwik_storefront_bp = Blueprint('gokwik_storefront', __name__)
gokwik_api_bp = Blueprint('gokwik_api', __name__, url_prefix='/api/gokwik/v1')

MONEY_QUANTUM = Decimal('0.01')
CHECKOUT_TTL = timedelta(minutes=30)
SUPPORTED_PAYMENT_METHODS = {'cod', 'gokwik_prepaid'}


class GoKwikAPIError(Exception):
    """A client-safe GoKwik API failure."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@gokwik_api_bp.errorhandler(GoKwikAPIError)
@gokwik_storefront_bp.errorhandler(GoKwikAPIError)
def handle_gokwik_api_error(error: GoKwikAPIError):
    return jsonify({'code': error.code, 'message': error.message}), error.status_code


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise GoKwikAPIError('gc_invalid_amount', 'An amount is invalid.', 400) from error
    if not amount.is_finite():
        raise GoKwikAPIError('gc_invalid_amount', 'An amount is invalid.', 400)
    return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _request_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _request_parameter(name: str) -> Any:
    payload = _request_payload()
    if name in payload:
        return payload[name]
    if name in request.form:
        return request.form.get(name)
    return request.args.get(name)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes'}


def _secure_header_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = request.headers.get(name)
        if value:
            return value.strip()
    return ''


def require_gokwik_auth(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if not current_app.config.get('GOKWIK_ENABLED'):
            raise GoKwikAPIError('gc_checkout_disabled', 'GoKwik Checkout is disabled.', 503)

        supplied_app_id = _secure_header_value(('appid', 'app-id', 'gk-app-id'))
        supplied_app_secret = _secure_header_value(('appsecret', 'app-secret', 'gk-app-secret'))
        expected_app_id = current_app.config.get('GOKWIK_APP_ID') or ''
        expected_app_secret = current_app.config.get('GOKWIK_APP_SECRET') or ''

        authenticated = (
            bool(supplied_app_id)
            and bool(supplied_app_secret)
            and hmac.compare_digest(supplied_app_id, expected_app_id)
            and hmac.compare_digest(supplied_app_secret, expected_app_secret)
        )
        if not authenticated:
            raise GoKwikAPIError('gc_authentication_failed', 'Invalid GoKwik credentials.', 401)
        return view(*args, **kwargs)

    return wrapped


def _checkout_by_id(checkout_id: str, lock: bool) -> GoKwikCheckoutSession:
    if not checkout_id:
        raise GoKwikAPIError('gc_missing_session_key', 'Session key is missing.', 400)

    query = GoKwikCheckoutSession.query.filter_by(id=checkout_id)
    if lock:
        query = query.with_for_update()
    checkout = query.first()
    if not checkout:
        raise GoKwikAPIError('gc_cart_not_found', 'Checkout session was not found.', 404)
    if checkout.status == 'expired' or checkout.expires_at <= _utcnow():
        raise GoKwikAPIError('gc_cart_expired', 'Checkout session has expired.', 410)
    return checkout


def _decoded_json_object(value: str, field_name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise GoKwikAPIError(
            'gc_corrupt_checkout_session',
            f'Checkout {field_name} is invalid.',
            500,
        ) from error
    if not isinstance(decoded, dict):
        raise GoKwikAPIError('gc_corrupt_checkout_session', f'Checkout {field_name} is invalid.', 500)
    return decoded


def _cart_lines(
    checkout: GoKwikCheckoutSession,
    lock_variants: bool,
) -> list[tuple[ProductVariant, int, Decimal]]:
    cart_data = _decoded_json_object(checkout.cart_data, 'cart data')
    if not cart_data:
        raise GoKwikAPIError('gc_cart_has_no_items', 'Cart is empty.', 400)

    lines: list[tuple[ProductVariant, int, Decimal]] = []
    for raw_variant_id, raw_quantity in cart_data.items():
        try:
            variant_id = int(raw_variant_id)
            quantity = int(raw_quantity)
        except (TypeError, ValueError) as error:
            raise GoKwikAPIError('gc_corrupt_checkout_session', 'Cart data is invalid.', 500) from error
        if quantity <= 0:
            raise GoKwikAPIError('gc_corrupt_checkout_session', 'Cart quantity is invalid.', 500)

        query = ProductVariant.query.filter_by(id=variant_id)
        if lock_variants:
            query = query.with_for_update()
        variant = query.first()
        if not variant:
            raise GoKwikAPIError('gc_product_not_found', 'A cart item no longer exists.', 409)
        if (variant.stock_quantity or 0) < quantity:
            raise GoKwikAPIError(
                'gc_insufficient_stock',
                f'Only {variant.stock_quantity or 0} unit(s) of {variant.product.name} are available.',
                409,
            )
        unit_price = _money(variant.price)
        lines.append((variant, quantity, unit_price * quantity))
    return lines


def _cart_amounts(
    checkout: GoKwikCheckoutSession,
    lines: list[tuple[ProductVariant, int, Decimal]],
) -> tuple[Decimal, Decimal, Decimal, Decimal, int | None]:
    subtotal = sum((line_total for _, _, line_total in lines), Decimal('0.00'))
    settings = {setting.key: setting.value for setting in Setting.query.all()}
    shipping = _money(calculate_shipping(float(subtotal), settings))
    discount = Decimal('0.00')
    coupon_id = None

    if checkout.coupon_code:
        coupon_result = _validate_coupon(checkout.coupon_code, float(subtotal))
        if not coupon_result.get('valid'):
            raise GoKwikAPIError(
                'gc_cart_coupon_invalid',
                str(coupon_result.get('message') or 'Coupon is not valid.'),
                409,
            )
        discount = _money(coupon_result['discount'])
        coupon_id = int(coupon_result['coupon_id'])

    total = subtotal - discount + shipping
    return subtotal, discount, shipping, total, coupon_id


def _product_image_url(variant: ProductVariant) -> str:
    image_path = ''
    if variant.product.images:
        image_path = variant.product.images.split(',')[0].strip()
    if image_path.startswith(('https://', 'http://')):
        return image_path
    if image_path.startswith('/static/'):
        image_path = image_path[len('/static/'):]
    elif image_path.startswith('static/'):
        image_path = image_path[len('static/'):]
    if not image_path:
        image_path = 'images/banner/zuhran_2.webp'
    return url_for('static', filename=image_path, _external=True)


def _cart_payload(checkout: GoKwikCheckoutSession) -> dict[str, Any]:
    lines = _cart_lines(checkout, False)
    subtotal, discount, shipping, total, _ = _cart_amounts(checkout, lines)
    customer = _decoded_json_object(checkout.customer_data, 'customer data')
    items: list[dict[str, Any]] = []

    for variant, quantity, line_total in lines:
        regular_price = _money(variant.original_price if variant.original_price is not None else variant.price)
        unit_price = _money(variant.price)
        items.append({
            'key': str(variant.id),
            'product_id': variant.product_id,
            'variation_id': variant.id,
            'quantity': quantity,
            'line_subtotal': float(line_total),
            'line_total': float(line_total),
            'product_data': {
                'id': variant.product_id,
                'variation_id': variant.id,
                'name': variant.product.name,
                'slug': variant.product.slug,
                'sku': '',
                'price': float(unit_price),
                'regular_price': float(regular_price),
                'sale_price': float(unit_price),
                'salable_qty': variant.stock_quantity or 0,
                'stock_status': 'IN_STOCK' if (variant.stock_quantity or 0) >= quantity else 'OUT_OF_STOCK',
                'images': [{
                    'id': 0,
                    'src': _product_image_url(variant),
                    'name': variant.product.name,
                    'alt': variant.product.name,
                }],
            },
            'currency': '₹',
        })

    coupon_applied = [checkout.coupon_code] if checkout.coupon_code else []
    return {
        'user_id': checkout.user_id or 0,
        'customer_email': customer.get('email'),
        'customer': customer,
        'items': items,
        'coupon_applied': coupon_applied,
        'chosen_shipping_method': ['standard'],
        'chosen_payment_method': '',
        'shipping_methods': [{
            'method_id': 'standard',
            'rate_id': 'standard',
            'instance_id': 0,
            'method_name': 'Standard shipping',
            'charge': float(shipping),
            'tax_cost': 0.0,
            'taxes': [],
        }],
        'totals': {
            'subtotal': float(subtotal),
            'subtotal_tax': 0.0,
            'shipping_total': float(shipping),
            'shipping_tax': 0.0,
            'shipping_taxes': [],
            'discount_total': float(discount),
            'discount_tax': 0.0,
            'cart_contents_total': float(subtotal - discount),
            'cart_contents_tax': 0.0,
            'fee_total': 0.0,
            'fee_tax': 0.0,
            'total': float(total),
            'total_tax': 0.0,
        },
        'plugin_version': 'zuhraan-flask-1.0.0',
    }


def _initial_customer_data() -> dict[str, Any]:
    if not current_user.is_authenticated:
        return {'country': 'IN'}
    email = current_user.email or ''
    display_name = (current_user.name or '').strip()
    first_name, _, last_name = display_name.partition(' ')
    country = current_user.country or 'India'
    return {
        'id': current_user.id,
        'first_name': first_name,
        'last_name': last_name,
        'email': email,
        'phone': current_user.phone or '',
        'address_1': current_user.address_line1 or '',
        'address_2': current_user.address_line2 or '',
        'city': current_user.city or '',
        'state': current_user.state or '',
        'postcode': current_user.pincode or '',
        'country': 'IN' if country.lower() == 'india' else country,
    }


@gokwik_storefront_bp.route('/api/gokwik/checkout-session', methods=['POST'])
def create_checkout_session():
    if not current_app.config.get('GOKWIK_STOREFRONT_ENABLED'):
        raise GoKwikAPIError('gc_checkout_disabled', 'GoKwik Checkout is disabled.', 503)

    browser_cart = session.get('cart', {})
    if not isinstance(browser_cart, dict) or not browser_cart:
        raise GoKwikAPIError('gc_cart_has_no_items', 'Cart is empty.', 400)

    normalized_cart: dict[str, int] = {}
    for raw_variant_id, raw_quantity in browser_cart.items():
        try:
            variant_id = int(raw_variant_id)
            quantity = int(raw_quantity)
        except (TypeError, ValueError) as error:
            raise GoKwikAPIError('gc_invalid_cart', 'Cart contains invalid data.', 400) from error
        if quantity <= 0:
            raise GoKwikAPIError('gc_invalid_cart', 'Cart contains an invalid quantity.', 400)
        normalized_cart[str(variant_id)] = quantity

    checkout = GoKwikCheckoutSession(
        id=secrets.token_urlsafe(32),
        user_id=current_user.id if current_user.is_authenticated else None,
        cart_data=json.dumps(normalized_cart, separators=(',', ':')),
        customer_data=json.dumps(_initial_customer_data(), separators=(',', ':')),
        status='pending',
        expires_at=_utcnow() + CHECKOUT_TTL,
    )
    _cart_lines(checkout, False)

    try:
        db.session.add(checkout)
        db.session.commit()
    except SQLAlchemyError as error:
        db.session.rollback()
        current_app.logger.exception('Unable to create GoKwik checkout session.')
        raise GoKwikAPIError(
            'gc_checkout_session_error',
            'Unable to start checkout. Please try again.',
            500,
        ) from error

    return jsonify({
        'checkout_id': checkout.id,
        'environment': current_app.config['GOKWIK_ENV'],
        'merchant_id': current_app.config['GOKWIK_MERCHANT_ID'],
    }), 201


@gokwik_storefront_bp.route('/api/gokwik/checkout-complete', methods=['POST'])
def complete_checkout():
    payload = _request_payload()
    checkout_id = str(payload.get('checkout_id') or '').strip()
    merchant_order_id = str(payload.get('merchant_order_id') or '').strip()
    checkout = _checkout_by_id(checkout_id, False)

    if checkout.status != 'completed' or not checkout.order_id:
        raise GoKwikAPIError('gc_order_not_complete', 'Order is not complete yet.', 409)
    if merchant_order_id and merchant_order_id != str(checkout.order_id):
        raise GoKwikAPIError('gc_order_mismatch', 'Order does not match this checkout.', 409)

    session['cart'] = {}
    redirect_url = url_for('main.account') if current_user.is_authenticated else url_for('main.index')
    return jsonify({'status': 'success', 'redirect_url': redirect_url})


@gokwik_api_bp.route('/cart/health-check', methods=['GET', 'POST'])
@require_gokwik_auth
def health_check():
    return jsonify({
        'status': 'success',
        'message': 'API is operational.',
        'timestamp': _utcnow().isoformat(timespec='seconds') + 'Z',
        'application': 'zuhraan-flask',
        'integration_version': '1.0.0',
        'environment': current_app.config['GOKWIK_ENV'],
    })


@gokwik_api_bp.route('/cart', methods=['POST'])
@require_gokwik_auth
def get_cart():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    checkout = _checkout_by_id(checkout_id, False)
    return jsonify(_cart_payload(checkout))


@gokwik_api_bp.route('/cart/get-coupons', methods=['GET'])
@require_gokwik_auth
def get_coupons():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    checkout = _checkout_by_id(checkout_id, False)
    lines = _cart_lines(checkout, False)
    subtotal = sum((line_total for _, _, line_total in lines), Decimal('0.00'))
    coupons: list[dict[str, Any]] = []

    for coupon in Coupon.query.filter_by(is_active=True).all():
        result = _validate_coupon(coupon.code, float(subtotal))
        if not result.get('valid'):
            continue
        coupons.append({
            'code': coupon.code,
            'amount': float(_money(result['discount'])),
            'discount_value': coupon.discount_value,
            'discount_type': coupon.discount_type,
            'description': result['message'],
        })
    return jsonify({'coupons': coupons})


@gokwik_api_bp.route('/cart/apply-coupon', methods=['POST'])
@require_gokwik_auth
def apply_coupon():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    coupon_code = str(_request_parameter('coupon') or '').strip().upper()
    if not coupon_code:
        raise GoKwikAPIError('gc_cart_coupon_is_required', 'Coupon code is required.', 400)

    checkout = _checkout_by_id(checkout_id, True)
    lines = _cart_lines(checkout, False)
    subtotal = sum((line_total for _, _, line_total in lines), Decimal('0.00'))
    result = _validate_coupon(coupon_code, float(subtotal))
    if not result.get('valid'):
        raise GoKwikAPIError('gc_cart_coupon_invalid', str(result['message']), 400)

    checkout.coupon_code = coupon_code
    db.session.commit()
    return jsonify({
        'message': 'Coupon was successfully added to cart.',
        'cart': _cart_payload(checkout),
    })


@gokwik_api_bp.route('/cart/remove-coupon', methods=['POST'])
@require_gokwik_auth
def remove_coupon():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    checkout = _checkout_by_id(checkout_id, True)
    checkout.coupon_code = None
    db.session.commit()
    return jsonify({
        'message': 'Coupon was successfully removed from cart.',
        'cart': _cart_payload(checkout),
    })


@gokwik_api_bp.route('/cart/set-address', methods=['POST'])
@require_gokwik_auth
def set_address():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    checkout = _checkout_by_id(checkout_id, True)
    customer = _decoded_json_object(checkout.customer_data, 'customer data')
    supported_fields = (
        'first_name',
        'last_name',
        'phone',
        'email',
        'address_1',
        'address_2',
        'city',
        'state',
        'postcode',
        'country',
    )
    changed = False
    for field in supported_fields:
        value = _request_parameter(field)
        if value is not None and str(value).strip():
            customer[field] = str(value).strip()
            changed = True
    customer_email = _request_parameter('customerEmail')
    if customer_email:
        customer['email'] = str(customer_email).strip().lower()
        changed = True

    checkout.customer_data = json.dumps(customer, separators=(',', ':'))
    db.session.commit()
    return jsonify({
        'message': 'Address successfully updated.' if changed else 'No changes requested.',
        'cart': _cart_payload(checkout),
    })


@gokwik_api_bp.route('/cart/set-shipping-method', methods=['POST'])
@require_gokwik_auth
def set_shipping_method():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    checkout = _checkout_by_id(checkout_id, True)
    shipping_methods = _request_parameter('shipping_methods')
    if isinstance(shipping_methods, list):
        selected_method = str(shipping_methods[0]) if shipping_methods else ''
    else:
        selected_method = str(shipping_methods or '')
    if selected_method and selected_method != 'standard':
        raise GoKwikAPIError('gc_invalid_shipping_method', 'Shipping method is invalid.', 400)
    return jsonify({
        'message': 'Shipping method successfully updated.' if selected_method else 'No changes requested.',
        'cart': _cart_payload(checkout),
    })


@gokwik_api_bp.route('/cart/get-wallet-balance', methods=['POST'])
@require_gokwik_auth
def get_wallet_balance():
    customer_email = str(_request_parameter('customer_email') or '').strip().lower()
    if not customer_email:
        raise GoKwikAPIError('gc_missing_customer_email', 'Customer email is missing.', 400)
    user = User.query.filter(db.func.lower(User.email) == customer_email).first()
    return jsonify({
        'customer_id': user.id if user else 0,
        'wallet_balance': '0.00',
    })


@gokwik_api_bp.route('/cart/deduct-wallet-balance', methods=['POST'])
@require_gokwik_auth
def deduct_wallet_balance():
    raise GoKwikAPIError(
        'gc_wallet_not_configured',
        'Store wallet payments are not configured.',
        400,
    )


def _normalized_address(payload: Any, fallback: dict[str, Any]) -> dict[str, str]:
    provided = payload if isinstance(payload, dict) else {}
    normalized: dict[str, str] = {}
    for field in (
        'first_name',
        'last_name',
        'email',
        'phone',
        'address_1',
        'address_2',
        'city',
        'state',
        'postcode',
        'country',
    ):
        value = provided.get(field)
        if value is None or not str(value).strip():
            value = fallback.get(field, '')
        normalized[field] = str(value).strip()
    return normalized


def _validate_order_address(address: dict[str, str]) -> None:
    required_fields = ('first_name', 'email', 'phone', 'address_1', 'city', 'state', 'postcode')
    missing_fields = [field for field in required_fields if not address.get(field)]
    if missing_fields:
        raise GoKwikAPIError(
            'gc_missing_address_fields',
            'Missing required address fields: ' + ', '.join(missing_fields),
            400,
        )


@gokwik_api_bp.route('/cart/place-order', methods=['POST'])
@require_gokwik_auth
def place_order():
    payload = _request_payload()
    checkout_id = str(payload.get('session_key') or '').strip()
    payment_method = str(payload.get('payment_method') or '').strip().lower()
    if payment_method not in SUPPORTED_PAYMENT_METHODS:
        raise GoKwikAPIError('gc_cart_invalid_payment_method', 'Payment method is invalid.', 400)
    if payload.get('fee_lines'):
        raise GoKwikAPIError(
            'gc_unsupported_fee_lines',
            'GoKwik fee lines are not configured for this merchant.',
            400,
        )

    try:
        checkout = _checkout_by_id(checkout_id, True)
        if checkout.order_id:
            return jsonify({'id': checkout.order_id}), 200

        customer = _decoded_json_object(checkout.customer_data, 'customer data')
        billing = _normalized_address(payload.get('billing'), customer)
        shipping = _normalized_address(payload.get('shipping'), billing)
        _validate_order_address(billing)
        _validate_order_address(shipping)

        lines = _cart_lines(checkout, True)
        subtotal, _, shipping_charge, total, coupon_id = _cart_amounts(checkout, lines)
        provided_total = payload.get('order_total')
        if provided_total is not None and abs(_money(provided_total) - total) > MONEY_QUANTUM:
            raise GoKwikAPIError(
                'gc_cart_total_mismatch',
                'Cart total does not match the order total.',
                400,
            )

        transaction_id = str(payload.get('transaction_id') or '').strip() or None
        paid = payment_method == 'gokwik_prepaid' and _is_truthy(payload.get('set_paid'))
        if paid and not transaction_id:
            raise GoKwikAPIError(
                'gc_missing_transaction_id',
                'A paid order requires a transaction ID.',
                400,
            )

        customer_name = ' '.join(
            part for part in (shipping['first_name'], shipping['last_name']) if part
        )
        country = 'India' if shipping['country'].upper() == 'IN' else shipping['country']
        full_address = ', '.join(filter(None, [
            shipping['address_1'],
            shipping['address_2'],
            shipping['city'],
            shipping['state'],
            shipping['postcode'],
            country,
        ]))
        order = Order(
            user_id=checkout.user_id,
            customer_name=customer_name,
            customer_email=shipping['email'] or billing['email'],
            customer_phone=shipping['phone'] or billing['phone'],
            shipping_address=full_address,
            address_line1=shipping['address_1'],
            address_line2=shipping['address_2'],
            city=shipping['city'],
            state=shipping['state'],
            pincode=shipping['postcode'],
            country=country,
            total_amount=float(total),
            shipping_charges=float(shipping_charge),
            status='processing' if paid else 'pending',
            payment_status='paid' if paid else 'unpaid',
            payment_method=payment_method,
            gokwik_transaction_id=transaction_id,
            coupon_id=coupon_id,
        )
        db.session.add(order)
        db.session.flush()

        for variant, quantity, _ in lines:
            db.session.add(OrderItem(
                order_id=order.id,
                variant_id=variant.id,
                quantity=quantity,
                price_at_time=float(_money(variant.price)),
            ))
            variant.stock_quantity = (variant.stock_quantity or 0) - quantity

        if coupon_id:
            coupon = Coupon.query.filter_by(id=coupon_id).with_for_update().first()
            if not coupon or (coupon.max_uses and coupon.used_count >= coupon.max_uses):
                raise GoKwikAPIError('gc_cart_coupon_invalid', 'Coupon usage limit reached.', 409)
            coupon.used_count = (coupon.used_count or 0) + 1

        if checkout.user_id:
            user = User.query.filter_by(id=checkout.user_id).first()
            if user:
                user.phone = order.customer_phone
                user.address_line1 = order.address_line1
                user.address_line2 = order.address_line2
                user.city = order.city
                user.state = order.state
                user.pincode = order.pincode
                user.country = order.country

        checkout.order_id = order.id
        checkout.status = 'completed'
        checkout.updated_at = _utcnow()
        db.session.commit()
        return jsonify({'id': order.id}), 200
    except GoKwikAPIError:
        db.session.rollback()
        raise
    except SQLAlchemyError as error:
        db.session.rollback()
        current_app.logger.exception(
            'Unable to create GoKwik order.',
            extra={'gokwik_checkout_id': checkout_id},
        )
        raise GoKwikAPIError(
            'gc_cart_place_order_error',
            'Unable to create order. Please retry.',
            500,
        ) from error


@gokwik_api_bp.route('/cart/check-order-exists', methods=['POST'])
@require_gokwik_auth
def check_order_exists():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    customer_email = str(_request_parameter('customer_email') or '').strip().lower()
    checkout = _checkout_by_id(checkout_id, False)
    if not checkout.order_id:
        raise GoKwikAPIError('gc_order_not_found', 'No order found.', 404)

    order = Order.query.filter_by(id=checkout.order_id).first()
    if not order or (customer_email and (order.customer_email or '').lower() != customer_email):
        raise GoKwikAPIError('gc_order_not_found', 'No order found.', 404)
    return jsonify({'message': 'Order exists.', 'order_id': order.id})


@gokwik_api_bp.route('/cart/update-order-status', methods=['POST'])
@require_gokwik_auth
def update_order_status():
    merchant_order_id = _request_parameter('merchant_order_id')
    order_status = str(_request_parameter('order_status') or '').strip().lower()
    if not merchant_order_id or order_status not in {'pending', 'processing', 'shipped', 'delivered', 'cancelled'}:
        raise GoKwikAPIError('gc_invalid_order_status', 'Invalid order status provided.', 400)

    order = Order.query.filter_by(id=merchant_order_id).first()
    checkout = GoKwikCheckoutSession.query.filter_by(order_id=merchant_order_id).first()
    if not order or not checkout:
        raise GoKwikAPIError('gc_order_not_found', 'Order not found.', 404)
    old_status = order.status
    order.status = order_status
    db.session.commit()
    return jsonify({
        'message': 'Order status updated successfully.',
        'order_id': order.id,
        'old_status': old_status,
        'new_status': order.status,
    })


@gokwik_api_bp.route('/cart/remove-out-of-stock-items', methods=['POST'])
@require_gokwik_auth
def remove_out_of_stock_items():
    checkout_id = str(_request_parameter('session_key') or '').strip()
    checkout = _checkout_by_id(checkout_id, True)
    cart_data = _decoded_json_object(checkout.cart_data, 'cart data')
    updated_cart: dict[str, int] = {}
    changed = False

    for raw_variant_id, raw_quantity in cart_data.items():
        variant = ProductVariant.query.filter_by(id=int(raw_variant_id)).first()
        stock = (variant.stock_quantity or 0) if variant else 0
        quantity = int(raw_quantity)
        if stock <= 0:
            changed = True
            continue
        updated_quantity = min(quantity, stock)
        if updated_quantity != quantity:
            changed = True
        updated_cart[str(raw_variant_id)] = updated_quantity

    checkout.cart_data = json.dumps(updated_cart, separators=(',', ':'))
    db.session.commit()
    if not updated_cart:
        raise GoKwikAPIError('gc_cart_has_no_items', 'All cart items are out of stock.', 409)
    return jsonify({
        'status': 'success',
        'message': (
            'Out-of-stock items have been removed from the cart'
            if changed
            else 'No out-of-stock items in the cart'
        ),
        'cart': _cart_payload(checkout),
    })
