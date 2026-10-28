# Zuhraan Perfumes E-Commerce

Welcome to the Zuhraan Perfumes application. This platform is a fully-featured, luxury e-commerce application powered by Python (Flask), SQLAlchemy, and GoKwik Checkout.

## 👑 Admin Dashboard Manual

The administrative interface gives complete control over the store's operations. Access the dashboard by logging in via a superuser or admin-level account and navigating to: `/admin` (or by clicking the Admin Panel link if authorized). 

### 1. Dashboard Overview
- **Metrics Bar:** Get an instantaneous snapshot of total site revenue (in ₹), total accumulated orders, active user count, and overall product list.
- **Recent Orders:** View the latest 5 incoming orders simultaneously with their full status, payment data, and one-click fulfillment actions.

### 2. Categories Management
- **Add / Edit Categories:** Classify perfumes into distinct collections.
- **Images:** You can directly upload category cover photos. These heavily dictate the visual presentation on the homepage "Category Cards" section. Linking the images works seamlessly. Let a category shine with a distinct visual identity!

### 3. Products
- **Creation & Variants:** Build out comprehensive product portfolios complete with base summaries, robust detail descriptions, base prices, tags (like `best_seller` to force onto the homepage), and image hosting.
- **Modifiers:** You can actively modify any existing product (description, name, price) directly via the edit button. Warnings correctly intercept deletion attempts.

### 4. Offers & Hero Banners
- Located under "Offers" in the admin sidebar.
- **Dynamic Banners:** Any panoramic images you upload here automatically sequence on the primary storefront homepage center banner wrapper. They auto-fade through an integrated Swiper.js layout.
- If you delete all dynamic banner uploads, the system intelligently defaults to a hardcoded luxury fallback image (`zuhran_2.webp`) to prevent structural page collapse.

### 5. Order Fulfillment
- Use the status dropdown strictly to transition orders. Currently recognized states represent a full e-commerce lifecycle (Processing -> Shipped -> Delivered -> Cancelled). 
- Changing an order's status reflects globally, so customers checking their `/account` view will see the exact state in real time.

### 6. Discount & Coupons
- Generate dynamic cart percentage triggers. 
- You can establish limited `amount` cuts or `%` percentage discounts, minimum cart values to activate the coupon, and total usage limits. Coupons can be toggled on/off instantly.

---

## 💻 Tech Stack & Deployment Security

- **Database:** Uses Supabase PostgreSQL through SQLAlchemy. Set `SUPABASE_DATABASE_URL` to the SQLAlchemy-formatted connection string from the Supabase Dashboard. URL-encode special characters in the database password and keep `?sslmode=require`.
- **Security:** CSRF validation globally. Sensitive GoKwik, iThink Logistics, Supabase, and media credentials are supplied through environment variables and ignored by Git. Never commit `.env` credentials.
- **Currency:** Fully localized format to `INR` (₹).

> **Note**: For initial setup, make sure you configure `.env` mimicking whatever was stored securely offline, as the project deliberately lacks it on GitHub for defense architecture. 

*Property of Zuhraan Perfumes.*

## VPS deployment

1. Place the application in `/srv/zuhraan`, create `.venv`, and install
   `requirements.txt` inside it.
2. Create `/srv/zuhraan/.env` from `.env.example`. Keep it readable only by the
   service account and never commit it.
3. For a brand-new Supabase project, run
   `python -m flask --app app init-db` once to create the base schema and seed
   the admin account, categories, settings, and About page. Existing databases
   should be upgraded with the SQL files in `migrations/` instead.
4. Run `python -m pytest -q` and `python -m flask --app app gokwik-readiness`
   from the virtual environment.
5. Adapt `deploy/zuhraan.service.example`, install it as a systemd unit, then
   enable and start the service.
6. Adapt `deploy/nginx.conf.example`, validate the Nginx configuration, and
   reload Nginx after the HTTPS certificate paths exist.
7. Verify `GET https://<store-domain>/healthz` returns HTTP 200 and the GoKwik
   health route returns 401 without merchant credentials.

Do not run `python app.py` or `flask run` as the public server. Schema changes
must be applied as migrations before restarting Gunicorn; application startup
does not create or alter production tables.

On Windows, `python app.py` starts the application locally with Waitress on
`http://127.0.0.1:5005`. The production Linux service continues to use Gunicorn
behind Nginx.

## Razorpay direct checkout

Direct Razorpay checkout is available from the cart when `RAZORPAY_ENABLED=1`
and the three Razorpay environment variables are configured. The server creates
the Razorpay order, verifies the browser signature and payment amount, and
reconciles `payment.captured` and `order.paid` webhooks before marking the local
order paid. Configure Razorpay's webhook URL as:

```text
https://<store-domain>/payment/webhook
```

In the Razorpay Dashboard, use the same value as `RAZORPAY_WEBHOOK_SECRET`
and enable the `payment.captured` and `order.paid` events. The browser callback
verifies a payment immediately; the webhook covers customers who close the
payment page before the browser callback completes. If a provider callback is
missed, an administrator can use **Reconcile Razorpay payment** on an unpaid
Razorpay order. Reconciliation checks Razorpay's paid amount and captured
payment before updating Supabase, inventory, and the local order status.

Run `migrations/20261027_razorpay_checkout.sql` in the Supabase SQL Editor
before enabling the checkout. The webhook signing secret must match
`RAZORPAY_WEBHOOK_SECRET`; it is never sent to the browser.

GoKwik remains available as an optional hosted checkout integration.

## GoKwik Checkout integration

This custom Flask storefront also supports GoKwik as a hosted checkout and
payment integration. Both prepaid and COD orders must be created through
authenticated GoKwik merchant callbacks.

1. Complete GoKwik merchant onboarding and request the custom web integration
   specification, sandbox Merchant ID, App ID, and App Secret.
2. Copy `.env.example` to `.env` and fill the GoKwik values locally. Never
   commit `.env` or send credentials through chat or email.
3. Run `migrations/20261009_gokwik.sql` and then
   `migrations/20261015_gokwik_only_checkout.sql` in the Supabase SQL Editor.
   They are idempotent and keep schema changes out of application startup.
4. Deploy the server endpoints with `GOKWIK_ENABLED=1` while GoKwik validates
   the public APIs and browser checkout in sandbox.
5. Use `GOKWIK_ENV=sandbox` while testing. Production uses a different SDK URL
   and must only be enabled after GoKwik confirms the integration.

On a persistent IPv4 VPS, use Supabase's Session pooler URL on port `5432` and
include `sslmode=require`. Set `APP_ENV=production`, use a random `SECRET_KEY`
of at least 32 characters, and set `TRUST_PROXY_HEADERS=1` only when Gunicorn is
reachable exclusively through the single trusted Nginx proxy. Example systemd
and Nginx configurations are under `deploy/`.

Run the application with Gunicorn, not `python app.py` or `flask run`. The
unauthenticated `/healthz` endpoint is intended for Nginx or an uptime monitor;
it reports only `ok` or `unhealthy` and verifies database connectivity.

The integration exposes the following authenticated merchant API base URL:

```text
https://<store-domain>/api/gokwik/v1
```

Configure GoKwik to use these endpoints beneath that base URL:

- `POST /cart`
- `GET /cart/get-coupons`
- `POST /cart/apply-coupon`
- `POST /cart/remove-coupon`
- `POST /cart/set-address`
- `POST /cart/set-shipping-method`
- `POST /cart/get-wallet-balance`
- `POST /cart/deduct-wallet-balance`
- `POST /cart/place-order`
- `POST /cart/check-order-exists`
- `POST /cart/update-order-status`
- `POST /cart/remove-out-of-stock-items`
- `GET|POST /cart/health-check`

Machine requests must provide `appid` and `appsecret` headers. These values are
never sent to browser JavaScript. The Merchant ID is public and is passed to
the GoKwik SDK together with an opaque, 30-minute checkout identifier.

Run the credential-safe local readiness check after changing environment
variables and restarting the application:

```text
flask --app app gokwik-readiness --verify-api
```

The command checks configuration, database columns, the checkout-session table,
and the authenticated health route. `--verify-api` also checks merchant registration
in the selected GoKwik environment. A production merchant ID cannot be used with
the sandbox SDK: GoKwik returns `Merchant not found`. Sandbox testing requires
separate sandbox credentials issued by GoKwik. Remote merchant registration does
not verify App ID/App Secret acceptance or callback delivery by GoKwik.

GoKwik must have a public HTTPS API base URL registered for this custom merchant.
`127.0.0.1` cannot receive GoKwik server callbacks. For local testing, register a
public HTTPS tunnel pointing to port 5005 with GoKwik, or use a deployed backend
with the same database and configuration. The browser origin must be approved
by GoKwik where required. Do not add permissive CORS to expose merchant secrets;
the authenticated merchant endpoints are called server-to-server.

The readiness command never prints credential values. Admin
status changes for GoKwik orders are synchronized to GoKwik; shipped and
delivered orders require both a shipping provider and AWB/tracking number.
This store has no wallet ledger, so the wallet balance endpoint reports zero
and wallet deduction is rejected rather than creating an unverified paid order.

Production activation order:

New production checkout sessions require `GOKWIK_PRODUCTION_APPROVED=1`.
Leave this unset or `0` until GoKwik confirms custom-platform onboarding and
real sandbox purchase/callback tests pass. Existing order callbacks remain
available for reconciliation. Passing local tests or merchant registration
checks alone is not production acceptance.

1. In sandbox, set `GOKWIK_ENABLED=1`.
2. Deploy to a public HTTPS domain and give GoKwik the API base URL above.
3. Have GoKwik validate the health, cart, coupon, address, shipping, and order
   endpoints with the sandbox App ID and App Secret.
4. Complete COD, prepaid, failed-payment, cancellation, idempotency, and
   inventory tests through the GoKwik checkout.
5. Switch to `GOKWIK_ENV=production` only after GoKwik issues/activates
   production credentials and approves the sandbox test results.

Before enabling checkout, ask GoKwik to confirm the endpoint base URL and
payload contract for the merchant account. The implementation intentionally
rejects unconfigured GoKwik fee lines so a discount or surcharge cannot alter
the locally verified order total.

## iThink Logistics fulfillment

iThink Logistics is the fulfillment layer for eligible COD and paid prepaid orders.
Configure `ITHINK_ACCESS_TOKEN`, `ITHINK_SECRET_KEY`, `ITHINK_PICKUP_ADDRESS_ID`, and
`ITHINK_RETURN_ADDRESS_ID` in the deployment environment. Apply
`migrations/20261027_ithink_logistics.sql` before enabling the integration.

Keep `ITHINK_ENABLED=0` until the migration and credentials are ready, then run:

```bash
flask --app app ithink-readiness --verify-api
```

The readiness command validates configuration and warehouse access without creating an
order. Eligible COD orders are submitted immediately after checkout; prepaid orders are
submitted after payment confirmation. iThink returns the AWB and courier during order
creation, and the admin order page can refresh tracking on demand.

iThink's public API does not provide a webhook contract for this integration, so schedule
the read-only tracking sync every 15 minutes:

```bash
flask --app app ithink-sync-tracking
```

The sync uses iThink's tracking API in batches of ten AWBs and updates customer and
GoKwik-facing order status.
