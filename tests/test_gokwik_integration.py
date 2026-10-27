import os
from types import SimpleNamespace

os.environ['APP_ENV'] = 'testing'
os.environ['SUPABASE_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['ITHINK_ENABLED'] = '0'
os.environ['GOKWIK_ENABLED'] = '1'
os.environ['GOKWIK_ENV'] = 'sandbox'
os.environ['GOKWIK_MERCHANT_ID'] = 'test-merchant'
os.environ['GOKWIK_APP_ID'] = 'test-app-id'
os.environ['GOKWIK_APP_SECRET'] = 'test-app-secret'

import pytest
from werkzeug.security import generate_password_hash

from app import app, get_database_engine_options, get_supabase_database_url
from gokwik_client import verify_gokwik_merchant, GoKwikRequestError
from models import Category, Coupon, GoKwikCheckoutSession, Order, Product, ProductVariant, Setting, User, db


@pytest.fixture(autouse=True, params=['sandbox', 'production'])
def isolated_database(request):
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        GOKWIK_ENABLED=True,
        GOKWIK_ENV=request.param,
        GOKWIK_PRODUCTION_APPROVED=True,
        GOKWIK_SDK_URL=(
            'https://sandbox.pdp.gokwik.co/v4/build/gokwik.js' if request.param == 'sandbox'
            else 'https://pdp.gokwik.co/v4/build/gokwik.js'
        ),
        GOKWIK_API_BASE_URL=(
            'https://api-gw-v4.dev.gokwik.io/sandbox/' if request.param == 'sandbox'
            else 'https://gkx.gokwik.co/'
        ),
        GOKWIK_MERCHANT_ID='test-merchant',
        GOKWIK_APP_ID='test-app-id',
        GOKWIK_APP_SECRET='test-app-secret',
        RAZORPAY_ENABLED=True,
    )
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client():
    return app.test_client()


def create_cart_product() -> int:
    category = Category(name='Test Category', slug='test-category')
    db.session.add(category)
    db.session.flush()
    product = Product(name='Test Perfume', slug='test-perfume', category_id=category.id)
    db.session.add(product)
    db.session.flush()
    variant = ProductVariant(product_id=product.id, size='50ml', price=149.0, stock_quantity=10)
    db.session.add(variant)
    db.session.add_all([
        Setting(key='shipping_charge', value='0'),
        Setting(key='free_shipping_threshold', value='0'),
    ])
    db.session.commit()
    return variant.id


def gokwik_headers() -> dict[str, str]:
    return {'appid': 'test-app-id', 'appsecret': 'test-app-secret'}


def checkout_address() -> dict[str, str]:
    return {
        'first_name': 'Test',
        'last_name': 'Customer',
        'email': 'customer@example.test',
        'phone': '9999999999',
        'address_1': '123 Test Street',
        'city': 'Mumbai',
        'state': 'MH',
        'postcode': '400001',
        'country': 'IN',
    }


def create_checkout(client, variant_id: int, quantity: int) -> str:
    with client.session_transaction() as browser_session:
        browser_session['cart'] = {str(variant_id): quantity}
    response = client.post('/api/gokwik/checkout-session')
    assert response.status_code == 201
    return response.get_json()['checkout_id']


def test_health_check_requires_valid_gokwik_credentials(client):
    unauthorized = client.get('/api/gokwik/v1/cart/health-check')
    authorized = client.get('/api/gokwik/v1/cart/health-check', headers=gokwik_headers())

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.get_json()['status'] == 'success'


def test_production_gate_preserves_cart_without_creating_checkout(client, monkeypatch):
    monkeypatch.setitem(app.config, 'GOKWIK_ENV', 'production')
    monkeypatch.setitem(app.config, 'GOKWIK_PRODUCTION_APPROVED', False)
    with app.app_context():
        variant_id = create_cart_product()
    with client.session_transaction() as browser_session:
        browser_session['cart'] = {str(variant_id): 1}
    response = client.post('/api/gokwik/checkout-session')
    assert response.status_code == 503
    assert response.get_json()['code'] == 'gc_production_not_approved'
    with app.app_context():
        assert GoKwikCheckoutSession.query.count() == 0
        assert Order.query.count() == 0
    with client.session_transaction() as browser_session:
        assert browser_session['cart'] == {str(variant_id): 1}


def test_remote_merchant_mismatch_is_actionable_and_does_not_expose_secrets(monkeypatch):
    def merchant_response(url, headers, timeout):
        assert url == app.config['GOKWIK_API_BASE_URL'] + 'v3/checkout/guest/get-merchant-config'
        assert headers == {'gk-merchant-id': 'test-merchant'}
        return SimpleNamespace(status_code=500, text='Merchant not found')

    monkeypatch.setattr('gokwik_client.requests.get', merchant_response)
    with app.app_context(), pytest.raises(GoKwikRequestError, match='Merchant not found') as error:
        verify_gokwik_merchant()
    assert 'test-app-secret' not in str(error.value)
    assert app.config['GOKWIK_ENV'] in str(error.value)


def test_cod_order_creation_is_idempotent_and_uses_authoritative_cart(client):
    with app.app_context():
        variant_id = create_cart_product()

    checkout_id = create_checkout(client, variant_id, 2)

    cart_response = client.post(
        '/api/gokwik/v1/cart',
        json={'session_key': checkout_id},
        headers=gokwik_headers(),
    )
    assert cart_response.status_code == 200
    assert cart_response.get_json()['totals']['total'] == 298.0

    address = checkout_address()
    payload = {
        'session_key': checkout_id,
        'payment_method': 'cod',
        'order_total': 298.0,
        'billing': address,
        'shipping': address,
        'fee_lines': [],
    }
    first_response = client.post(
        '/api/gokwik/v1/cart/place-order',
        json=payload,
        headers=gokwik_headers(),
    )
    retry_response = client.post(
        '/api/gokwik/v1/cart/place-order',
        json=payload,
        headers=gokwik_headers(),
    )

    assert first_response.status_code == 200
    assert retry_response.status_code == 200
    assert retry_response.get_json()['id'] == first_response.get_json()['id']
    with app.app_context():
        assert Order.query.count() == 1
        assert db.session.get(ProductVariant, variant_id).stock_quantity == 8
        assert db.session.get(GoKwikCheckoutSession, checkout_id).status == 'completed'


def test_guest_gokwik_order_appears_after_same_browser_login(client):
    with app.app_context():
        variant_id = create_cart_product()
        customer = User(
            email='account-owner@example.test',
            password=generate_password_hash('test-password', method='pbkdf2:sha256'),
            role='user',
        )
        db.session.add(customer)
        db.session.commit()
    checkout_id = create_checkout(client, variant_id, 1)
    address = checkout_address()
    placed = client.post(
        '/api/gokwik/v1/cart/place-order',
        json={
            'session_key': checkout_id,
            'payment_method': 'cod',
            'order_total': 149.0,
            'billing': address,
            'shipping': address,
            'fee_lines': [],
        },
        headers=gokwik_headers(),
    )
    order_id = placed.get_json()['id']

    completed = client.post(
        '/api/gokwik/checkout-complete',
        json={'checkout_id': checkout_id, 'merchant_order_id': str(order_id)},
    )
    account_response = client.post(
        '/account/login',
        data={
            'email': 'account-owner@example.test',
            'password': 'test-password',
        },
        follow_redirects=True,
    )

    assert placed.status_code == 200
    assert completed.status_code == 200
    assert account_response.status_code == 200
    assert f'Order #{order_id}'.encode() in account_response.data
    assert b'Test Perfume' in account_response.data
    with app.app_context():
        order = db.session.get(Order, order_id)
        customer = User.query.filter_by(email='account-owner@example.test').one()
        assert order.user_id == customer.id


def test_cod_order_is_automatically_created_in_ithink(client, monkeypatch):
    class FakeIThinkClient:
        def create_order(self, payload):
            assert payload['shipments'][0]['payment_mode'] == 'COD'
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    '1': {
                        'status': 'Success',
                        'waybill': 'AWB123',
                        'refnum': 'ZUHRAAN-1',
                        'logistic_name': 'Xpressbees',
                    },
                },
            }

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setitem(app.config, 'ITHINK_ACCESS_TOKEN', 'test-access-token')
    monkeypatch.setitem(app.config, 'ITHINK_SECRET_KEY', 'test-secret-key')
    monkeypatch.setitem(app.config, 'ITHINK_PICKUP_ADDRESS_ID', '127196')
    monkeypatch.setitem(app.config, 'ITHINK_RETURN_ADDRESS_ID', '127196')
    monkeypatch.setattr('ithink_client.IThinkClient', FakeIThinkClient)
    with app.app_context():
        variant_id = create_cart_product()
    checkout_id = create_checkout(client, variant_id, 1)
    address = checkout_address()

    response = client.post(
        '/api/gokwik/v1/cart/place-order',
        json={
            'session_key': checkout_id,
            'payment_method': 'cod',
            'order_total': 149.0,
            'billing': address,
            'shipping': address,
            'fee_lines': [],
        },
        headers=gokwik_headers(),
    )

    assert response.status_code == 200
    with app.app_context():
        order = db.session.get(Order, response.get_json()['id'])
        assert order.shipping_order_reference == 'ZUHRAAN-1'
        assert order.awb_number == 'AWB123'
        assert order.shipping_provider == 'Xpressbees'
        assert order.shipping_status == 'Success'


def test_prepaid_order_requires_confirmed_payment_and_matching_total(client):
    with app.app_context():
        variant_id = create_cart_product()
    checkout_id = create_checkout(client, variant_id, 1)
    address = checkout_address()
    payload = {
        'session_key': checkout_id,
        'payment_method': 'gokwik_prepaid',
        'order_total': 1.0,
        'billing': address,
        'shipping': address,
        'fee_lines': [],
        'set_paid': True,
    }

    tampered = client.post(
        '/api/gokwik/v1/cart/place-order',
        json=payload,
        headers=gokwik_headers(),
    )
    assert tampered.status_code == 400
    assert tampered.get_json()['code'] == 'gc_cart_total_mismatch'

    payload['order_total'] = 149.0
    payload['set_paid'] = False
    unconfirmed = client.post(
        '/api/gokwik/v1/cart/place-order',
        json=payload,
        headers=gokwik_headers(),
    )
    assert unconfirmed.status_code == 402
    assert unconfirmed.get_json()['code'] == 'gc_payment_not_confirmed'

    payload['set_paid'] = True
    missing_transaction = client.post(
        '/api/gokwik/v1/cart/place-order',
        json=payload,
        headers=gokwik_headers(),
    )
    assert missing_transaction.status_code == 400
    assert missing_transaction.get_json()['code'] == 'gc_missing_transaction_id'

    payload['transaction_id'] = 'gokwik_test_transaction'
    placed = client.post(
        '/api/gokwik/v1/cart/place-order',
        json=payload,
        headers=gokwik_headers(),
    )
    assert placed.status_code == 200
    with app.app_context():
        order = db.session.get(Order, placed.get_json()['id'])
        assert order.payment_status == 'paid'
        assert order.gokwik_transaction_id == 'gokwik_test_transaction'


def test_failed_checkout_keeps_cart_and_creates_no_order(client):
    with app.app_context():
        variant_id = create_cart_product()
    checkout_id = create_checkout(client, variant_id, 1)

    failed = client.post(
        '/api/gokwik/checkout-failed',
        json={'checkout_id': checkout_id, 'reason': 'payment_failed'},
    )
    assert failed.status_code == 200
    with client.session_transaction() as browser_session:
        assert browser_session['cart'] == {str(variant_id): 1}
    with app.app_context():
        checkout = db.session.get(GoKwikCheckoutSession, checkout_id)
        assert checkout.status == 'failed'
        assert checkout.failure_reason == 'payment_failed'
        assert Order.query.count() == 0

    unconfirmed = client.post('/api/gokwik/checkout-complete', json={'checkout_id': checkout_id})
    assert unconfirmed.status_code == 409

    # A retry inside the SDK or a delayed authenticated callback can still succeed.
    attempted_order = client.post(
        '/api/gokwik/v1/cart/place-order',
        json={
            'session_key': checkout_id,
            'payment_method': 'gokwik_prepaid',
            'order_total': 149.0,
            'billing': checkout_address(),
            'shipping': checkout_address(),
            'fee_lines': [],
            'set_paid': True,
            'transaction_id': 'failed-checkout-transaction',
        },
        headers=gokwik_headers(),
    )

    assert attempted_order.status_code == 200
    completed = client.post('/api/gokwik/checkout-complete', json={'checkout_id': checkout_id})
    assert completed.status_code == 200
    late_failure = client.post('/api/gokwik/checkout-failed', json={'checkout_id': checkout_id})
    assert late_failure.get_json()['status'] == 'completed'
    with app.app_context():
        checkout = db.session.get(GoKwikCheckoutSession, checkout_id)
        assert checkout.status == 'completed'
        assert checkout.failure_reason is None
        assert Order.query.count() == 1


def test_gokwik_transaction_cannot_be_reused(client):
    with app.app_context():
        variant_id = create_cart_product()
    first_checkout_id = create_checkout(client, variant_id, 1)
    second_checkout_id = create_checkout(client, variant_id, 1)
    address = checkout_address()

    def place_paid_order(checkout_id: str):
        return client.post(
            '/api/gokwik/v1/cart/place-order',
            json={
                'session_key': checkout_id,
                'payment_method': 'gokwik_prepaid',
                'order_total': 149.0,
                'billing': address,
                'shipping': address,
                'fee_lines': [],
                'set_paid': True,
                'transaction_id': 'unique-gokwik-transaction',
            },
            headers=gokwik_headers(),
        )

    first = place_paid_order(first_checkout_id)
    duplicate = place_paid_order(second_checkout_id)

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.get_json()['code'] == 'gc_duplicate_transaction'
    with app.app_context():
        assert Order.query.count() == 1


def test_cart_uses_only_razorpay_checkout(client):
    with app.app_context():
        variant_id = create_cart_product()
    with client.session_transaction() as browser_session:
        browser_session['cart'] = {str(variant_id): 1}

    cart_response = client.get('/cart')
    legacy_get = client.get('/checkout')
    legacy_post = client.post('/checkout')
    cart_body = cart_response.get_data(as_text=True)

    assert cart_response.status_code == 200
    assert 'Checkout securely with Razorpay' not in cart_body
    assert 'Checkout' in cart_body
    assert 'Checkout securely with GoKwik' not in cart_body
    assert app.config['GOKWIK_SDK_URL'] not in cart_body
    assert legacy_get.status_code == 302
    assert legacy_get.headers['Location'].endswith('/cart')
    assert legacy_post.status_code == 405


def test_coupon_address_shipping_and_wallet_contract(client):
    with app.app_context():
        variant_id = create_cart_product()
        db.session.add(Coupon(
            code='SAVE10',
            discount_type='fixed',
            discount_value=10.0,
            min_order_amount=0.0,
            used_count=0,
            is_active=True,
        ))
        db.session.commit()
    checkout_id = create_checkout(client, variant_id, 1)

    address_response = client.post(
        '/api/gokwik/v1/cart/set-address',
        json={
            'session_key': checkout_id,
            'first_name': 'Test',
            'email': 'customer@example.test',
            'phone': '9999999999',
            'address_1': '123 Test Street',
            'city': 'Mumbai',
            'state': 'MH',
            'postcode': '400001',
            'country': 'IN',
        },
        headers=gokwik_headers(),
    )
    coupon_response = client.post(
        '/api/gokwik/v1/cart/apply-coupon',
        json={'session_key': checkout_id, 'coupon': 'SAVE10'},
        headers=gokwik_headers(),
    )
    shipping_response = client.post(
        '/api/gokwik/v1/cart/set-shipping-method',
        json={'session_key': checkout_id, 'shipping_methods': ['standard']},
        headers=gokwik_headers(),
    )
    wallet_response = client.post(
        '/api/gokwik/v1/cart/get-wallet-balance',
        json={'customer_email': 'customer@example.test'},
        headers=gokwik_headers(),
    )
    wallet_deduction = client.post(
        '/api/gokwik/v1/cart/deduct-wallet-balance',
        json={'email': 'customer@example.test', 'amount': 10},
        headers=gokwik_headers(),
    )

    assert address_response.status_code == 200
    assert coupon_response.status_code == 200
    assert coupon_response.get_json()['cart']['totals']['total'] == 139.0
    assert shipping_response.status_code == 200
    assert wallet_response.status_code == 200
    assert wallet_response.get_json()['wallet_balance'] == '0.00'
    assert wallet_deduction.status_code == 400
    assert wallet_deduction.get_json()['code'] == 'gc_wallet_not_configured'


def test_readiness_command_does_not_print_credentials():
    with app.app_context():
        db.create_all()
    result = app.test_cli_runner().invoke(args=['gokwik-readiness'])

    assert result.exit_code == 0
    assert '[PASS] authenticated_health_route' in result.output
    assert 'test-app-secret' not in result.output


def test_production_database_requires_ssl(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv(
        'SUPABASE_DATABASE_URL',
        'postgresql://user:password@pooler.supabase.com:5432/postgres',
    )

    assert get_supabase_database_url().endswith('?sslmode=require')

    secure_url = (
        'postgresql://user:password@pooler.supabase.com:5432/postgres?sslmode=require'
    )
    monkeypatch.setenv('SUPABASE_DATABASE_URL', secure_url)

    assert get_supabase_database_url() == secure_url
    assert get_database_engine_options() == {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
