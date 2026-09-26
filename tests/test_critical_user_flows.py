import os
import hashlib
import hmac
import json

os.environ['SUPABASE_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['RAZORPAY_KEY_ID'] = 'rzp_test_example'
os.environ['RAZORPAY_KEY_SECRET'] = 'test-api-secret'
os.environ['RAZORPAY_WEBHOOK_SECRET'] = 'test-webhook-secret'

import pytest

from app import app
from models import Category, Coupon, Order, Product, ProductVariant, Setting, User, db
from routes.main import calculate_shipping


@pytest.fixture(autouse=True)
def isolated_database():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client():
    return app.test_client()


def create_product(price):
    category = Category(name='Test Category', slug='test-category')
    db.session.add(category)
    db.session.flush()
    product = Product(name='Test Perfume', slug='test-perfume', category_id=category.id)
    db.session.add(product)
    db.session.flush()
    variant = ProductVariant(product_id=product.id, size='50ml', price=price, stock_quantity=10)
    db.session.add(variant)
    db.session.flush()
    variant_id = variant.id
    db.session.commit()
    return variant_id


def add_admin_session(client, admin_id):
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_id)
        session['_fresh'] = True


def test_cart_add_returns_success_message_and_updated_count(client):
    with app.app_context():
        variant_id = create_product(149.0)

    response = client.post(
        '/cart/add',
        data={'variant_id': variant_id, 'quantity': 2},
        headers={'Accept': 'application/json'},
    )

    assert response.status_code == 200
    assert response.get_json() == {'message': 'Test Perfume added to cart!', 'cart_count': 2}


def test_shipping_settings_persist_and_checkout_adds_standard_charge(client):
    with app.app_context():
        admin = User(email='admin@example.test', password='test-password', role='admin')
        db.session.add(admin)
        db.session.flush()
        admin_id = admin.id
        variant_id = create_product(149.0)

    add_admin_session(client, admin_id)
    response = client.post(
        '/admin/settings',
        data={
            'shipping_charge': '79',
            'free_shipping_threshold': '999',
            'payment_cod_enabled': '1',
            'payment_online_enabled': '1',
        },
    )

    assert response.status_code == 200
    with app.app_context():
        assert Setting.query.filter_by(key='shipping_charge').one().value == '79.00'
        assert Setting.query.filter_by(key='free_shipping_threshold').one().value == '999.00'

    with client.session_transaction() as session:
        session['cart'] = {str(variant_id): 1}
    checkout = client.get('/checkout')
    checkout_body = checkout.get_data(as_text=True)

    assert checkout.status_code == 200
    assert '79.00' in checkout_body
    assert '228.00' in checkout_body


def test_checkout_shows_zero_shipping_and_subtotal_at_free_shipping_threshold(client):
    with app.app_context():
        variant_id = create_product(999.0)
        db.session.add_all([
            Setting(key='shipping_charge', value='79.00'),
            Setting(key='free_shipping_threshold', value='999.00'),
            Setting(key='payment_cod_enabled', value='1'),
            Setting(key='payment_online_enabled', value='1'),
        ])
        db.session.commit()

    with client.session_transaction() as session:
        session['cart'] = {str(variant_id): 1}
    checkout = client.get('/checkout')
    checkout_body = checkout.get_data(as_text=True)

    assert checkout.status_code == 200
    assert 'id="shippingAmount"' in checkout_body
    assert '&#8377;0.00' in checkout_body
    assert '999.00' in checkout_body


@pytest.mark.parametrize(
    ('subtotal', 'expected_shipping'),
    [(998.99, 79.0), (999.0, 0.0), (1200.0, 0.0)],
)
def test_shipping_becomes_free_at_or_above_threshold(subtotal, expected_shipping):
    settings = {'shipping_charge': '79.00', 'free_shipping_threshold': '999.00'}

    assert calculate_shipping(subtotal, settings) == expected_shipping


def create_webhook_signature(body):
    return hmac.new(
        app.config['RAZORPAY_WEBHOOK_SECRET'].encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()


def test_payment_captured_webhook_marks_matching_order_paid(client):
    with app.app_context():
        order = Order(total_amount=100.0, razorpay_order_id='order_webhook_test')
        db.session.add(order)
        db.session.commit()

    payload = {
        'event': 'payment.captured',
        'payload': {
            'payment': {
                'entity': {
                    'id': 'pay_webhook_test',
                    'order_id': 'order_webhook_test',
                    'status': 'captured',
                }
            }
        },
    }
    body = json.dumps(payload, separators=(',', ':'))

    response = client.post(
        '/payment/webhook',
        data=body,
        content_type='application/json',
        headers={'X-Razorpay-Signature': create_webhook_signature(body)},
    )

    assert response.status_code == 200
    with app.app_context():
        order = Order.query.filter_by(razorpay_order_id='order_webhook_test').one()
        assert order.payment_status == 'paid'
        assert order.status == 'processing'
        assert order.razorpay_payment_id == 'pay_webhook_test'


def test_order_paid_webhook_is_idempotent_for_coupon_usage(client):
    with app.app_context():
        coupon = Coupon(code='WEBHOOK', discount_type='fixed', discount_value=10.0)
        db.session.add(coupon)
        db.session.flush()
        order = Order(total_amount=100.0, razorpay_order_id='order_paid_test', coupon_id=coupon.id)
        db.session.add(order)
        db.session.commit()

    payload = {
        'event': 'order.paid',
        'payload': {
            'order': {'entity': {'id': 'order_paid_test'}},
            'payment': {'entity': {'id': 'pay_order_paid_test'}},
        },
    }
    body = json.dumps(payload, separators=(',', ':'))
    headers = {'X-Razorpay-Signature': create_webhook_signature(body)}

    assert client.post('/payment/webhook', data=body, content_type='application/json', headers=headers).status_code == 200
    assert client.post('/payment/webhook', data=body, content_type='application/json', headers=headers).status_code == 200

    with app.app_context():
        coupon = Coupon.query.filter_by(code='WEBHOOK').one()
        assert coupon.used_count == 1


def test_webhook_with_invalid_signature_is_rejected_without_changing_order(client):
    with app.app_context():
        order = Order(total_amount=100.0, razorpay_order_id='order_invalid_signature')
        db.session.add(order)
        db.session.commit()

    body = json.dumps({
        'event': 'payment.captured',
        'payload': {'payment': {'entity': {'id': 'pay_invalid_signature', 'order_id': 'order_invalid_signature'}}},
    })
    response = client.post(
        '/payment/webhook',
        data=body,
        content_type='application/json',
        headers={'X-Razorpay-Signature': 'invalid'},
    )

    assert response.status_code == 400
    with app.app_context():
        order = Order.query.filter_by(razorpay_order_id='order_invalid_signature').one()
        assert order.payment_status == 'unpaid'


def test_payment_verification_marks_only_the_matching_captured_order_paid(client, monkeypatch):
    class PaymentClient:
        def fetch(self, payment_id):
            return {
                'id': payment_id,
                'order_id': 'order_verification_test',
                'amount': 10000,
                'status': 'captured',
            }

    class RazorpayClient:
        payment = PaymentClient()

        class utility:
            @staticmethod
            def verify_payment_signature(parameters):
                return True

    monkeypatch.setattr('routes.main.get_razorpay_client', lambda: RazorpayClient())
    with app.app_context():
        order = Order(total_amount=100.0, razorpay_order_id='order_verification_test')
        db.session.add(order)
        db.session.commit()

    response = client.post(
        '/payment/verify',
        data={
            'razorpay_payment_id': 'pay_verification_test',
            'razorpay_order_id': 'order_verification_test',
            'razorpay_signature': 'valid-signature',
        },
    )

    assert response.status_code == 302
    with app.app_context():
        order = Order.query.filter_by(razorpay_order_id='order_verification_test').one()
        assert order.payment_status == 'paid'
        assert order.razorpay_payment_id == 'pay_verification_test'


def test_payment_verification_rejects_mismatched_amount(client, monkeypatch):
    class PaymentClient:
        def fetch(self, payment_id):
            return {
                'id': payment_id,
                'order_id': 'order_amount_test',
                'amount': 9900,
                'status': 'captured',
            }

    class RazorpayClient:
        payment = PaymentClient()

        class utility:
            @staticmethod
            def verify_payment_signature(parameters):
                return True

    monkeypatch.setattr('routes.main.get_razorpay_client', lambda: RazorpayClient())
    with app.app_context():
        order = Order(total_amount=100.0, razorpay_order_id='order_amount_test')
        db.session.add(order)
        db.session.commit()

    response = client.post(
        '/payment/verify',
        data={
            'razorpay_payment_id': 'pay_amount_test',
            'razorpay_order_id': 'order_amount_test',
            'razorpay_signature': 'valid-signature',
        },
    )

    assert response.status_code == 302
    with app.app_context():
        order = Order.query.filter_by(razorpay_order_id='order_amount_test').one()
        assert order.payment_status == 'unpaid'


def test_purge_legacy_razorpay_settings_command_removes_only_legacy_settings():
    with app.app_context():
        db.session.add_all([
            Setting(key='razorpay_key', value='obsolete-key'),
            Setting(key='razorpay_secret', value='obsolete-secret'),
            Setting(key='shipping_charge', value='79.00'),
        ])
        db.session.commit()

    result = app.test_cli_runner().invoke(args=['purge-legacy-razorpay-settings'])

    assert result.exit_code == 0
    with app.app_context():
        assert Setting.query.filter_by(key='razorpay_key').first() is None
        assert Setting.query.filter_by(key='razorpay_secret').first() is None
        assert Setting.query.filter_by(key='shipping_charge').one().value == '79.00'
