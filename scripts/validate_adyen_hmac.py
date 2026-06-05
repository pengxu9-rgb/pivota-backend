#!/usr/bin/env python3
"""
Validate the DEPLOYED Adyen webhook HMAC verification against real vectors.

It extracts the EXACT functions from routes/psp_routes.py (via AST — no copy/drift)
and runs them against:
  1. Adyen's OFFICIAL published HMAC test vector (known-answer test vs Adyen's own
     computed signature) — proves the scheme matches Adyen's spec.
  2. (optional) a REAL notification from your Adyen test account — proves the
     merchant's specific HMAC key + endpoint config works end to end.

Usage:
  python3 scripts/validate_adyen_hmac.py
  # account-specific: paste a real test notification + its HMAC key
  ADYEN_HMAC_KEY=<hexkey> ADYEN_NOTIFICATION_JSON='<the NotificationRequestItem JSON>' \
    python3 scripts/validate_adyen_hmac.py
"""
import ast
import base64
import hashlib
import hmac
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PSP_ROUTES = os.path.join(HERE, "..", "routes", "psp_routes.py")

# --- Extract the real functions from the deployed source (no drift) ---
WANT = {
    "_adyen_hmac_key_bytes",
    "_adyen_escape_component",
    "_adyen_notification_signing_string",
    "_verify_adyen_notification_hmac",
}


def load_real_funcs():
    with open(PSP_ROUTES, "r") as f:
        src = f.read()
    tree = ast.parse(src)
    ns = {"hmac": hmac, "hashlib": hashlib, "base64": base64}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT:
            code = ast.get_source_segment(src, node)
            exec(compile(code, PSP_ROUTES, "exec"), ns)
            found.add(node.name)
    missing = WANT - found
    if missing:
        print(f"FATAL: could not extract {missing} from psp_routes.py", file=sys.stderr)
        sys.exit(1)
    return ns


def main():
    fns = load_real_funcs()
    verify = fns["_verify_adyen_notification_hmac"]
    sign_str = fns["_adyen_notification_signing_string"]
    key_bytes = fns["_adyen_hmac_key_bytes"]

    ok = True

    # ---- TEST 1: Adyen's official canonical HMAC test vector ----
    CANON_KEY = "44782DEF547AAA06C910C43932B1EB0C71FC68D9D0C057550C48EC2ACF6BA056"
    CANON_EXPECTED_SIG = "coqCmt/IZ4E3CzPvMY8zTjQVL5hYJUiBRg8UU+iCWo0="
    CANON_EXPECTED_SIGNING = (
        "7914073381342284::TestMerchant:TestPayment-1407325143704:1130:EUR:AUTHORISATION:true"
    )
    canon = {
        "pspReference": "7914073381342284",
        "originalReference": "",
        "merchantAccountCode": "TestMerchant",
        "merchantReference": "TestPayment-1407325143704",
        "amount": {"value": 1130, "currency": "EUR"},
        "eventCode": "AUTHORISATION",
        "success": "true",
        "additionalData": {"hmacSignature": CANON_EXPECTED_SIG},
    }
    print("=== TEST 1: Adyen official canonical vector (known-answer vs Adyen) ===")
    got_signing = sign_str(canon)
    signing_ok = got_signing == CANON_EXPECTED_SIGNING
    print(f"  signing string match : {signing_ok}")
    if not signing_ok:
        print(f"    expected: {CANON_EXPECTED_SIGNING}")
        print(f"    got     : {got_signing}")
    # recompute the signature with the real key-handling and compare to Adyen's published sig
    digest = hmac.new(key_bytes(CANON_KEY), got_signing.encode("utf-8"), hashlib.sha256).digest()
    got_sig = base64.b64encode(digest).decode("utf-8")
    sig_ok = got_sig == CANON_EXPECTED_SIG
    print(f"  signature match      : {sig_ok}  (ours == Adyen's published)")
    if not sig_ok:
        print(f"    expected: {CANON_EXPECTED_SIG}")
        print(f"    got     : {got_sig}")
    verify_ok = verify(canon, CANON_KEY) is True
    print(f"  _verify_(...) == True : {verify_ok}")
    ok = ok and signing_ok and sig_ok and verify_ok

    # ---- TEST 2: negative — tampered signature must be rejected ----
    print("=== TEST 2: tampered signature is rejected ===")
    tampered = json.loads(json.dumps(canon))
    tampered["additionalData"]["hmacSignature"] = "AAAA" + CANON_EXPECTED_SIG[4:]
    neg_ok = verify(tampered, CANON_KEY) is False
    print(f"  tampered -> False    : {neg_ok}")
    # wrong key must also fail
    wrongkey_ok = verify(canon, "00" * 32) is False
    print(f"  wrong key -> False   : {wrongkey_ok}")
    ok = ok and neg_ok and wrongkey_ok

    # ---- TEST 3 (optional): a REAL notification from the user's Adyen test account ----
    real_json = os.getenv("ADYEN_NOTIFICATION_JSON")
    real_key = os.getenv("ADYEN_HMAC_KEY")
    if real_json and real_key:
        print("=== TEST 3: REAL account notification (account-specific) ===")
        try:
            item = json.loads(real_json)
            # accept either a bare NotificationRequestItem or the full webhook envelope
            if "notificationItems" in item:
                item = item["notificationItems"][0]["NotificationRequestItem"]
            elif "NotificationRequestItem" in item:
                item = item["NotificationRequestItem"]
            res = verify(item, real_key)
            print(f"  merchantReference    : {item.get('merchantReference')}  (= your kernel order id?)")
            print(f"  eventCode/success    : {item.get('eventCode')} / {item.get('success')}")
            print(f"  HMAC verifies        : {res}")
            ok = ok and (res is True)
        except Exception as e:
            print(f"  ERROR parsing/verifying real notification: {type(e).__name__}: {e}")
            ok = False
    else:
        print("=== TEST 3: skipped (set ADYEN_NOTIFICATION_JSON + ADYEN_HMAC_KEY for the account-specific check) ===")

    print()
    print("RESULT:", "ALL PASS ✅" if ok else "FAIL ❌")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
