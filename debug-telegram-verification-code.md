#[OPEN] Telegram verification code not delivered

## Session
- sessionId: telegram-verification-code
- Symptom: User clicks "Connect / Sign in", UI says code requested, but no Telegram login code is received.
- Expected: Telegram delivers a login code (app/SMS/call/etc) and UI proceeds to verification.

## Hypotheses
- A: `send_code_request()` is not actually executed (request fails before call or UI never hits endpoint).
- B: `send_code_request()` succeeds but Telegram delivers via a different channel (e.g., in-app to an already signed-in device), and user is checking the wrong place.
- C: Telegram rejects or throttles code delivery (FloodWait / invalid phone formatting / account restrictions), but the UI error path hides important details.
- D: A stale local session causes `is_user_authorized()` to be true, so no code is sent and UI path differs from expectation.
- E: Telethon returns a `phone_code_hash` that is not persisted/used correctly, causing verify failures (not delivery), misinterpreted as “not receiving code”.

## Evidence to Collect
- Server-side: whether `/api/connect` is called, Telethon `send_code_request` result type, `timeout`, `next_type`, and whether exceptions occur.
- Client-side: UI state shown in `deliveryInfo` and the exact delivery method.

## Runs
- pre-fix: pending
- post-fix: pending

