import os

os.environ['APP_ENV'] = 'testing'
os.environ['SUPABASE_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['SHIPROCKET_ENABLED'] = '0'
os.environ['GOKWIK_ENABLED'] = '1'
os.environ['GOKWIK_STOREFRONT_ENABLED'] = '1'
os.environ['GOKWIK_ENV'] = 'sandbox'
os.environ['GOKWIK_MERCHANT_ID'] = 'test-merchant'
os.environ['GOKWIK_APP_ID'] = 'test-app-id'
os.environ['GOKWIK_APP_SECRET'] = 'test-app-secret'

import pytest

from app import app, get_database_engine_options, get_supabase_database_url
from models import Category, Coupon, GoKwikCheckoutSession, Order, Product, ProductVariant, Setting, db


@pytest.fixture(autouse=True)
def isolated_database():
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        GOKWIK_ENABLED=True,
        GOKWIK_STOREFRONT_ENABLED=True,
        GOKWIK_ENV='sandbox',
        GOKWIK_MERCHANT_ID='test-merchant',
        GOKWIK_APP_ID='test-app-id',
        GOKWIK_APP_SECRET='test-app-secret',
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


def test_prepaid_order_rejects_tampered_total_and_missing_transaction(client):
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

    with pytest.raises(RuntimeError, match='require SSL'):
        get_supabase_database_url()

    secure_url = (
        'postgresql://user:password@pooler.supabase.com:5432/postgres?sslmode=require'
    )
    monkeypatch.setenv('SUPABASE_DATABASE_URL', secure_url)

    assert get_supabase_database_url() == secure_url
    assert get_database_engine_options() == {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
