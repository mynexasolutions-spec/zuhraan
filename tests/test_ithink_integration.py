import os

import pytest

from app import app
from ithink_client import (
    IThinkRequestError,
    extract_created_shipment,
    normalize_indian_pincode,
)
from models import Category, Order, OrderItem, Product, ProductVariant, User, db


os.environ['ITHINK_ENABLED'] = '0'


@pytest.fixture(autouse=True)
def isolated_database():
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        ITHINK_ENABLED=False,
        ITHINK_ACCESS_TOKEN='test-access-token',
        ITHINK_SECRET_KEY='test-secret-key',
        ITHINK_PICKUP_ADDRESS_ID='127196',
        ITHINK_RETURN_ADDRESS_ID='127196',
        ITHINK_DEFAULT_WEIGHT_KG='0.5',
        ITHINK_DEFAULT_LENGTH_CM='10',
        ITHINK_DEFAULT_WIDTH_CM='10',
        ITHINK_DEFAULT_HEIGHT_CM='10',
    )
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
        payment_method='cod',
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


@pytest.mark.parametrize('value', ['400001.0', '400001.00'])
def test_normalize_indian_pincode_accepts_legacy_integer_format(value):
    assert normalize_indian_pincode(value) == '400001'


def test_normalize_indian_pincode_rejects_non_six_digit_value():
    with pytest.raises(ValueError, match='6-digit Indian pincode'):
        normalize_indian_pincode('40000')


def test_admin_can_create_ithink_shipment(client, monkeypatch):
    class FakeIThinkClient:
        def create_order(self, payload):
            assert payload['pickup_address_id'] == '127196'
            assert payload['shipments'][0]['payment_mode'] == 'COD'
            assert payload['shipments'][0]['waybill'] == ''
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    'shipments': [{
                        'status': 'Success',
                        'waybill': 'AWB123',
                        'refnum': 'ZUHRAAN-1',
                        'logistic_name': 'Xpressbees',
                        'tracking_url': 'https://tracking.example.test/AWB123',
                    }],
                },
            }

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setattr('routes.admin.IThinkClient', FakeIThinkClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
    add_admin_session(client, admin_id)

    response = client.post(
        f'/admin/orders/{order_id}/ithink/create',
        data={'weight': '0.5', 'length': '10', 'width': '10', 'height': '10'},
    )

    assert response.status_code == 302
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.shipping_order_reference == 'ZUHRAAN-1'
        assert order.awb_number == 'AWB123'
        assert order.shipping_provider == 'Xpressbees'
        assert order.shipping_tracking_url == 'https://tracking.example.test/AWB123'
        assert order.shipping_status == 'Success'
        assert order.status == 'processing'


def test_extract_created_shipment_prefers_record_with_awb():
    shipment = extract_created_shipment(
        {
            'status': 'success',
            'status_code': 200,
            'data': {
                'request': {
                    'order': 'ZUHRAAN-7',
                    'waybill': '',
                    'status': 'Success',
                },
                '1': {
                    'status': 'Success',
                    'remark': 'Order Booked Successfully',
                    'awb_no': 'AWB777',
                    'order_id': 'ZUHRAAN-7',
                    'courier_name': 'Delhivery',
                },
            },
        },
        'ZUHRAAN-7',
    )

    assert shipment['order_reference'] == 'ZUHRAAN-7'
    assert shipment['awb_number'] == 'AWB777'
    assert shipment['courier_name'] == 'Delhivery'
    assert shipment['status'] == 'Success'
    assert shipment['remark'] == 'Order Booked Successfully'


def test_admin_recovers_awb_from_partial_create_response(client, monkeypatch):
    class FakeIThinkClient:
        def create_order(self, payload):
            order_reference = payload['shipments'][0]['order']
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    '1': {
                        'status': 'Success',
                        'remark': 'AWB pending',
                        'waybill': '',
                        'refnum': order_reference,
                    },
                },
            }

        def get_order_details(self, *, order_reference, start_date, end_date):
            assert order_reference.startswith('ZUHRAAN-')
            assert start_date <= end_date
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    '99': {
                        'awb_no': 'AWB999',
                        'order': order_reference,
                        'logistic': 'Delhivery',
                        'latest_courier_status': 'Manifested',
                    },
                },
            }

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setattr('routes.admin.IThinkClient', FakeIThinkClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
    add_admin_session(client, admin_id)

    response = client.post(
        f'/admin/orders/{order_id}/ithink/create',
        data={'weight': '0.5', 'length': '10', 'width': '10', 'height': '10'},
    )

    assert response.status_code == 302
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.shipping_order_reference == f'ZUHRAAN-{order_id}'
        assert order.awb_number == 'AWB999'
        assert order.shipping_provider == 'Delhivery'
        assert order.shipping_status == 'Manifested'
        assert order.status == 'processing'


def test_admin_recovers_existing_ithink_order_after_duplicate_error(client, monkeypatch):
    class FakeIThinkClient:
        def create_order(self, payload):
            raise IThinkRequestError('Order already exists for this reference.')

        def get_order_details(self, *, order_reference, start_date, end_date):
            assert order_reference.startswith('ZUHRAAN-')
            assert start_date <= end_date
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    '100': {
                        'awb_no': 'AWB1000',
                        'order': order_reference,
                        'logistic': 'Xpressbees',
                        'latest_courier_status': 'Picked Up',
                    },
                },
            }

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setattr('routes.admin.IThinkClient', FakeIThinkClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
    add_admin_session(client, admin_id)

    response = client.post(
        f'/admin/orders/{order_id}/ithink/create',
        data={'weight': '0.5', 'length': '10', 'width': '10', 'height': '10'},
    )

    assert response.status_code == 302
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.shipping_order_reference == f'ZUHRAAN-{order_id}'
        assert order.awb_number == 'AWB1000'
        assert order.shipping_provider == 'Xpressbees'
        assert order.shipping_status == 'Picked Up'
        assert order.status == 'processing'


def test_admin_preserves_reference_and_reason_when_awb_is_missing(client, monkeypatch):
    class FakeIThinkClient:
        def create_order(self, payload):
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    '1': {
                        'status': 'Failed',
                        'remark': 'Pickup pincode is not serviceable',
                        'waybill': '',
                        'refnum': payload['shipments'][0]['order'],
                    },
                },
            }

        def get_order_details(self, *, order_reference, start_date, end_date):
            raise IThinkRequestError('No order details found.')

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setattr('routes.admin.IThinkClient', FakeIThinkClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
    add_admin_session(client, admin_id)

    response = client.post(
        f'/admin/orders/{order_id}/ithink/create',
        data={'weight': '0.5', 'length': '10', 'width': '10', 'height': '10'},
    )

    assert response.status_code == 302
    with client.session_transaction() as session:
        flashes = session.get('_flashes', [])
    assert any(
        'Pickup pincode is not serviceable' in message
        for _, message in flashes
    )
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.shipping_order_reference == f'ZUHRAAN-{order_id}'
        assert order.awb_number is None
        assert order.shipping_status == 'Failed'
        assert order.status == 'pending'


def test_admin_retries_creation_after_failed_partial_shipment(client, monkeypatch):
    create_calls = []

    class FakeIThinkClient:
        def create_order(self, payload):
            create_calls.append(payload['shipments'][0]['order'])
            return {
                'status': 'success',
                'status_code': 200,
                'data': {
                    '1': {
                        'status': 'Success',
                        'waybill': 'AWB-RETRY',
                        'refnum': payload['shipments'][0]['order'],
                        'logistic_name': 'Delhivery',
                    },
                },
            }

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setattr('routes.admin.IThinkClient', FakeIThinkClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
        order = db.session.get(Order, order_id)
        order.shipping_order_reference = f'ZUHRAAN-{order_id}'
        order.shipping_status = 'Failed'
        db.session.commit()
    add_admin_session(client, admin_id)

    response = client.post(
        f'/admin/orders/{order_id}/ithink/create',
        data={'weight': '0.5', 'length': '10', 'width': '10', 'height': '10'},
    )

    assert response.status_code == 302
    assert create_calls == [f'ZUHRAAN-{order_id}']
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.awb_number == 'AWB-RETRY'
        assert order.shipping_status == 'Success'
        assert order.status == 'processing'


def test_admin_can_refresh_ithink_tracking(client, monkeypatch):
    class FakeIThinkClient:
        def track_awbs(self, awb_numbers):
            assert awb_numbers == ['AWB123']
            return {
                'status_code': 200,
                'data': {
                    'AWB123': {
                        'awb_no': 'AWB123',
                        'logistic': 'Xpressbees',
                        'current_status': 'In Transit',
                        'current_status_code': 'UD',
                    },
                },
            }

    monkeypatch.setitem(app.config, 'ITHINK_ENABLED', True)
    monkeypatch.setattr('routes.admin.IThinkClient', FakeIThinkClient)
    with app.app_context():
        admin_id, order_id = create_fulfillable_order()
        order = db.session.get(Order, order_id)
        order.shipping_order_reference = 'ZUHRAAN-1'
        order.awb_number = 'AWB123'
        db.session.commit()
    add_admin_session(client, admin_id)

    response = client.post(f'/admin/orders/{order_id}/ithink/refresh')

    assert response.status_code == 302
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.shipping_status == 'In Transit'
        assert order.shipping_status_code == 'UD'
        assert order.shipping_provider == 'Xpressbees'
        assert order.status == 'shipped'
