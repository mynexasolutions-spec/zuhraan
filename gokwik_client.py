import time
from typing import Any

import requests
from flask import current_app


GOKWIK_PAYMENT_METHODS = frozenset({'cod', 'gokwik_prepaid', 'wallet'})
GOKWIK_ORDER_STATUS_MAP = {
    'pending': 'Pending',
    'processing': 'Confirmed',
    'cancelled': 'Cancelled',
}


class GoKwikRequestError(RuntimeError):
    """Raised when an authenticated GoKwik API request cannot be completed."""


def is_gokwik_payment_method(payment_method: str | None) -> bool:
    return payment_method in GOKWIK_PAYMENT_METHODS


def _order_update_payload(
    order_id: int,
    order_status: str,
    shipping_provider: str | None,
    awb_number: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {'merchant_order_id': str(order_id)}
    mapped_status = GOKWIK_ORDER_STATUS_MAP.get(order_status)
    if mapped_status:
        payload['order_status'] = mapped_status
    if shipping_provider and awb_number:
        payload['shipping_provider'] = shipping_provider
        payload['awb_number'] = awb_number
    return payload


def sync_gokwik_order(
    order_id: int,
    order_status: str,
    shipping_provider: str | None,
    awb_number: str | None,
) -> bool:
    """Send supported order status and tracking changes to GoKwik."""
    if not current_app.config.get('GOKWIK_ENABLED'):
        return False

    payload = _order_update_payload(order_id, order_status, shipping_provider, awb_number)
    if len(payload) == 1:
        return False

    url = current_app.config['GOKWIK_API_BASE_URL'] + 'v3/orders/update'
    headers = {
        'Content-Type': 'application/json',
        'gk-app-id': current_app.config['GOKWIK_APP_ID'],
        'gk-app-secret': current_app.config['GOKWIK_APP_SECRET'],
    }
    final_error: GoKwikRequestError | None = None

    for attempt in range(1, 4):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=5)
            if 200 <= response.status_code < 300:
                return True
            response_excerpt = response.text[:500]
            final_error = GoKwikRequestError(
                f'GoKwik order update returned HTTP {response.status_code}: {response_excerpt}'
            )
        except requests.RequestException as error:
            final_error = GoKwikRequestError(f'GoKwik order update request failed: {error}')

        current_app.logger.warning(
            'GoKwik order update attempt failed.',
            extra={
                'gokwik_order_id': order_id,
                'gokwik_order_status': order_status,
                'gokwik_attempt': attempt,
            },
        )
        if attempt < 3:
            time.sleep(0.25 * attempt)

    if final_error is None:
        raise GoKwikRequestError('GoKwik order update failed without a response.')
    raise final_error
