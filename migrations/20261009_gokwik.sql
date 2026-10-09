BEGIN;

ALTER TABLE public.product
    ADD COLUMN IF NOT EXISTS show_top_notes BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS show_middle_notes BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS show_base_notes BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS show_longevity BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS show_projection BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS variants_enabled BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE public."order"
    ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50),
    ADD COLUMN IF NOT EXISTS gokwik_transaction_id VARCHAR(150),
    ADD COLUMN IF NOT EXISTS shipping_provider VARCHAR(100),
    ADD COLUMN IF NOT EXISTS awb_number VARCHAR(150);

CREATE TABLE IF NOT EXISTS public.go_kwik_checkout_session (
    id VARCHAR(64) PRIMARY KEY,
    user_id INTEGER REFERENCES public."user"(id) ON DELETE SET NULL,
    cart_data TEXT NOT NULL,
    customer_data TEXT NOT NULL DEFAULT '{}',
    coupon_code VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    order_id INTEGER UNIQUE REFERENCES public."order"(id) ON DELETE SET NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (timezone('utc', now())),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (timezone('utc', now()))
);

CREATE INDEX IF NOT EXISTS ix_go_kwik_checkout_session_expires_at
    ON public.go_kwik_checkout_session (expires_at);

ALTER TABLE public.go_kwik_checkout_session ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.go_kwik_checkout_session FROM anon, authenticated;

COMMIT;
