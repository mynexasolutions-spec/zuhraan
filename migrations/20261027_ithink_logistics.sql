BEGIN;

ALTER TABLE public."order"
    ADD COLUMN IF NOT EXISTS shipping_order_reference VARCHAR(150),
    ADD COLUMN IF NOT EXISTS shipping_status VARCHAR(100),
    ADD COLUMN IF NOT EXISTS shipping_status_code VARCHAR(30),
    ADD COLUMN IF NOT EXISTS shipping_tracking_url VARCHAR(500),
    ADD COLUMN IF NOT EXISTS shipping_tracking_updated_at TIMESTAMP WITHOUT TIME ZONE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_order_shipping_order_reference
    ON public."order" (shipping_order_reference)
    WHERE shipping_order_reference IS NOT NULL;

COMMIT;
