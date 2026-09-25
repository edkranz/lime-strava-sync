"""
mitmproxy addon: harvest the Lime app's live bearer token off your phone.

Run:  mitmdump -s mitm_harvest.py
Then point your iPhone's Wi-Fi proxy at this Mac and open the Lime app.

Every time the app calls Lime, this grabs the Authorization: Bearer token and
writes it to probe-output/01-login.json (the file sync.py reads). Because it's
the *app's own* token, using it does NOT create a competing session — the app
keeps working. Leave mitmdump running and the token auto-refreshes whenever the
app talks to Lime.

Nothing is modified in flight; this only reads headers.
"""
import json
from pathlib import Path
from mitmproxy import http

OUT = Path(__file__).parent / "probe-output" / "01-login.json"
_last = None


def request(flow: http.HTTPFlow) -> None:
    global _last
    if "lime.bike" not in flow.request.pretty_host:
        return
    auth = flow.request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return
    token = auth.split(" ", 1)[1].strip()
    if not token or token == _last:
        return
    _last = token
    OUT.parent.mkdir(exist_ok=True)
    data = {}
    if OUT.exists():
        try:
            data = json.loads(OUT.read_text())
        except json.JSONDecodeError:
            data = {}
    data["token"] = token
    data["_source"] = "mitm_harvest (app session)"
    OUT.write_text(json.dumps(data))
    print(f"[lime-harvest] ✅ captured token ({len(token)} chars) from "
          f"{flow.request.pretty_host} -> {OUT}")
