# PayPal Top-Up Setup

The billing system (`billing.py` + `/account` page) accepts real payments through
PayPal Checkout (Orders v2 API). Money goes to whichever PayPal account owns the
API credentials — no email address is ever placed in the code.

Without credentials the app runs in **test mode**: the `/account` page shows an
instant fake top-up button and no real money moves.

## 1. Upgrade your PayPal account to Business

A personal PayPal account cannot receive API payments.

- Log in at [paypal.com](https://www.paypal.com) with the account that should receive the money
- Settings → **Upgrade to Business account** (free)

## 2. Create API credentials

- Log in at [developer.paypal.com](https://developer.paypal.com) with the same account
- Go to **Apps & Credentials**
- Select the **Sandbox** tab (for testing) → **Create App**
- Copy the **Client ID** and **Secret**
- Repeat on the **Live** tab when ready for real payments (separate credentials)

## 3. Run the app with credentials

Never commit these values. Pass them as environment variables:

```bash
export PAYPAL_CLIENT_ID="your-client-id"
export PAYPAL_CLIENT_SECRET="your-secret"
export PAYPAL_ENV=sandbox   # or "live" for real payments
uv run flask_app.py
```

When `PAYPAL_CLIENT_ID` is set:

- `/account` shows PayPal buttons instead of the fake top-up
- the fake top-up endpoint returns 403

## 4. Test in sandbox

- developer.paypal.com → **Testing Tools** → **Sandbox Accounts**
- Use the generated *personal* (buyer) sandbox account to pay on `/account`
- Balance credits after capture; no real money moves

## 5. Go live

```bash
export PAYPAL_CLIENT_ID="live-client-id"
export PAYPAL_CLIENT_SECRET="live-secret"
export PAYPAL_ENV=live
```

Real payments now land in the PayPal account that owns the live app.

## How it works (security notes)

- The browser only chooses the amount and clicks the PayPal button; the server
  creates the order (`POST /api/paypal/create_order`, 1–500 €) and captures it
  (`POST /api/paypal/capture_order`)
- The credited amount is read from **PayPal's capture response**, never from the
  client
- Captures are idempotent: replaying the same capture ID credits nothing extra
- Currency is enforced to EUR

## Related commands

```bash
python billing.py seed            # create test accounts (alice/bob/carol)
python billing.py list            # list accounts and balances
python billing.py topup alice 5   # manual CLI credit
python billing.py check           # self-check
```
