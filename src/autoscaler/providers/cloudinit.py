"""Build cloud-init user data for a provider that rents a raw VM.

Shared by any such provider — OVH today, a Hetzner or direct-EC2 provider
tomorrow — and imported by none that does not. A provider that boots its
machines over SSH instead never touches this module: it runs the provider's
provision.sh directly, because that file is a real script with no placeholders
in it.

ASSEMBLED, NOT CONCATENATED. The document is built as a dict and serialised by
PyYAML, which is the whole design. YAML written by string substitution fails in
ways a YAML check cannot catch: a `: ` inside a runcmd entry becomes a mapping
and cloud-init refuses the whole module; a multi-line value at the wrong
indentation truncates everything after it; an empty ssh key list renders
`ssh_authorized_keys:` as null and invalidates the document. None of those are
reachable here — the serialiser owns quoting, indentation and types.

The machine-specific inputs go into one env file that provision.sh sources, so
the script itself is identical on every machine and can be linted, diffed and
executed by hand.
"""
from collections import namedtuple
from pathlib import Path
import shlex

import yaml

#: Where k3s looks for its certificate authorities.
K3S_TLS_DIR = "/var/lib/rancher/k3s/server/tls"

#: `mode` is the cloud-init `permissions` string, not an int.
CAFile = namedtuple("CAFile", "path mode")

#: THE COMPLETE SET, AND IT HAS TO BE COMPLETE. k3s bypasses automatic CA
#: generation the moment it finds custom material at first server start and does
#: NOT fill in what is missing, so ten of these eleven yields a server that
#: starts and then fails somewhere unrelated. Add, never subtract.
K3S_CA_FILES = (
    # Signs the apiserver's serving cert, which k3s still mints at boot from
    # --tls-san — hence the operator never needs the machine's address to
    # generate certificates, only to connect.
    CAFile(f"{K3S_TLS_DIR}/server-ca.crt", "0640"),
    CAFile(f"{K3S_TLS_DIR}/server-ca.key", "0600"),
    # --client-ca-file; the operator's admin cert is signed by this one.
    CAFile(f"{K3S_TLS_DIR}/client-ca.crt", "0640"),
    CAFile(f"{K3S_TLS_DIR}/client-ca.key", "0600"),
    # Aggregation layer. Wrong here and k3s starts Ready with only APIServices
    # and metrics broken, which nothing in this repo checks for.
    CAFile(f"{K3S_TLS_DIR}/request-header-ca.crt", "0640"),
    CAFile(f"{K3S_TLS_DIR}/request-header-ca.key", "0600"),
    CAFile(f"{K3S_TLS_DIR}/etcd/peer-ca.crt", "0640"),
    CAFile(f"{K3S_TLS_DIR}/etcd/peer-ca.key", "0600"),
    CAFile(f"{K3S_TLS_DIR}/etcd/server-ca.crt", "0640"),
    CAFile(f"{K3S_TLS_DIR}/etcd/server-ca.key", "0600"),
    # Service-account signing key: a bare private key, no certificate.
    CAFile(f"{K3S_TLS_DIR}/service.key", "0600"),
)

#: The provisioning script every raw-VM provider boots by default. Shared
#: rather than per-provider because nothing in it is provider-specific;
#: a provider that needs its own points `provisionScript` at that file.
DEFAULT_SCRIPT = str(Path(__file__).parent / "provision.sh")

SCRIPT_PATH = "/root/provision.sh"
ENV_PATH = "/root/provision.env"


class _Block(str):
    """A string PyYAML must emit as a literal block, not an escaped scalar."""


def _represent_block(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Block, _represent_block)


def env_file(values):
    """`KEY='value'` lines for provision.sh to source.

    Quoted with shlex: these values are machine names, a version string and an
    address list today, but an env file is executed by the shell that reads it,
    so quoting is not optional on principle.
    """
    return _Block("".join(f"{k}={shlex.quote(str(v))}\n"
                          for k, v in sorted(values.items())))


def k3s_config(bootstrap, gpu, provider):
    """/etc/rancher/k3s/config.yaml, itself serialised rather than written out.

    These labels are on the MACHINE's own node, which the hub never schedules
    against — it sees only the virtual node, labelled from the pool by
    liqo.ensure_node_metadata. So nothing selects on these. The one label that IS
    a contract is external-node-autoscaler/bootstrap, applied by provision.sh.
    """
    labels = [
        "external-node-autoscaler/managed=true",
        f"byon/hoster={provider}",
        f"byon/gpu={str(bool(gpu)).lower()}",
    ]
    if bootstrap.pool:
        labels.append(f"byon/pool={bootstrap.pool}")
    return _Block(yaml.safe_dump({
        "node-name": bootstrap.name,
        "node-label": labels,
        # Nothing here serves ingress. Model traffic arrives through the Liqo
        # tunnel from the in-cluster gateway, never from this box's own network.
        "disable": ["traefik", "servicelb"],
    }, default_flow_style=False, sort_keys=False))


def ca_write_files(pki):
    """One write_files entry per CA file, driven by K3S_CA_FILES.

    write_files runs before runcmd, which is what satisfies k3s's "before first
    server start" requirement structurally rather than by ordering care inside a
    script. cloud-init creates the etcd/ subdirectory.

    `encoding: b64` because the values are already base64 (pki.py) and because a
    certificate is not something to read in a user-data dump.
    """
    return [{
        "path": f.path,
        "encoding": "b64",
        "content": pki[f.path],
        "owner": "root:root",
        "permissions": f.mode,
    } for f in K3S_CA_FILES]


def build(bootstrap, script_path, gpu=True, provider="unknown"):
    """The complete #cloud-config document for one machine, as a string.

    `script_path` is the provider's provision.sh — read verbatim, never
    templated, so the file on disk is byte-for-byte what runs on the machine.

    `gpu` and `provider` come from the provider rather than `bootstrap`: both
    are facts about where the machine is being rented. Bootstrap carries what the
    hub supplies, in one direction only.
    """
    script = Path(script_path).read_text(encoding="utf-8")

    doc = {
        "package_update": True,
        "write_files": ca_write_files(bootstrap.pki) + [
            {"path": "/etc/rancher/k3s/config.yaml",
             "content": k3s_config(bootstrap, gpu, provider)},
            {"path": ENV_PATH,
             "owner": "root:root",
             "permissions": "0600",
             "content": env_file({
                 "NAME": bootstrap.name,
                 "GPU": str(bool(gpu)).lower(),
                 "LIQO_VERSION": bootstrap.liqo_version,
                 "HUB_EGRESS_IPS": " ".join(bootstrap.hub_egress_ips),
             })},
            {"path": SCRIPT_PATH,
             "owner": "root:root",
             "permissions": "0700",
             "content": _Block(script)},
        ],
        "runcmd": [SCRIPT_PATH],
    }

    # Omitted entirely when empty. An `ssh_authorized_keys:` with nothing under
    # it parses as null and cloud-init rejects the whole document — which is a
    # thing that can only happen if you are writing YAML by hand.
    keys = [k.strip() for k in bootstrap.ssh_public_keys if k.strip()]
    if keys:
        doc["ssh_authorized_keys"] = keys

    # allow_unicode is load-bearing, not cosmetic. Without it PyYAML escapes
    # every non-ASCII character, and an escaped string CANNOT be a literal
    # block — so the whole provision script silently collapses into one
    # "#!/bin/bash\n# What a rented..." scalar. It still boots; it is just
    # unreadable in `cloud-init query userdata` and in the OVH console when
    # someone is trying to work out why a machine failed. One em dash in a
    # comment is enough to trigger it, which is why tests/test_cloudinit.py
    # asserts the block style rather than trusting this line to stay put.
    body = yaml.dump(doc, Dumper=_Dumper, default_flow_style=False,
                     sort_keys=False, width=10_000, allow_unicode=True)
    return f"#cloud-config\n{body}"
