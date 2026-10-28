import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any

import requests
from flask import current_app

from models import Order


ITHINK_API_BASE_URL = 'https://my.ithinklogistics.com/api_v3'
ITHINK_TRACKING_API_BASE_URL = 'https://api.ithinklogistics.com/api_v3'
COD_PAYMENT_METHODS = frozenset({'cod'})


class IThinkRequestError(RuntimeError):
    """Raised when iThink Logistics rejects or cannot complete an API request."""


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f'{label} must be a valid number.') from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f'{label} must be greater than zero.')
    return amount


def normalize_indian_phone(value: Any) -> str:
    """Normalize an Indian phone number to the ten-digit local form."""
    digits = ''.join(character for character in str(value or '') if character.isdigit())
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError('Customer phone must contain a valid 10-digit Indian number.')
    return digits


def normalize_indian_pincode(value: Any) -> str:
    """Normalize and validate a six-digit Indian delivery pincode."""
    raw_value = str(value or '').strip()
    digits = ''.join(character for character in raw_value if character.isdigit())
    if len(digits) != 6:
        # Older rows can contain a numeric pincode serialized as ``400001.0``.
        # Accept only a six-digit integer with a zero-only decimal suffix.
        integer_like = re.fullmatch(r'(\d{6})\.0+', raw_value)
        if integer_like:
            return integer_like.group(1)
        raise ValueError('Delivery pincode must contain a valid 6-digit Indian pincode.')
    return digits


_indian_phone = normalize_indian_phone
_indian_pincode = normalize_indian_pincode


def parcel_dimensions(
    weight: Any,
    length: Any,
    width: Any,
    height: Any,
) -> dict[str, float]:
    """Validate iThink parcel measurements and return API-ready floats."""
    return {
        'weight': float(_positive_decimal(weight, 'Weight')),
        'length': float(_positive_decimal(length, 'Length')),
        'width': float(_positive_decimal(width, 'Width')),
        'height': float(_positive_decimal(height, 'Height')),
    }


def _response_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ('html_message', 'message', 'error', 'status'):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        data = payload.get('data')
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    for key in ('remark', 'message', 'status'):
                        nested_value = value.get(key)
                        if isinstance(nested_value, str) and nested_value.strip():
                            return nested_value.strip()[:500]
    return 'iThink Logistics returned an unexpected response.'


def _response_shape(value: Any) -> str:
    if isinstance(value, dict):
        keys = ', '.join(sorted(str(key) for key in value.keys())[:20])
        return f'dict keys=[{keys}]'
    if isinstance(value, list):
        return f'list length={len(value)}'
    if value is None:
        return 'null'
    return type(value).__name__


def _is_success_response(payload: dict[str, Any]) -> bool:
    top_level_status = str(payload.get('status') or '').strip().lower()
    if top_level_status in {'success', 'successful'}:
        return True
    status_code = payload.get('status_code')
    if str(status_code).strip() == '200' and 'data' not in payload:
        return True
    data = payload.get('data')
    if isinstance(data, list):
        return any(
            isinstance(item, dict)
            and str(item.get('status') or '').strip().lower() in {'success', 'successful'}
            for item in data
        )
    if isinstance(data, dict):
        return any(
            isinstance(item, dict)
            and str(item.get('status') or '').strip().lower() in {'success', 'successful'}
            for item in data.values()
        )
    return False


def _format_decimal(value: Decimal | float | int | str) -> str:
    return format(Decimal(str(value)).quantize(Decimal('0.01')), 'f')


class IThinkClient:
    """Authenticated boundary for the iThink Logistics v3 API."""

    def __init__(self) -> None:
        self.access_token = str(current_app.config.get('ITHINK_ACCESS_TOKEN') or '').strip()
        self.secret_key = str(current_app.config.get('ITHINK_SECRET_KEY') or '').strip()
        self.pickup_address_id = str(
            current_app.config.get('ITHINK_PICKUP_ADDRESS_ID') or ''
        ).strip()
        self.return_address_id = str(
            current_app.config.get('ITHINK_RETURN_ADDRESS_ID') or ''
        ).strip()
        if not self.access_token or not self.secret_key:
            raise IThinkRequestError(
                'iThink API access token and secret key are not configured.'
            )
        if not self.pickup_address_id or not self.return_address_id:
            raise IThinkRequestError(
                'iThink pickup and return address IDs are not configured.'
            )

    def _send(
        self,
        path: str,
        data: dict[str, Any],
        *,
        tracking: bool = False,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        payload = {
            **data,
            'access_token': self.access_token,
            'secret_key': self.secret_key,
        }
        url = (ITHINK_TRACKING_API_BASE_URL if tracking else ITHINK_API_BASE_URL) + path
        attempts = 3 if retry_safe else 1
        final_error: IThinkRequestError | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    url,
                    json={'data': payload},
                    headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                    timeout=20,
                )
            except requests.RequestException as error:
                final_error = IThinkRequestError(f'iThink request failed: {error}')
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise final_error from error

            try:
                response_payload: Any = response.json()
            except ValueError as error:
                raise IThinkRequestError(
                    f'iThink returned non-JSON HTTP {response.status_code}.'
                ) from error

            if not isinstance(response_payload, dict):
                raise IThinkRequestError('iThink returned an invalid JSON object.')
            if not 200 <= response.status_code < 300:
                raise IThinkRequestError(
                    f'iThink returned HTTP {response.status_code}: '
                    f'{_response_message(response_payload)}'
                )
            if not _is_success_response(response_payload):
                raise IThinkRequestError(_response_message(response_payload))
            return response_payload

        if final_error is None:
            raise IThinkRequestError('iThink request failed without a response.')
        raise final_error

    def get_warehouses(self) -> dict[str, Any]:
        """Validate credentials and retrieve registered warehouse records."""
        return self._send('/warehouse/get.json', {}, retry_safe=True)

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send('/order/add.json', payload)

    def get_order_details(
        self,
        *,
        order_reference: str | None = None,
        awb_number: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        order_no = str(order_reference or '').strip()
        awb = str(awb_number or '').strip()
        start = str(start_date or '').strip()
        end = str(end_date or '').strip()
        if not order_no and not awb:
            raise ValueError('An iThink order reference or AWB is required.')
        if not start or not end:
            raise ValueError('iThink order details require a start and end date.')
        return self._send(
            '/order/get_details.json',
            {
                'awb_number_list': awb,
                'order_no': order_no,
                'start_date': start,
                'end_date': end,
            },
            retry_safe=True,
        )

    def cancel_order(self, awb_number: str) -> dict[str, Any]:
        awb = str(awb_number or '').strip()
        if not awb:
            raise ValueError('An AWB number is required to cancel an iThink order.')
        return self._send('/order/cancel.json', {'awb_numbers': awb})

    def track_awbs(self, awb_numbers: list[str]) -> dict[str, Any]:
        cleaned_awbs = [str(awb).strip() for awb in awb_numbers if str(awb).strip()]
        if not cleaned_awbs:
            raise ValueError('At least one AWB number is required for tracking.')
        if len(cleaned_awbs) > 10:
            raise ValueError('iThink tracking supports at most 10 AWBs per request.')
        return self._send(
            '/order/track.json',
            {'awb_number_list': ','.join(cleaned_awbs)},
            tracking=True,
            retry_safe=True,
        )


def build_ithink_order_payload(
    order: Order,
    dimensions: dict[str, float],
) -> dict[str, Any]:
    """Build an iThink domestic order request from the local order."""
    if not order.items:
        raise ValueError('The order has no items to ship.')
    country = str(order.country or 'India').strip()
    phone = _indian_phone(order.customer_phone)
    pincode = _indian_pincode(order.pincode)
    required_address = (
        order.customer_name,
        phone,
        order.address_line1,
        order.city,
        order.state,
        pincode,
        country,
    )
    if not all(required_address):
        raise ValueError('The order does not contain a complete delivery address.')

    if len(order.items) > 40:
        raise ValueError('iThink supports at most 40 products in one shipment.')

    customer_parts = str(order.customer_name).strip().split(maxsplit=1)
    item_subtotal = sum(
        Decimal(str(item.price_at_time)) * item.quantity for item in order.items
    )
    shipping_charges = Decimal(str(order.shipping_charges or 0))
    charged_subtotal = max(Decimal('0'), Decimal(str(order.total_amount)) - shipping_charges)
    total_discount = max(Decimal('0'), item_subtotal - charged_subtotal)
    is_cod = order.payment_method in COD_PAYMENT_METHODS

    products = [
        {
            'product_name': item.variant.product.name,
            'product_sku': f'VARIANT-{item.variant_id}',
            'product_quantity': str(item.quantity),
            'product_price': _format_decimal(item.price_at_time),
            'product_tax_rate': '0',
            'product_hsn_code': '',
            'product_discount': '0',
        }
        for item in order.items
    ]
    shipment: dict[str, Any] = {
        'waybill': '',
        'order': f'ZUHRAAN-{order.id}',
        'sub_order': '',
        'order_date': order.created_at.strftime('%d-%m-%Y %H:%M:%S'),
        'total_amount': _format_decimal(order.total_amount),
        'name': str(order.customer_name).strip(),
        'company_name': '',
        'add': str(order.address_line1).strip(),
        'add2': str(order.address_line2 or '').strip(),
        'add3': '',
        'pin': pincode,
        'city': str(order.city).strip(),
        'state': str(order.state).strip(),
        'country': country,
        'phone': phone,
        'alt_phone': '',
        'email': str(order.customer_email or '').strip(),
        'is_billing_same_as_shipping': 'yes',
        'billing_name': customer_parts[0],
        'billing_company_name': '',
        'billing_add': str(order.address_line1).strip(),
        'billing_add2': str(order.address_line2 or '').strip(),
        'billing_add3': '',
        'billing_pin': pincode,
        'billing_city': str(order.city).strip(),
        'billing_state': str(order.state).strip(),
        'billing_country': country,
        'billing_phone': phone,
        'billing_alt_phone': '',
        'billing_email': str(order.customer_email or '').strip(),
        'products': products,
        'shipment_length': str(dimensions['length']),
        'shipment_width': str(dimensions['width']),
        'shipment_height': str(dimensions['height']),
        'weight': str(dimensions['weight']),
        'shipping_charges': _format_decimal(shipping_charges),
        'giftwrap_charges': '0',
        'transaction_charges': '0',
        'total_discount': _format_decimal(total_discount),
        'first_attemp_discount': '0',
        'cod_charges': '0',
        'advance_amount': '0' if is_cod else _format_decimal(order.total_amount),
        'cod_amount': _format_decimal(order.total_amount) if is_cod else '0',
        'payment_mode': 'COD' if is_cod else 'Prepaid',
        'reseller_name': '',
        'eway_bill_number': '',
        'gst_number': '',
        'return_address_id': str(current_app.config['ITHINK_RETURN_ADDRESS_ID']),
    }

    store_id = str(current_app.config.get('ITHINK_STORE_ID') or '').strip()
    api_source = str(current_app.config.get('ITHINK_API_SOURCE') or '').strip()
    if store_id:
        shipment['store_id'] = store_id
        shipment['api_source'] = api_source or '1'
    elif api_source:
        shipment['api_source'] = api_source

    payload: dict[str, Any] = {
        'shipments': [shipment],
        'pickup_address_id': str(current_app.config['ITHINK_PICKUP_ADDRESS_ID']),
    }
    logistics = str(current_app.config.get('ITHINK_LOGISTICS') or '').strip()
    service_type = str(current_app.config.get('ITHINK_SERVICE_TYPE') or 'surface').strip()
    if logistics:
        payload['logistics'] = logistics
    payload['s_type'] = service_type
    return payload


_AWB_KEYS = ('waybill', 'awb_no', 'awb_number', 'awb', 'airwaybill')
_ORDER_REFERENCE_KEYS = (
    'refnum',
    'order_reference',
    'reference_number',
    'order_no',
    'order_id',
    'order',
)
_COURIER_KEYS = ('logistic_name', 'logistic', 'courier_name', 'courier')
_TRACKING_URL_KEYS = ('tracking_url', 'tracking_link')
_RECORD_KEYS = frozenset({
    *_AWB_KEYS,
    *_ORDER_REFERENCE_KEYS,
    *_COURIER_KEYS,
    *_TRACKING_URL_KEYS,
    'status',
    'remark',
    'message',
    'error',
    'html_message',
})


def _clean_text(value: Any, limit: int = 500) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_identifier(value: Any) -> str | None:
    text = _clean_text(value)
    if text is None or text.lower() in {'none', 'null', '0'}:
        return None
    return text


def _first_identifier(record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _clean_identifier(record.get(key))
        if value is not None:
            return value
    return None


def _first_text(record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _clean_text(record.get(key))
        if value is not None:
            return value
    return None


def _record_awb(record: dict[str, Any]) -> str | None:
    return _first_identifier(record, _AWB_KEYS)


def _record_order_reference(record: dict[str, Any]) -> str | None:
    return _first_identifier(record, _ORDER_REFERENCE_KEYS)


def _record_status(record: dict[str, Any]) -> str | None:
    return _first_text(record, ('status', 'latest_courier_status', 'current_status'))


def _record_remark(record: dict[str, Any]) -> str | None:
    return _first_text(record, ('remark', 'message', 'error', 'html_message'))


def _shipment_records(value: Any):
    if isinstance(value, dict):
        if _RECORD_KEYS.intersection(value):
            yield value
        for nested_value in value.values():
            yield from _shipment_records(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            yield from _shipment_records(nested_value)


def _record_score(record: dict[str, Any], expected_order_reference: str | None) -> int:
    score = 0
    reference = _record_order_reference(record)
    if _record_awb(record):
        score += 10_000_000
    if expected_order_reference and reference == expected_order_reference:
        score += 1_000_000
    if reference:
        score += 1_000
    status = str(_record_status(record) or '').strip().lower()
    if status in {'success', 'successful'}:
        score += 100
    elif status:
        score -= 500
    if _first_text(record, _COURIER_KEYS):
        score += 10
    if _first_text(record, _TRACKING_URL_KEYS):
        score += 1
    return score


def _first_data_record(
    payload: dict[str, Any],
    *,
    expected_order_reference: str | None = None,
    awb_number: str | None = None,
    strict_reference: bool = False,
    response_label: str = 'shipment record',
) -> dict[str, Any]:
    candidates = list(_shipment_records(payload.get('data')))
    if not candidates:
        candidates = list(_shipment_records(payload))
    if awb_number:
        matching_awbs = [
            record for record in candidates if _record_awb(record) == awb_number
        ]
        candidates = matching_awbs
    if expected_order_reference:
        matching_references = [
            record
            for record in candidates
            if _record_order_reference(record) == expected_order_reference
        ]
        if matching_references or strict_reference:
            candidates = matching_references
    if candidates:
        return max(
            candidates,
            key=lambda record: _record_score(record, expected_order_reference),
        )
    message = _response_message(payload)
    detail = '' if message.lower() in {
        'success',
        'successful',
        'iThink logistics returned an unexpected response.',
    } else f' Message: {message}.'
    raise IThinkRequestError(
        f'iThink returned no {response_label}. '
        f'Response data shape: {_response_shape(payload.get("data"))}.{detail}'
    )


def extract_created_shipment(
    payload: dict[str, Any],
    expected_order_reference: str | None = None,
) -> dict[str, str | None]:
    """Normalize the iThink create-order response."""
    record = _first_data_record(
        payload,
        expected_order_reference=expected_order_reference,
    )
    status = _record_status(record) or _clean_text(payload.get('status')) or 'Manifested'
    return {
        'order_reference': _record_order_reference(record),
        'awb_number': _record_awb(record),
        'courier_name': _first_text(record, _COURIER_KEYS),
        'tracking_url': _first_text(record, _TRACKING_URL_KEYS),
        'status': status,
        'remark': _record_remark(record),
    }


def extract_order_details(
    payload: dict[str, Any],
    *,
    order_reference: str | None = None,
    awb_number: str | None = None,
) -> dict[str, str | None]:
    """Normalize an existing iThink order returned by the details endpoint."""
    record = _first_data_record(
        payload,
        expected_order_reference=order_reference,
        awb_number=awb_number,
        strict_reference=bool(order_reference),
        response_label='order details record',
    )
    return {
        'order_reference': _record_order_reference(record) or order_reference,
        'awb_number': _record_awb(record) or awb_number,
        'courier_name': _first_text(record, _COURIER_KEYS),
        'tracking_url': _first_text(record, _TRACKING_URL_KEYS),
        'status': _first_text(
            record,
            ('latest_courier_status', 'current_status', 'status'),
        ) or 'Manifested',
        'remark': _record_remark(record),
    }


def extract_tracking_data(
    payload: dict[str, Any],
    awb_number: str,
) -> dict[str, str | None]:
    """Normalize one AWB record from the iThink tracking response."""
    data = payload.get('data')
    if not isinstance(data, dict):
        raise IThinkRequestError('iThink returned no tracking data.')
    record = data.get(awb_number)
    if not isinstance(record, dict):
        for candidate in data.values():
            if isinstance(candidate, dict) and str(candidate.get('awb_no') or '').strip() == awb_number:
                record = candidate
                break
    if not isinstance(record, dict):
        raise IThinkRequestError(f'iThink returned no tracking record for AWB {awb_number}.')
    return {
        'status': str(record.get('current_status') or record.get('latest_courier_status') or '').strip() or None,
        'status_code': str(record.get('current_status_code') or '').strip() or None,
        'courier_name': str(record.get('logistic') or '').strip() or None,
        'awb_number': str(record.get('awb_no') or awb_number).strip() or awb_number,
        'tracking_url': str(record.get('tracking_url') or '').strip() or None,
    }


def api_datetime(value: datetime) -> str:
    """Format a datetime for iThink's tracking change-feed API."""
    return value.strftime('%Y-%m-%d %H:%M:%S')
