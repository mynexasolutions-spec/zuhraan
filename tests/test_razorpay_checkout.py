import json
import os
from types import SimpleNamespace

os.environ['APP_ENV'] = 'testing'
os.environ['SUPABASE_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['ITHINK_ENABLED'] = '0'
os.environ['RAZORPAY_ENABLED'] = '1'
os.environ['RAZORPAY_KEY_ID'] = 'rzp_test_key'
os.environ['RAZORPAY_KEY_SECRET'] = 'rzp_test_secret'
os.environ['RAZORPAY_WEBHOOK_SECRET'] = 'rzp_test_webhook_secret'

import pytest

from app import app
from models import Category, Coupon, Order, OrderItem, Product, ProductVariant, Setting, db
from routes.main import release_order_inventory


class FakeRazorpayClient:
    def __init__(self):
        self.created_orders = []
        self.payment_records = {}
        self.captured_payments = []
        self.payment_signature_error = None
        self.webhook_signature_error = None
        self.order = SimpleNamespace(create=self._create_order)
        self.payment = SimpleNamespace(fetch=self._fetch_payment, capture=self._capture_payment)
        self.utility = SimpleNamespace(
            verify_payment_signature=self._verify_payment_signature,
            verify_webhook_signature=self._verify_webhook_signature,
        )

    def _create_order(self, payload):
        self.created_orders.append(payload)
        return {'id': f'order_test_{len(self.created_orders)}'}

    def _fetch_payment(self, payment_id):
        return self.payment_records[payment_id]

    def _capture_payment(self, payment_id, amount):
        self.captured_payments.append((payment_id, amount))
        self.payment_records[payment_id]['status'] = 'captured'
        return {'status': 'captured'}

    def _verify_payment_signature(self, data):
        if self.payment_signature_error:
            raise self.payment_signature_error
        return True

    def _verify_webhook_signature(self, body, signature, secret):
        if self.webhook_signature_error:
            raise self.webhook_signature_error
        return True


CHECKOUT_FORM = {
    'name': 'Test Customer',
    'email': 'customer@example.test',
    'phone': '9999999999',
    'address_line1': '123 Test Street',
    'address_line2': 'Near Test Park',
    'city': 'Mumbai',
    'state': 'MH',
    'pincode': '400001',
    'country': 'India',
    'coupon_code': '',
}


@pytest.fixture(autouse=True)
def isolated_database():
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        RAZORPAY_ENABLED=True,
        RAZORPAY_KEY_ID='rzp_test_key',
        RAZORPAY_KEY_SECRET='rzp_test_secret',
        RAZORPAY_WEBHOOK_SECRET='rzp_test_webhook_secret',
        ITHINK_ENABLED=False,
    )
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client():
    return app.test_client()


@pytest.fixture
def razorpay_client(monkeypatch):
    fake = FakeRazorpayClient()
    monkeypatch.setattr('routes.main.get_razorpay_client', lambda: fake)
    return fake


def create_cart_product(price=149.0, stock_quantity=10):
    category = Category(name='Test Category', slug='test-category')
    db.session.add(category)
    db.session.flush()
    product = Product(name='Test Perfume', slug='test-perfume', category_id=category.id)
    db.session.add(product)
    db.session.flush()
    variant = ProductVariant(
        product_id=product.id,
        size='50ml',
        price=price,
        stock_quantity=stock_quantity,
    )
    db.session.add(variant)
    db.session.add_all([
        Setting(key='shipping_charge', value='0'),
        Setting(key='free_shipping_threshold', value='0'),
    ])
    db.session.commit()
    return variant.id


def place_checkout(client, variant_id, quantity=1, coupon_code=''):
    with client.session_transaction() as browser_session:
        browser_session['cart'] = {str(variant_id): quantity}
    form = dict(CHECKOUT_FORM)
    form['coupon_code'] = coupon_code
    return client.post('/razorpay/checkout', data=form)


def test_checkout_get_renders_summary(client):
    with app.app_context():
        variant_id = create_cart_product()

    with client.session_transaction() as browser_session:
        browser_session['cart'] = {str(variant_id): 2}

    response = client.get('/razorpay/checkout')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Secure Checkout' in body
    assert 'Test Perfume' in body
    assert 'Place Order' in body
    assert 'customer@example.test' not in body


def test_checkout_post_creates_local_and_razorpay_order(client, razorpay_client):
    with app.app_context():
        variant_id = create_cart_product(price=149.0)
        db.session.add(Coupon(
            code='SAVE10',
            discount_type='percent',
            discount_value=10.0,
            min_order_amount=0.0,
            used_count=0,
            is_active=True,
        ))
        db.session.commit()

    response = place_checkout(client, variant_id, quantity=2, coupon_code='save10')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Complete your payment' in body
    assert razorpay_client.created_orders[0]['amount'] == 26820
    assert razorpay_client.created_orders[0]['currency'] == 'INR'

    with app.app_context():
        order = Order.query.one()
        assert order.payment_method == 'razorpay'
        assert order.payment_status == 'unpaid'
        assert order.status == 'pending'
        assert order.razorpay_order_id == 'order_test_1'
        assert order.total_amount == pytest.approx(268.20)
        assert order.shipping_charges == pytest.approx(0.0)
        assert order.coupon_id is not None
        items = OrderItem.query.filter_by(order_id=order.id).all()
        assert len(items) == 1
        assert items[0].variant_id == variant_id
        assert items[0].quantity == 2
        assert items[0].price_at_time == pytest.approx(149.0)
        variant = db.session.get(ProductVariant, variant_id)
        assert variant.stock_quantity == 10

    with client.session_transaction() as browser_session:
        assert browser_session['cart'] == {str(variant_id): 2}


def test_checkout_post_rejects_invalid_coupon_without_creating_order(client, razorpay_client):
    with app.app_context():
        variant_id = create_cart_product()

    response = place_checkout(client, variant_id, coupon_code='DOESNOTEXIST')

    assert response.status_code == 200
    assert 'Invalid or inactive coupon code.' in response.get_data(as_text=True)
    assert razorpay_client.created_orders == []
    with app.app_context():
        assert Order.query.count() == 0


def test_verify_payment_marks_order_paid_and_updates_stock(client, razorpay_client):
    with app.app_context():
        variant_id = create_cart_product(stock_quantity=10)
        db.session.add(Coupon(
            code='SAVE20',
            discount_type='fixed',
            discount_value=20.0,
            min_order_amount=0.0,
            used_count=0,
            is_active=True,
        ))
        db.session.commit()

    assert place_checkout(client, variant_id, quantity=2, coupon_code='SAVE20').status_code == 200

    with app.app_context():
        order = Order.query.one()
        razorpay_order_id = order.razorpay_order_id
        local_order_id = order.id
    assert razorpay_client.created_orders[0]['amount'] == 27800

    razorpay_client.payment_records['pay_test_1'] = {
        'id': 'pay_test_1',
        'order_id': razorpay_order_id,
        'status': 'captured',
        'amount': 27800,
        'currency': 'INR',
    }

    response = client.post('/payment/verify', data={
        'razorpay_payment_id': 'pay_test_1',
        'razorpay_order_id': razorpay_order_id,
        'razorpay_signature': 'valid-signature',
    })

    assert response.status_code == 302
    assert response.headers['Location'] == '/'

    with app.app_context():
        order = db.session.get(Order, local_order_id)
        assert order.payment_status == 'paid'
        assert order.status == 'processing'
        assert order.razorpay_payment_id == 'pay_test_1'
        variant = db.session.get(ProductVariant, variant_id)
        assert variant.stock_quantity == 8
        coupon = Coupon.query.filter_by(code='SAVE20').one()
        assert coupon.used_count == 1

    with client.session_transaction() as browser_session:
        assert browser_session['cart'] == {}
        assert browser_session['guest_order_ids'] == [local_order_id]


def test_verify_payment_rejects_amount_mismatch(client, razorpay_client):
    with app.app_context():
        variant_id = create_cart_product(stock_quantity=10)

    assert place_checkout(client, variant_id, quantity=2).status_code == 200

    with app.app_context():
        order = Order.query.one()
        razorpay_order_id = order.razorpay_order_id
        local_order_id = order.id

    razorpay_client.payment_records['pay_test_wrong'] = {
        'id': 'pay_test_wrong',
        'order_id': razorpay_order_id,
        'status': 'captured',
        'amount': 100,
        'currency': 'INR',
    }

    response = client.post('/payment/verify', data={
        'razorpay_payment_id': 'pay_test_wrong',
        'razorpay_order_id': razorpay_order_id,
        'razorpay_signature': 'valid-signature',
    })

    assert response.status_code == 302
    with app.app_context():
        order = db.session.get(Order, local_order_id)
        assert order.payment_status == 'unpaid'
        variant = db.session.get(ProductVariant, variant_id)
        assert variant.stock_quantity == 10


def test_cancelling_unpaid_razorpay_order_does_not_restore_undeducted_stock():
    with app.app_context():
        variant_id = create_cart_product(stock_quantity=10)
        order = Order(
            customer_name='Test Customer',
            customer_email='customer@example.test',
            total_amount=149.0,
            status='cancelled',
            payment_status='unpaid',
            payment_method='razorpay',
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(
            order_id=order.id,
            variant_id=variant_id,
            quantity=1,
            price_at_time=149.0,
        ))
        db.session.commit()

        release_order_inventory(order)
        db.session.commit()

        variant = db.session.get(ProductVariant, variant_id)
        assert variant.stock_quantity == 10


def test_webhook_marks_order_paid_idempotently(client, razorpay_client):
    with app.app_context():
        variant_id = create_cart_product(stock_quantity=10)

    assert place_checkout(client, variant_id, quantity=2).status_code == 200

    with app.app_context():
        order = Order.query.one()
        razorpay_order_id = order.razorpay_order_id
        local_order_id = order.id

    payload = {
        'event': 'payment.captured',
        'payload': {
            'payment': {
                'entity': {
                    'id': 'pay_webhook_1',
                    'order_id': razorpay_order_id,
                    'amount': 29800,
                    'currency': 'INR',
                },
            },
        },
    }

    first = client.post(
        '/payment/webhook',
        data=json.dumps(payload),
        headers={'X-Razorpay-Signature': 'valid-signature', 'Content-Type': 'application/json'},
    )
    second = client.post(
        '/payment/webhook',
        data=json.dumps(payload),
        headers={'X-Razorpay-Signature': 'valid-signature', 'Content-Type': 'application/json'},
    )

    assert first.status_code == 200
    assert first.get_json()['status'] == 'ok'
    assert second.status_code == 200
    assert second.get_json()['status'] == 'ok'

    with app.app_context():
        order = db.session.get(Order, local_order_id)
        assert order.payment_status == 'paid'
        assert order.razorpay_payment_id == 'pay_webhook_1'
        variant = db.session.get(ProductVariant, variant_id)
        assert variant.stock_quantity == 8
