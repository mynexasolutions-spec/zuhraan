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

- **Database:** Uses SQLite + SQLAlchemy ORM locally.
- **Security:** CSRF Validation globally. Sensitive tokens (`.env`) like Razorpay Key Secret/IDs strings are decoupled safely via environment variables and ignored from Git. NEVER commit your `.env` keys.
- **Currency:** Fully localized format to `INR` (₹).

> **Note**: For initial setup, make sure you configure `.env` mimicking whatever was stored securely offline, as the project deliberately lacks it on GitHub for defense architecture. 

*Property of Zuhraan Perfumes.*
