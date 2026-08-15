"""Add an Azure Files volume to a Container App `az containerapp show` document.

Reads the show-JSON on stdin, writes a reduced document on stdout that
`az containerapp update --yaml` will accept. Existing secrets are omitted so
the update does not blank them.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: _mount_files.py <storage-name> <mount-path>")
    storage_name, mount_path = sys.argv[1], sys.argv[2]
    app = json.load(sys.stdin)
    props = app["properties"]
    tpl = props.setdefault("template", {})
    tpl["terminationGracePeriodSeconds"] = 600
    tpl["volumes"] = [
        {
            "name": "storage",
            "storageType": "AzureFile",
            "storageName": storage_name,
        }
    ]
    for container in tpl.get("containers") or []:
        container["volumeMounts"] = [
            {"volumeName": "storage", "mountPath": mount_path}
        ]
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
