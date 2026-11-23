"""A small client for the Blygger Studio owner API ({mount}/api), standard library only.

Written against the contract in the blygger-studio repository (openapi.json, contract 5.14.0,
Studio 0.65.0) and docs/api.md and docs/client-access.md there, read on 2026-11-19. The contract
is private and changes between releases, so test against the deployed Studio's version
(GET /capabilities reports it) before relying on a new field.

What it does, and the scope each call needs (scopes do not imply each other):
  capabilities()                       any valid token
  get_item, list_items                 owner:read
  list_mentions                        owner:read
  create_draft, update_draft           owner:draft
  publish                              owner:publish, and refused unless permissions.yaml allows it

Rules kept here:
- The token is sent only in the Authorization header and is never put in an exception, a log
  line or a file. Errors carry the status, the API's error code and its message.
- Every list call passes a limit (an unlimited listItems once returned about 194,000 characters).
- Item writes carry expectedRevision. A 409 revision-conflict is raised to the caller, which
  decides whether to re-read; nothing here retries a write on its own.
- publish() checks the phase in permissions.yaml first, so a caller that holds a publish token in
  the wrong phase still cannot publish (CLAUDE.md rule 3).
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "PotholePortolanBot/0.1 (+https://github.com/patwater/pothole-portolan; Blygger Studio client)"


class StudioError(RuntimeError):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.details = status, code, details
        super().__init__(f"HTTP {status} {code or ''}: {message}".strip())


class WritePathUnavailable(RuntimeError):
    pass


def token_expiry(token):
    """The `exp` claim (Unix seconds) of a Studio token, or None if it cannot be read.

    Studio tokens are RS256 JWTs (better-auth's jwt plugin, src/oauth.ts). The payload is read
    without verifying the signature, because the only use is a reminder to renew; the Studio
    still verifies every request. Every Studio credential ends within 30 days by design
    (docs/client-access.md), so a reminder is the way to make renewal predictable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except (IndexError, ValueError, AttributeError):
        return None


def publish_allowed(permissions):
    """True only when permissions.yaml says the agent may publish in the current phase."""
    phase = permissions.get("phase")
    allowed = (permissions.get("publish") or {}).get("agent_may_publish_in_phases") or []
    return phase in allowed


def load_permissions(root):
    import yaml
    return yaml.safe_load((Path(root) / "permissions.yaml").read_text(encoding="utf-8")) or {}


class StudioClient:
    def __init__(self, api_base, token, user_agent=USER_AGENT, timeout=30, opener=None):
        if not api_base:
            raise ValueError("api_base is required, for example https://blyg.example.org/api")
        if not token:
            raise ValueError("a bearer token is required")
        self.api_base = api_base.rstrip("/")
        self._token = token
        self.user_agent = user_agent
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def __repr__(self):  # never show the token
        return f"StudioClient({self.api_base!r})"

    # -- transport
    def request(self, method, path, body=None, query=None):
        url = self.api_base + path
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            code, message, details = None, e.reason, None
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    code = payload.get("code")
                    message = payload.get("message") or payload.get("error") or message
                    details = payload.get("details")
            except ValueError:
                pass
            raise StudioError(e.code, code, message, details) from None

    # -- reads
    def capabilities(self):
        return self.request("GET", "/capabilities")

    def get_item(self, item_id):
        return self.request("GET", f"/items/{urllib.parse.quote(item_id, safe='')}")

    def list_items(self, status=None, limit=50, cursor=None):
        """One page: {"items": [...], "page": {"limit", "nextCursor"}}."""
        return self.request("GET", "/items", query={"status": status, "limit": int(limit), "cursor": cursor})

    def list_mentions(self, direction="inbound", limit=50, cursor=None):
        return self.request("GET", "/mentions", query={"direction": direction, "limit": int(limit), "cursor": cursor})

    # -- drafts (owner:draft)
    def create_draft(self, content_md, kind="thread"):
        return self.request("POST", "/items", {"kind": kind, "contentMd": content_md})

    def update_draft(self, item_id, content_md, expected_revision):
        """Replace the working copy. On a published item this edits the draft of its next version;
        the public version does not change until someone publishes."""
        return self.request("PATCH", f"/items/{urllib.parse.quote(item_id, safe='')}",
                            {"contentMd": content_md, "expectedRevision": int(expected_revision)})

    # -- publishing (owner:publish), gated by the phase
    def publish(self, item_id, expected_revision, note=None, permissions=None):
        if permissions is None or not publish_allowed(permissions):
            raise WritePathUnavailable(
                "Publishing is not permitted in this phase (permissions.yaml publish."
                "agent_may_publish_in_phases). In Phase 2 the supervisor publishes in the Studio.")
        body = {"expectedRevision": int(expected_revision)}
        if note:
            body["note"] = note
        return self.request("POST", f"/items/{urllib.parse.quote(item_id, safe='')}/publish", body)
