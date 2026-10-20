"""Set the web app's scale rules on a Container App `az containerapp show` document.

Reads the show-JSON on stdin, writes a reduced document on stdout that
`az containerapp update --yaml` will accept - the same shape _mount_files.py
produces. Existing secrets are omitted so the update does not blank them.

    usage: _scale_web.py <timezone> <start-cron> <end-cron>

The web app scales to zero when idle, and a cold start costs the first visitor
about 15 seconds. A cron rule keeps one replica up through the working day;
outside it the app still scales to zero and wakes on the first request.

That last part depends on the HTTP rule below. Container Apps applies its
default HTTP rule only when an app defines no rules at all, so adding a cron
rule on its own would leave nothing to wake the app outside office hours - the
site would simply be down until the next morning. Both rules are therefore
always written out together.

An empty start leaves out the cron rule: scale-to-zero as before.
"""
from __future__ import annotations

import json
import sys

MIN_REPLICAS = 0
MAX_REPLICAS = 2

# The value Container Apps uses for its default HTTP rule, made explicit.
HTTP_RULE = {"name": "http", "http": {"metadata": {"concurrentRequests": "10"}}}


def _cron(label: str, expr: str) -> str:
    expr = " ".join(expr.split())
    if len(expr.split(" ")) != 5:
        sys.exit(f"{label} must be a 5-field cron expression (minute hour day month weekday), got {expr!r}")
    return expr


def scale_rules(timezone: str, start: str, end: str) -> dict:
    rules = [HTTP_RULE]
    if start.strip():
        if not end.strip():
            sys.exit("WEB_WARM_START is set but WEB_WARM_END is empty")
        if not timezone.strip():
            sys.exit("WEB_WARM_TIMEZONE is empty")
        rules.append(
            {
                "name": "office-hours",
                "custom": {
                    "type": "cron",
                    "metadata": {
                        "timezone": timezone.strip(),
                        "start": _cron("WEB_WARM_START", start),
                        "end": _cron("WEB_WARM_END", end),
                        "desiredReplicas": "1",
                    },
                },
            }
        )
    return {"minReplicas": MIN_REPLICAS, "maxReplicas": MAX_REPLICAS, "rules": rules}


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("usage: _scale_web.py <timezone> <start-cron> <end-cron>")
    timezone, start, end = sys.argv[1:]
    scale = scale_rules(timezone, start, end)
    app = json.load(sys.stdin)
    props = app["properties"]
    tpl = props.setdefault("template", {})
    tpl["scale"] = scale
    cfg = dict(props.get("configuration") or {})
    cfg.pop("secrets", None)
    out = {
        "location": app.get("location"),
        "properties": {
            "environmentId": props["environmentId"],
            "configuration": cfg,
            "template": tpl,
        },
    }
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
