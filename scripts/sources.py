"""Stand up whatever vendors the sources repo declares.

THE PLATFORM OWNS THE MECHANISM, THE DECLARATION OWNS THE CONTENT. This file
knows how to run an OpenAPI simulator and a CDC stack; it does not know that
Contoso exists, how many vendors there are, or what any of them serve.

WHAT THIS REPLACED, and why it mattered more than it looked. This repository
used to carry its OWN copy of the vendors: all eight definition files under
`sources/`, plus a materialiser to write their bytes. It referenced
contoso-sources nowhere. The files were byte-identical to that repo's, which is
exactly why nothing noticed -- they agreed by accident of history rather than by
structure, and the first edit to a spec over there would have made this one cell
pull different bytes from every other cell. That would not have FAILED. It would
have quietly stopped meaning anything, because identical inputs are the whole
basis on which `compare_products.py` treats agreement as evidence.

PER-VENDOR TUNING SURVIVED THE MOVE, and now lives where it belongs. A memory
budget sized to a 95 MB export is a fact about that vendor, not about this
platform, so `memory`, `mem_limit` and `health` are read from the declaration.
The measurements behind them are recorded in contoso-sources/sources.yaml.

Emits a compose fragment on stdout rather than starting anything, so the
services join the same project, network and lifecycle as the rest of the stack
and `make down` really does take everything with it.
"""

from __future__ import annotations

import json
import pathlib
import sys

# THIS PLATFORM'S host ports, keyed by the declaration's vendor name. The
# declaration says which port a vendor listens on inside its own container;
# which host port it is published on is a deployment fact, and two platforms
# running side by side must not fight over it. These are the values this
# repository has always used, kept so an existing checkout does not move.
# (api, dashboard), both in THIS PLATFORM'S 1809x block. The dashboards used
# to sit at 18081/18082/18084, which was 1808x with a hole punched in it for
# Debezium at 18083 -- and 1808x is the family's AIRFLOW UI block, one port per
# Airflow platform. So contoso_web's dashboard collided with
# `databricks-platform-airflow3`'s Airflow and contoso_reference's with
# `snowflake-platform-airflow3`'s: two pairs of cells that could not run at the
# same time, and nothing said so, because a generated fragment is in no
# committed compose for a check to read. Each dashboard is now its api + 3, so
# a vendor's two ports stay adjacent and the whole platform stays in one block.
HOST_PORTS = {
    "contoso_pos": (18090, 18093),
    "contoso_web": (18091, 18094),
    "contoso_reference": (18092, 18095),
}
ERP_DB_PORT, ERP_BROKER_PORT, ERP_CONNECT_PORT = 55432, 19092, 18083


def _load(path: pathlib.Path) -> dict:
    """Read sources.yaml without a YAML dependency.

    Fails on anything it does not understand rather than guessing: a silently
    skipped vendor surfaces much later as an empty landing directory.
    """
    vendors: list[dict] = []
    current: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if (
            not line.strip()
            or line.strip() in ("vendors:",)
            or line.startswith("version:")
        ):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {}
            vendors.append(current)
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        current[key.strip()] = value
    return {"vendors": vendors}


def fragment(decl: dict, sources_dir: str, pins: dict) -> dict:
    services: dict = {}
    for v in decl["vendors"]:
        name = v["name"].replace("_", "-")
        kind = v.get("kind")
        if kind == "openapi":
            api_port, ui_port = HOST_PORTS.get(v["name"], (None, None))
            if api_port is None:
                raise SystemExit(
                    f"platform: vendor {v['name']!r} has no host port assigned in "
                    f"scripts/sources.py. Add one rather than letting compose pick, "
                    f"or two stacks will fight over it."
                )
            svc = {
                "image": f"mirror.gcr.io/mokapi/mokapi:{pins['MOKAPI_VERSION']}",
                # The dashboard keeps every request AND its response body in
                # memory -- for a 95 MB export that is a 246 MB JSON copy
                # retained per call, measured. One entry per API is all the
                # dashboard is used for here.
                "command": [
                    "--event-store-default-size=1",
                    f"/sources/{v['spec']}",
                    f"/sources/{v['script']}",
                ],
                # Go does NOT read the cgroup limit. Without this the runtime
                # assumes it can grow, the heap climbs past `mem_limit`, and the
                # container is terminated mid-response.
                "environment": {"GOMEMLIMIT": v.get("memory", "1GiB")},
                "mem_limit": v.get("mem_limit", "2g"),
                # READ-ONLY, and scoped to THIS vendor: its own spec and its own
                # bytes, nothing else. A path typo cannot serve another vendor's
                # export, because that path is not in this container.
                "volumes": [
                    f"{sources_dir}/{pathlib.PurePosixPath(v['spec']).parent}:"
                    f"/sources/{pathlib.PurePosixPath(v['spec']).parent}:ro",
                    f"{sources_dir}/{v['data']}:/sources/{v['data']}:ro",
                ],
                "restart": "unless-stopped",
                "ports": [f"{api_port}:{v['port']}", f"{ui_port}:8080"],
            }
            health = v.get("health")
            if health:
                # HEALTHY MEANS THE VENDOR ENFORCES ITS CREDENTIAL, not that a
                # port is open. Without its fixture mokapi does not fail: it
                # GENERATES bodies from the OpenAPI schema and answers every
                # request 200, wrong key included. A liveness probe passes
                # happily against that, and the lie surfaces three steps later
                # as a row count that is merely plausible. wget exits non-zero
                # on 401, which is the healthy case -- hence the inverted test.
                svc["healthcheck"] = {
                    "test": [
                        "CMD-SHELL",
                        f"wget -q -O /dev/null "
                        f"--header='X-Api-Key: definitely-not-the-key' "
                        f"http://localhost:{v['port']}{health} && exit 1 || exit 0",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 5,
                }
            services[name] = svc
        elif kind == "cdc":
            # THREE SERVICES, because a change stream needs all three and any
            # two of them is a snapshot wearing a stream's name.
            db, broker, connect = f"{name}-db", f"{name}-broker", f"{name}-connect"
            services[db] = {
                "image": f"mirror.gcr.io/library/postgres:{pins['POSTGRES_VERSION']}",
                # wal_level=logical is not tuning -- without it Postgres emits no
                # logical replication stream at all and Debezium fails at
                # connector creation with a message naming neither this setting
                # nor the cause.
                "command": [
                    "postgres",
                    "-c",
                    "wal_level=logical",
                    "-c",
                    "max_replication_slots=4",
                    "-c",
                    "max_wal_senders=4",
                ],
                "environment": {
                    "POSTGRES_USER": v.get("db_user", "contoso"),
                    "POSTGRES_PASSWORD": v.get("db_password", "contoso-erp-dev"),
                    "POSTGRES_DB": v.get("db_name", "erp"),
                },
                "ports": [f"{ERP_DB_PORT}:5432"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        f"pg_isready -U {v.get('db_user', 'contoso')} "
                        f"-d {v.get('db_name', 'erp')}",
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 20,
                },
                "volumes": [f"{sources_dir}:/sources:ro"],
            }
            services[broker] = {
                "image": (
                    "docker.redpanda.com/redpandadata/redpanda:"
                    f"{pins['REDPANDA_VERSION']}"
                ),
                # TWO LISTENERS. A broker tells clients where to reconnect, so a
                # single internal advertisement is correct for Debezium and
                # unusable from the host, which cannot resolve that name. The
                # failure is librdkafka's `Host resolution failure`, which names
                # the symptom and not the listener.
                "command": [
                    "redpanda",
                    "start",
                    "--mode=dev-container",
                    "--smp=1",
                    f"--kafka-addr=INTERNAL://0.0.0.0:9092,"
                    f"EXTERNAL://0.0.0.0:{ERP_BROKER_PORT}",
                    f"--advertise-kafka-addr=INTERNAL://{broker}:9092,"
                    f"EXTERNAL://localhost:{ERP_BROKER_PORT}",
                ],
                "ports": [f"{ERP_BROKER_PORT}:{ERP_BROKER_PORT}"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "rpk cluster health | grep -q 'Healthy:.*true'",
                    ],
                    "interval": "5s",
                    "timeout": "5s",
                    "retries": 30,
                },
            }
            services[connect] = {
                "image": f"mirror.gcr.io/debezium/connect:{pins['DEBEZIUM_VERSION']}",
                "depends_on": {
                    db: {"condition": "service_healthy"},
                    broker: {"condition": "service_healthy"},
                },
                "environment": {
                    "BOOTSTRAP_SERVERS": f"{broker}:9092",
                    "GROUP_ID": v["name"],
                    "CONFIG_STORAGE_TOPIC": "_connect_configs",
                    "OFFSET_STORAGE_TOPIC": "_connect_offsets",
                    "STATUS_STORAGE_TOPIC": "_connect_status",
                    # One partition each: ordering per key is what CDC
                    # guarantees, and more would trade it for throughput this
                    # does not need.
                    "CONFIG_STORAGE_REPLICATION_FACTOR": "1",
                    "OFFSET_STORAGE_REPLICATION_FACTOR": "1",
                    "STATUS_STORAGE_REPLICATION_FACTOR": "1",
                },
                "ports": [f"{ERP_CONNECT_PORT}:8083"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "curl -sf http://localhost:8083/connectors || exit 1",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 30,
                },
            }
        else:
            raise SystemExit(
                f"platform: vendor {v['name']!r} declares kind={kind!r}, which this "
                f"platform does not know how to run. Add it here or fix the "
                f"declaration; guessing would stand up the wrong vendor."
            )
    return {"services": services}


def host_ports(frag: dict) -> dict:
    """{service: [host ports]} for everything this fragment publishes.

    DERIVED FROM THE FRAGMENT, not from a second table beside it. The host
    ports live in `HOST_PORTS` and the `ERP_*` constants, and a function that
    re-listed them here would be a copy free to drift from what compose is
    actually handed -- which is the whole reason this is being written down.

    It exists because the fragment is GENERATED at `make up` and gitignored,
    so nothing committed anywhere records these ports: the family registry
    could not see them, and neither could the check that refuses two members
    claiming one host port.
    """
    out: dict[str, list[int]] = {}
    for name, svc in frag.get("services", {}).items():
        for mapping in svc.get("ports", []):
            host = str(mapping).split(":")[0]
            if host.isdigit():
                out.setdefault(name, []).append(int(host))
    return {k: sorted(v) for k, v in sorted(out.items())}


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[3] == "--ports":
        sys.argv = sys.argv[:3]
        ports = True
    else:
        ports = False
    if len(sys.argv) != 3:
        sys.exit("usage: sources.py <path-to-sources.yaml> <sources-dir-abs> [--ports]")
    decl = _load(pathlib.Path(sys.argv[1]))
    if not decl["vendors"]:
        sys.exit("platform: that sources.yaml declares no vendors")
    # Every image this platform starts on a product's behalf is pinned by the
    # SOURCES repo. Two consumers on different mokapis are not pulling from the
    # same vendor even if the specs match.
    versions = pathlib.Path(sys.argv[2]) / "versions.env"
    pins = (
        dict(
            line.split("=", 1)
            for line in versions.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.strip().startswith("#")
        )
        if versions.exists()
        else {}
    )
    pins = {k.strip(): val.strip() for k, val in pins.items()}
    needed = {
        "openapi": ["MOKAPI_VERSION"],
        "cdc": ["POSTGRES_VERSION", "REDPANDA_VERSION", "DEBEZIUM_VERSION"],
    }
    for v in decl["vendors"]:
        for key in needed.get(v.get("kind"), []):
            if key not in pins:
                sys.exit(
                    f"platform: vendor {v['name']!r} is kind={v.get('kind')!r} but "
                    f"{versions} does not pin {key}; this platform will not guess it"
                )
    frag = fragment(decl, sys.argv[2], pins)
    print(json.dumps(host_ports(frag) if ports else frag, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
