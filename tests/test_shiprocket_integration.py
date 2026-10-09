import os

os.environ['APP_ENV'] = 'testing'
os.environ['SUPABASE_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['SHIPROCKET_ENABLED'] = '0'

import pytest

from app import app
from models import Category, Order, OrderItem, Product, ProductVariant, Setting, User, db


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


def create_fulfillable_order() -> tuple[int, int]:
    admin = User(email='admin@example.test', password='test-password', role='admin')
    category = Category(name='Test Category', slug='test-category')
    db.session.add_all([admin, category])
    db.session.flush()
    product = Product(name='Test Perfume', slug='test-perfume', category_id=category.id)
    db.session.add(product)
    db.session.flush()
    variant = ProductVariant(product_id=product.id, size='50ml', price=499.0, stock_quantity=9)
    db.session.add(variant)
    db.session.flush()
    order = Order(
        customer_name='Test Customer',
        customer_email='customer@example.test',
        customer_phone='9999999999',
        address_line1='123 Test Street',
        city='Mumbai',
        state='Maharashtra',
        pincode='400001',
        country='India',
        total_amount=578.0,
        shipping_charges=79.0,
        payment_method='native_cod',
        payment_status='unpaid',
        status='pending',
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(
        order_id=order.id,
        variant_id=variant.id,
        quantity=1,
        price_at_time=499.0,
    ))
    db.session.commit()
    return admin.id, order.id


def add_admin_session(client, admin_id: int) -> None:
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_id)
        session['_fresh'] = True


def test_authenticated_webhook_updates_tracking_and_order_status(client, monkeypatch):
    monkeypatch.setitem(app.config, 'SHIPROCKET_ENABLED', True)
    monkeypatch.setitem(app.config, 'SHIPROCKET_WEBHOOK_TOKEN', 'webhook-test-token')
    with app.app_context():
        _, order_id = create_fulfillable_order()
        order = db.session.get(Order, order_id)
        order.shiprocket_order_id = '12345'
        order.shiprocket_shipment_id = '67890'
        order.awb_number = 'AWB123'
        order.status = 'processing'
        db.session.commit()

    payload = {
        'awb': 'AWB123',
        'sr_order_id': 12345,
        'courier_name': 'Test Courier',
        'current_status': 'IN TRANSIT',
        'current_status_id': 20,
    }
    unauthorized = client.post('/api/fulfillment/webhook', json=payload)
    accepted = client.post(
        '/api/fulfillment/webhook',
        json=payload,
        headers={'x-api-key': 'webhook-test-token'},
    )

    assert unauthorized.status_code == 401
    assert accepted.status_code == 200
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.status == 'shipped'
        assert order.shiprocket_status == 'IN TRANSIT'
        assert order.shiprocket_status_id == 20
        assert order.shipping_provider == 'Test Courier'


def test_admin_can_create_awb_and_schedule_pickup(client, monkeypatch):
    class FakeShiprocketClient:
        def create_order(self, payload):
            assert payload['pickup_location'] == 'Primary Warehouse'
            assert payload['payment_method'] == 'COD'
            assert payload['sub_total'] == 499.0
            return {'order_id': 12345, 'shipment_id': 67890, 'status': 'NEW'}

        def assign_awb(self, shipment_id, courier_id):
            assert shipment_id == '67890'
            assert courier_id is None
            return {
                'response': {
                    'data': {
                        'awb_code': 'AWB123',
                        'courier_name': 'Test Courier',
                    }
                }
            }

        def schedule_pickup(self, shipment_id):
            assert shipment_id == '67890'
            return {'pickup_status': 1}

    monkeypatch.setitem(app.config, 'SHIPROCKET_ENABLED', True)
    monkeypatch.setitem(app.config, 'SHIPROCKET_PICKUP_LOCATION', 'Primary Warehouse')
    monkeypatch.setattr('routes.admin.ShiprocketClient', FakeShiprocketClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
        db.session.add(Setting(key='shiprocket_auto_pickup', value='1'))
        db.session.commit()
    add_admin_session(client, admin_id)

    response = client.post(
        f'/admin/orders/{order_id}/shiprocket/create',
        data={'weight': '0.5', 'length': '10', 'breadth': '10', 'height': '10'},
    )

    assert response.status_code == 302
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.shiprocket_order_id == '12345'
        assert order.shiprocket_shipment_id == '67890'
        assert order.awb_number == 'AWB123'
        assert order.shipping_provider == 'Test Courier'
        assert order.shiprocket_pickup_scheduled is True
        assert order.shiprocket_status == 'PICKUP SCHEDULED'
        assert order.status == 'processing'
