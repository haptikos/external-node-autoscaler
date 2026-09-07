"""What has to hold about the material a machine boots with.

Every failure here would otherwise surface minutes into a peering, on a machine
that is already billing, naming neither certificates nor this module.

Nothing on disk: `make no-secrets-in-git` greps for PEM headers, and it is right
to. Everything is generated at run time.
"""
import base64
import datetime
import json
import unittest

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from autoscaler import pki
from autoscaler.providers import cloudinit


def rel_path(f):
    return f.path[len(cloudinit.K3S_TLS_DIR) + 1:]


class PkiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pki, cls.creds = pki.generate("external-ovh-test")

    def decode(self, rel):
        return base64.b64decode(self.pki[f"{cloudinit.K3S_TLS_DIR}/{rel}"])

    def cert(self, rel):
        return x509.load_pem_x509_certificate(self.decode(rel))

    # ------------------------------------------------------- the file set --
    def test_generates_exactly_the_declared_file_set(self):
        """The all-or-none constraint: k3s bypasses CA generation on finding
        custom material and does not fill in what is missing."""
        self.assertEqual(set(self.pki),
                         {f.path for f in cloudinit.K3S_CA_FILES})
        self.assertEqual(len(self.pki), 11)

    def test_every_value_is_single_line_base64_pem(self):
        """render() is a plain string replace into a YAML block scalar."""
        for path, value in self.pki.items():
            self.assertNotIn("\n", value, path)
            self.assertTrue(
                base64.b64decode(value).startswith(b"-----BEGIN"), path)

    # ----------------------------------------------------- the CAs proper --
    def test_ca_certs_are_certificate_authorities(self):
        for f in cloudinit.K3S_CA_FILES:
            if not f.path.endswith(".crt"):
                continue
            rel = rel_path(f)
            with self.subTest(rel):
                cert = self.cert(rel)
                bc = cert.extensions.get_extension_for_class(
                    x509.BasicConstraints)
                self.assertTrue(bc.value.ca)
                self.assertTrue(bc.critical)
                ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
                self.assertTrue(ku.value.key_cert_sign)
                self.assertEqual(cert.issuer, cert.subject)  # self-signed

    def test_ca_keys_are_ec_p256_like_k3s_generates(self):
        for f in cloudinit.K3S_CA_FILES:
            if not f.path.endswith("-ca.key"):
                continue
            rel = rel_path(f)
            with self.subTest(rel):
                key = serialization.load_pem_private_key(
                    self.decode(rel), password=None)
                self.assertIsInstance(key, ec.EllipticCurvePrivateKey)
                self.assertIsInstance(key.curve, ec.SECP256R1)

    def test_service_key_is_rsa_and_not_a_certificate(self):
        """RSA-2048 while every CA is EC, matching k3s."""
        key = serialization.load_pem_private_key(
            self.decode("service.key"), password=None)
        self.assertIsInstance(key, rsa.RSAPrivateKey)
        self.assertEqual(key.key_size, 2048)

    # ------------------------------------------------ the admin credential --
    def test_admin_cert_is_signed_by_client_ca_and_not_server_ca(self):
        """The most consequential wiring mistake available here: k3s passes
        client-ca.crt as --client-ca-file, so a cert signed by server-ca returns
        a bare 401 with no hint, ten minutes into a peering."""
        admin = x509.load_pem_x509_certificate(self.creds.client_crt)
        client_ca = self.cert("client-ca.crt")
        server_ca = self.cert("server-ca.crt")

        self.assertEqual(admin.issuer, client_ca.subject)
        client_ca.public_key().verify(
            admin.signature, admin.tbs_certificate_bytes,
            ec.ECDSA(admin.signature_hash_algorithm))

        self.assertNotEqual(admin.issuer, server_ca.subject)
        with self.assertRaises(InvalidSignature):
            server_ca.public_key().verify(
                admin.signature, admin.tbs_certificate_bytes,
                ec.ECDSA(admin.signature_hash_algorithm))

    def test_admin_cert_carries_system_masters(self):
        """The whole authorization story on the remote side; there is no RBAC
        to create there."""
        admin = x509.load_pem_x509_certificate(self.creds.client_crt)
        orgs = admin.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        self.assertEqual([o.value for o in orgs], ["system:masters"])
        eku = admin.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH, eku.value)

    def test_certs_are_backdated_against_a_slow_ntp_settle(self):
        """A clock behind real time rejects a not-yet-valid CA at k3s startup,
        and the error reads as a TLS fault."""
        now = datetime.datetime.now(datetime.timezone.utc)
        admin = x509.load_pem_x509_certificate(self.creds.client_crt)
        self.assertLess(admin.not_valid_before_utc, now)
        self.assertGreater(admin.not_valid_after_utc,
                           now + datetime.timedelta(days=365))
        self.assertLess(self.cert("server-ca.crt").not_valid_before_utc, now)

    # ------------------------------------------------------ what is kept --
    def test_the_operator_keeps_no_ca_private_key(self):
        """A leaked hub Secret yields admin on one ephemeral machine, not the
        authority to mint credentials for it."""
        kept = (self.creds.client_crt + self.creds.client_key
                + self.creds.server_ca_crt)
        for f in cloudinit.K3S_CA_FILES:
            if not f.path.endswith(".key"):
                continue
            rel = rel_path(f)
            with self.subTest(rel):
                self.assertNotIn(self.decode(rel), kept)

    def test_each_machine_gets_its_own_authorities(self):
        """A shared CA would let a cert minted for one box authenticate to
        every box."""
        first, _ = pki.generate("external-ovh-aaaa")
        second, _ = pki.generate("external-ovh-bbbb")
        for path in first:
            self.assertNotEqual(first[path], second[path], path)

    def test_kubeconfig_embeds_server_ca_not_client_ca(self):
        """The other easy transposition: client-ca here fails the handshake."""
        cfg = json.loads(self.creds.kubeconfig("203.0.113.7"))
        cluster = cfg["clusters"][0]["cluster"]
        self.assertEqual(cluster["server"], "https://203.0.113.7:6443")
        self.assertEqual(
            base64.b64decode(cluster["certificate-authority-data"]),
            self.decode("server-ca.crt"))
        self.assertNotIn("insecure-skip-tls-verify", cluster)
        user = cfg["users"][0]["user"]
        self.assertEqual(base64.b64decode(user["client-certificate-data"]),
                         self.creds.client_crt)
        self.assertEqual(base64.b64decode(user["client-key-data"]),
                         self.creds.client_key)


if __name__ == "__main__":
    unittest.main()
