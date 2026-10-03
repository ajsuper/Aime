#!/usr/bin/env python3
"""Show or apply Aime's SMS HELP / STOP keyword replies on the AWS number.

AWS End User Messaging answers HELP and STOP itself, from keyword settings on
the origination number or pool; the app never sees those inbound texts. The
reply text lives in aime.config (SMS_HELP_REPLY / SMS_STOP_REPLY) so it is
versioned with the code and the Terms, and this script copies it onto the
number with PutKeyword. STOP keeps its OPT_OUT action, so a STOP still adds
the sender to the opt-out list.

Examples:
    # Print what would be set (default — changes nothing)
    ./scripts/sms_keywords.py

    # Push the replies to AWS_SMS_ORIGINATION_IDENTITY
    ./scripts/sms_keywords.py --apply

    # A different number or pool
    ./scripts/sms_keywords.py --apply --origination pool-abc123

Uses boto3's standard credential chain; the caller needs
sms-voice:PutKeyword. AWS_SMS_REGION picks the region, as for sending.
"""

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from aime import config  # noqa: E402

# (keyword, reply, action) — HELP just answers; STOP answers and opts out.
KEYWORDS = (
    ("HELP", config.SMS_HELP_REPLY, "AUTOMATIC_RESPONSE"),
    ("STOP", config.SMS_STOP_REPLY, "OPT_OUT"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="set the keywords in AWS (default: only print them)")
    parser.add_argument("--origination",
                        default=os.environ.get("AWS_SMS_ORIGINATION_IDENTITY", ""),
                        help="phone number, pool or their ID/ARN "
                             "(default: AWS_SMS_ORIGINATION_IDENTITY)")
    args = parser.parse_args()

    for keyword, reply, action in KEYWORDS:
        print(f"{keyword} ({action}, {len(reply)} chars):\n  {reply}")
    if not args.apply:
        print("\nDry run — pass --apply to set these in AWS.")
        return 0
    if not args.origination.strip():
        print("No origination identity: set AWS_SMS_ORIGINATION_IDENTITY "
              "or pass --origination.", file=sys.stderr)
        return 2

    import boto3

    region = os.environ.get("AWS_SMS_REGION", "").strip() or None
    client = boto3.client("pinpoint-sms-voice-v2", region_name=region)
    for keyword, reply, action in KEYWORDS:
        client.put_keyword(
            OriginationIdentity=args.origination.strip(),
            Keyword=keyword,
            KeywordMessage=reply,
            KeywordAction=action,
        )
        print(f"Set {keyword} on {args.origination.strip()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
