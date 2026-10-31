ALTER TABLE public."order"
    ADD COLUMN IF NOT EXISTS checkout_token VARCHAR(128);

CREATE UNIQUE INDEX IF NOT EXISTS uq_order_checkout_token
    ON public."order" (checkout_token)
    WHERE checkout_token IS NOT NULL;
