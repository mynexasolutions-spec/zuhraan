# Zuhraan Perfumes E-Commerce

Welcome to the Zuhraan Perfumes application. This platform is a fully-featured, luxury e-commerce application powered by Python (Flask), SQLAlchemy, and Razorpay.

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
- **Security:** CSRF Validation globally. Sensitive tokens (`.env`) like Razorpay Key Secret/IDs strings are decoupled safely via environment variables and ignored from Git. NEVER commit your `.env` keys.
- **Currency:** Fully localized format to `INR` (₹).

> **Note**: For initial setup, make sure you configure `.env` mimicking whatever was stored securely offline, as the project deliberately lacks it on GitHub for defense architecture. 

*Property of Zuhraan Perfumes.*

## VPS deployment

1. Place the application in `/srv/zuhraan`, create `.venv`, and install
   `requirements.txt` inside it.
2. Create `/srv/zuhraan/.env` from `.env.example`. Keep it readable only by the
   service account and never commit it.
3. Run `python -m pytest -q` and `python -m flask --app app gokwik-readiness`
   from the virtual environment.
4. Adapt `deploy/zuhraan.service.example`, install it as a systemd unit, then
   enable and start the service.
5. Adapt `deploy/nginx.conf.example`, validate the Nginx configuration, and
   reload Nginx after the HTTPS certificate paths exist.
6. Verify `GET https://<store-domain>/healthz` returns HTTP 200 and the GoKwik
   health route returns 401 without merchant credentials.

Do not run `python app.py` or `flask run` as the public server. Schema changes
must be applied as migrations before restarting Gunicorn; application startup
does not create or alter production tables.

## GoKwik Checkout integration

This custom Flask storefront uses a staged GoKwik integration. Keep the native
Razorpay/COD checkout active until every GoKwik sandbox flow has passed.

1. Complete GoKwik merchant onboarding and request the custom web integration
   specification, sandbox Merchant ID, App ID, and App Secret.
2. Copy `.env.example` to `.env` and fill the GoKwik values locally. Never
   commit `.env` or send credentials through chat or email.
3. Run `migrations/20261009_gokwik.sql` once in the Supabase SQL Editor before
   deploying the GoKwik-enabled application. It is idempotent and keeps schema
   changes out of application startup.
4. After deploying the server-side endpoints, use `GOKWIK_ENABLED=1` and
   `GOKWIK_STOREFRONT_ENABLED=0` while GoKwik validates the public APIs.
5. Set `GOKWIK_STOREFRONT_ENABLED=1` only after the sandbox APIs and checkout
   flow pass end-to-end testing.
6. Use `GOKWIK_ENV=sandbox` while testing. Production uses a different SDK URL
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
flask --app app gokwik-readiness
```

The command checks configuration, database columns, the checkout-session table,
and the authenticated health route. It never prints credential values. Admin
status changes for GoKwik orders are synchronized to GoKwik; shipped and
delivered orders require both a shipping provider and AWB/tracking number.
This store has no wallet ledger, so the wallet balance endpoint reports zero
and wallet deduction is rejected rather than creating an unverified paid order.

Activation order:

1. In sandbox, set `GOKWIK_ENABLED=1` and keep
   `GOKWIK_STOREFRONT_ENABLED=0`.
2. Deploy to a public HTTPS domain and give GoKwik the API base URL above.
3. Have GoKwik validate the health, cart, coupon, address, shipping, and order
   endpoints with the sandbox App ID and App Secret.
4. Set `GOKWIK_STOREFRONT_ENABLED=1`, restart, and complete COD, prepaid,
   failed-payment, cancellation, and inventory tests.
5. Switch to `GOKWIK_ENV=production` only after GoKwik issues/activates
   production credentials and approves the sandbox test results.

Before enabling checkout, ask GoKwik to confirm the endpoint base URL and
payload contract for the merchant account. The implementation intentionally
rejects unconfigured GoKwik fee lines so a discount or surcharge cannot alter
the locally verified order total.

## Shiprocket fulfillment

Shiprocket is an optional fulfillment layer for native and GoKwik orders. Credentials
must belong to a dedicated Shiprocket API user and stay in the deployment environment.
Apply `migrations/20261010_shiprocket.sql`, then configure the variables documented in
`.env.example`. Keep `SHIPROCKET_ENABLED=0` until the migration and pickup location are
ready.

After enabling it, run:

```bash
flask --app app shiprocket-readiness --verify-api
```

The command authenticates but does not create a shipment. In Shiprocket, configure the
tracking webhook as `https://<store-domain>/api/fulfillment/webhook`, set its security
token to the exact `SHIPROCKET_WEBHOOK_TOKEN` value, and enable it. The URL deliberately
does not contain Shiprocket-reserved keywords.

Admins create the Shiprocket order and AWB from **Admin > Orders**. Pickup can be
scheduled explicitly or automatically through **Admin > Settings**. After pickup,
authenticated webhook events update the local shipped/delivered status and synchronize
GoKwik orders. Customers see courier, live status, and a tracking link in their account.
