"""What the built cloud-config has to look like.

Deliberately thin on structural checks: the document is built from a dict and
serialised, so a `: ` cannot turn a runcmd entry into a mapping, a value cannot
truncate the document at the wrong indentation, and no placeholder can survive.
The serialiser owns all three. What is left is what it cannot own — the file set
being complete, the modes being right, and the script staying readable.
"""
import base64
import unittest

import yaml

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import pki
from autoscaler.providers import cloudinit
from autoscaler.providers.base import Bootstrap
from autoscaler.providers.ovh.config import OvhConfig

SCRIPT = OvhConfig.provision_script


def bootstrap(**over):
    machine_pki, _ = pki.generate("external-ovh-0001")
    kw = dict(name="external-ovh-0001", liqo_version="v1.2.0", pki=machine_pki,
              hub_egress_ips=("203.0.113.10", "203.0.113.11"),
              ssh_public_keys=())
    kw.update(over)
    return Bootstrap(**kw)


def built(gpu=True, **over):
    return yaml.safe_load(cloudinit.build(bootstrap(**over), SCRIPT, gpu=gpu))


class DocumentTest(unittest.TestCase):
    def test_it_starts_with_the_cloud_config_marker(self):
        """cloud-init identifies user data by this line and silently ignores a
        document without it."""
        raw = cloudinit.build(bootstrap(), SCRIPT)
        self.assertTrue(raw.startswith("#cloud-config\n"))

    def test_runcmd_entries_are_strings(self):
        """A ': ' inside an unquoted list item parses as a mapping, cloud-init
        rejects the whole runcmd module, and the machine boots having installed
        nothing."""
        doc = built()
        self.assertEqual(doc["runcmd"], [cloudinit.SCRIPT_PATH])
        for entry in doc["runcmd"]:
            self.assertIsInstance(entry, str)

    def test_write_files_entries_are_well_formed(self):
        for wf in built()["write_files"]:
            with self.subTest(wf.get("path")):
                self.assertIsInstance(wf.get("path"), str)
                self.assertIsInstance(wf.get("content"), str)

    def test_empty_ssh_keys_omit_the_key_entirely(self):
        """`ssh_authorized_keys:` with nothing under it parses as null and
        cloud-init rejects the document."""
        self.assertNotIn("ssh_authorized_keys", built(ssh_public_keys=()))
        self.assertNotIn("ssh_authorized_keys",
                         built(ssh_public_keys=("", "  ")))

    def test_ssh_keys_are_a_list_when_present(self):
        doc = built(ssh_public_keys=("ssh-rsa AAAA", "ssh-ed25519 BBBB"))
        self.assertEqual(doc["ssh_authorized_keys"],
                         ["ssh-rsa AAAA", "ssh-ed25519 BBBB"])


class ScriptTest(unittest.TestCase):
    def test_the_script_is_shipped_byte_for_byte(self):
        """Read verbatim, never templated — so the file on disk is exactly what
        runs on the machine, and `bash -n` on it means something."""
        doc = built()
        shipped = next(w for w in doc["write_files"]
                       if w["path"] == cloudinit.SCRIPT_PATH)
        with open(SCRIPT, encoding="utf-8") as fh:
            self.assertEqual(shipped["content"], fh.read())
        self.assertEqual(shipped["permissions"], "0700")

    def test_the_script_is_emitted_as_a_readable_block(self):
        """Silent-degradation guard. PyYAML cannot use literal block style for a
        string it is escaping, so a single non-ASCII character anywhere in the
        script collapses it into one `"#!/bin/bash\\n# What a rented..."` scalar.
        It still boots; it is just unreadable in `cloud-init query userdata` when
        someone is trying to work out why a machine failed.
        """
        raw = cloudinit.build(bootstrap(), SCRIPT)
        i = raw.index(f"path: {cloudinit.SCRIPT_PATH}")
        self.assertIn("content: |", raw[i:i + 200])

    def test_the_env_file_carries_what_the_script_sources(self):
        doc = built(gpu=False)
        env = next(w for w in doc["write_files"]
                   if w["path"] == cloudinit.ENV_PATH)
        keys = dict(line.split("=", 1) for line in
                    env["content"].strip().splitlines())
        self.assertEqual(set(keys),
                         {"NAME", "GPU", "LIQO_VERSION", "HUB_EGRESS_IPS"})
        self.assertEqual(keys["GPU"], "false")
        self.assertEqual(env["permissions"], "0600")

    def test_env_values_are_shell_quoted(self):
        """An env file is executed by the shell that sources it."""
        env = next(w for w in built()["write_files"]
                   if w["path"] == cloudinit.ENV_PATH)
        self.assertIn("HUB_EGRESS_IPS='203.0.113.10 203.0.113.11'",
                      env["content"])


class GpuFlagTest(unittest.TestCase):
    """One script, two modes. The flag reaches the machine through the env file,
    so the two renders differ in exactly one line of user data."""

    def label(self, gpu):
        doc = built(gpu=gpu)
        cfg = yaml.safe_load(next(w for w in doc["write_files"]
                                  if w["path"].endswith("k3s/config.yaml"))["content"])
        return cfg["node-label"]

    def test_gpu_true_labels_the_node_accordingly(self):
        self.assertIn("byon/gpu=true", self.label(True))

    def test_gpu_false_labels_the_node_accordingly(self):
        self.assertIn("byon/gpu=false", self.label(False))

    def test_only_the_env_and_the_label_differ_between_modes(self):
        """If a GPU difference ever leaks into the document itself, the two
        modes have stopped being one script and the CPU dry-run stops being a
        faithful rehearsal of the GPU path."""
        # One bootstrap for both renders, so the PKI is identical and any
        # difference is genuinely the flag.
        b = bootstrap()
        on = cloudinit.build(b, SCRIPT, gpu=True).splitlines()
        off = cloudinit.build(b, SCRIPT, gpu=False).splitlines()
        differing = [(a, c) for a, c in zip(on, off) if a != c]
        self.assertEqual(len(on), len(off), "documents differ in length")
        self.assertEqual(len(differing), 2, f"unexpected divergence: {differing}")
        self.assertTrue(all("true" in a and "false" in c for a, c in differing),
                        differing)


class CaFilesTest(unittest.TestCase):
    def test_every_declared_ca_file_is_written(self):
        """THE ALL-OR-NONE CONSTRAINT. k3s bypasses its own CA generation on
        finding custom material and does not fill in what is missing, so ten of
        eleven yields a server that starts, reports Ready, and fails somewhere
        that looks nothing like certificates."""
        paths = {w["path"] for w in built()["write_files"]}
        for f in cloudinit.K3S_CA_FILES:
            self.assertIn(f.path, paths)

    def test_modes_and_ownership_are_right(self):
        """A key at 0644 is not a boot failure, so nothing else would notice."""
        wf = {w["path"]: w for w in built()["write_files"]}
        for f in cloudinit.K3S_CA_FILES:
            with self.subTest(f.path):
                self.assertEqual(wf[f.path]["permissions"], f.mode)
                self.assertEqual(wf[f.path]["owner"], "root:root")
                self.assertEqual(wf[f.path]["encoding"], "b64")

    def test_ca_content_is_valid_base64(self):
        wf = {w["path"]: w for w in built()["write_files"]}
        for f in cloudinit.K3S_CA_FILES:
            with self.subTest(f.path):
                decoded = base64.b64decode(wf[f.path]["content"], validate=True)
                self.assertTrue(decoded.startswith(b"-----BEGIN"))

    def test_keys_are_stricter_than_certs(self):
        for f in cloudinit.K3S_CA_FILES:
            expected = "0600" if f.path.endswith(".key") else "0640"
            self.assertEqual(f.mode, expected, f.path)


if __name__ == "__main__":
    unittest.main()
