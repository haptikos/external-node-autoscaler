"""Per-machine certificate authorities, generated before the machine exists.

Why a rented machine never calls the hub: the operator generates that machine's
k3s CAs here and can therefore mint an admin client cert for a cluster that has
not booted yet. The model is cluster-api-k3s's (pkg/secret/certificates.go).

Per machine, never shared — a fleet-wide CA would mean a cert minted for one box
authenticates to every box. The operator keeps only the leaf credential; the CA
private keys end up solely on the machine.
"""
import base64
import datetime
import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from . import logging_setup
from .providers import cloudinit

log = logging_setup.get("pki")

#: Machines live hours, so rotation logic would be pure liability.
VALIDITY = datetime.timedelta(days=3650)

#: A VM behind real time until NTP settles rejects a not-yet-valid CA at k3s
#: startup, and the error reads as a TLS fault rather than a clock one.
BACKDATE = datetime.timedelta(minutes=5)

#: What k3s generates for itself (contrib/util/generate-custom-ca-certs.sh):
#: EC prime256v1 for all five leaf CAs, RSA-2048 for service.key.
CURVE = ec.SECP256R1


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _pem_key(key):
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def _pem_cert(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _self_signed_ca(common_name):
    """One self-signed CA, the shape k3s generates for itself."""
    key = ec.generate_private_key(CURVE())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                       critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
            key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _admin_cert(ca_key, ca_cert):
    """The operator's credential on the machine.

    SIGNED BY client-ca, NOT server-ca — k3s passes client-ca.crt as the
    apiserver's --client-ca-file, and the wrong authority gives a bare 401 with
    no hint, minutes into a peering. `O=system:masters` grants cluster-admin via
    the built-in binding, so there is no remote RBAC to create.
    """
    key = ec.generate_private_key(CURVE())
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "external-node-autoscaler"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "system:masters"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.ExtendedKeyUsage(
            [x509.ObjectIdentifier("1.3.6.1.5.5.7.3.2")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def generate(machine):
    """Returns (pki, credentials).

    pki: machine path -> single-line base64, exactly cloudinit.K3S_CA_FILES.
    Goes into user data. credentials: what the operator keeps, for the Secret.
    """
    stamp = int(_now().timestamp())
    cas = {}
    for name in ("server", "client", "request-header"):
        cas[name] = _self_signed_ca(f"k3s-{name}-ca@{stamp}")
    for name in ("peer", "server"):
        cas[f"etcd-{name}"] = _self_signed_ca(f"etcd-{name}-ca@{stamp}")

    # Signs service-account tokens rather than certificates.
    service_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    raw = {
        "server-ca.crt": _pem_cert(cas["server"][1]),
        "server-ca.key": _pem_key(cas["server"][0]),
        "client-ca.crt": _pem_cert(cas["client"][1]),
        "client-ca.key": _pem_key(cas["client"][0]),
        "request-header-ca.crt": _pem_cert(cas["request-header"][1]),
        "request-header-ca.key": _pem_key(cas["request-header"][0]),
        "etcd/peer-ca.crt": _pem_cert(cas["etcd-peer"][1]),
        "etcd/peer-ca.key": _pem_key(cas["etcd-peer"][0]),
        "etcd/server-ca.crt": _pem_cert(cas["etcd-server"][1]),
        "etcd/server-ca.key": _pem_key(cas["etcd-server"][0]),
        "service.key": _pem_key(service_key),
    }

    # Driven by K3S_CA_FILES rather than the dict above: a file declared there
    # and missing here is a machine that boots and then fails somewhere
    # unrelated.
    pki = {}
    for f in cloudinit.K3S_CA_FILES:
        rel = f.path[len(cloudinit.K3S_TLS_DIR) + 1:]
        if rel not in raw:
            raise RuntimeError(
                f"pki.generate does not produce {rel!r}, which "
                f"cloudinit.K3S_CA_FILES requires — k3s bypasses CA generation "
                f"on finding a partial set, so this must never ship")
        pki[f.path] = base64_line(raw[rel])

    client_key, client_cert = _admin_cert(*cas["client"])
    creds = Credentials(
        client_crt=_pem_cert(client_cert),
        client_key=_pem_key(client_key),
        server_ca_crt=raw["server-ca.crt"],
    )
    log.info("%s: generated %d CA files", machine, len(pki))
    return pki, creds


def base64_line(data):
    """Base64 with no line breaks — see cloudinit.substitutions on why."""
    return base64.b64encode(data).decode()


class Credentials:
    """What the operator keeps for one machine: a leaf cert, its key, and the
    CA to verify the machine with. No CA private key, by construction.

    Not a kubeconfig: the machine's address is unknown when these are generated,
    so it is derived per pass from these plus whatever the provider reports.
    """

    #: Epoch seconds, from the Secret's own creationTimestamp — the one clock
    #: here that an operator restart cannot rewind. None until persisted.
    created = None

    #: The pool this machine was rented for, from the Secret's label. None is
    #: treated as an unknown pool rather than guessed at.
    pool = None

    #: The flavor this machine was rented as, from the Secret's label. None
    #: until the provider has said which one it got.
    flavor = None

    def __init__(self, client_crt, client_key, server_ca_crt, created=None,
                 pool=None, flavor=None):
        self.client_crt = client_crt
        self.client_key = client_key
        self.server_ca_crt = server_ca_crt
        self.created = created
        self.pool = pool
        self.flavor = flavor

    def kubeconfig(self, ip):
        """A kubeconfig for this machine at `ip`, as bytes."""
        b64 = base64_line
        return json.dumps({
            "apiVersion": "v1", "kind": "Config", "current-context": "machine",
            "clusters": [{"name": "machine", "cluster": {
                "server": f"https://{ip}:6443",
                "certificate-authority-data": b64(self.server_ca_crt)}}],
            "users": [{"name": "machine", "user": {
                "client-certificate-data": b64(self.client_crt),
                "client-key-data": b64(self.client_key)}}],
            "contexts": [{"name": "machine", "context": {
                "cluster": "machine", "user": "machine"}}],
        }).encode()
