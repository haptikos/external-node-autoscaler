"""What OVH refusing to rent has to look like from the outside.

Provider.create says a shortage returns False and anything else raises. Not
tidiness: an exception used to abort the whole reconcile pass, so the condition
that stops the operator RENTING machines also stopped it RELEASING them.

Quota arrives as a bare APIError with no error code, so the message is all there
is to match on. These pin the real ones seen in production.
"""
import unittest

import ovh

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import pki
from autoscaler.providers.base import Bootstrap
from autoscaler.providers.ovh.config import OvhConfig, OvhPlacement
from autoscaler.providers.ovh.provider import OvhProvider, is_capacity_error

# Verbatim from the operator log, 2026-08-13.
QUOTA_MESSAGE = ("Quota exceeded for ram: Requested 8000, but already used "
                 "40000 of 44000 ram")


class FakeOvhClient:
    """Just the calls create() makes."""

    def __init__(self, post_raises=None):
        self.post_raises = post_raises
        self.posts = []

    def get(self, path, **kw):
        if path.endswith("/flavor"):
            return [{"name": "d2-8", "id": "flavor-1", "available": True}]
        if path.endswith("/image"):
            return [{"name": "Ubuntu 24.04", "id": "image-1"}]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, **kw):
        self.posts.append((path, kw))
        if self.post_raises:
            raise self.post_raises
        return {"id": "instance-1"}


def bootstrap():
    """Real PKI: create() renders the template, which needs every declared
    file."""
    machine_pki, _ = pki.generate("external-ovh-0001")
    return Bootstrap(name="external-ovh-0001", liqo_version="v1.2.0",
                     pki=machine_pki, hub_egress_ips=(), ssh_public_keys=(),
                     pool="gpu-a10")


def placement(**over):
    return OvhPlacement.from_dict({"flavors": ["d2-8"], **over})


class CapacityErrorTest(unittest.TestCase):
    def test_the_production_quota_message_is_recognised(self):
        self.assertTrue(is_capacity_error(ovh.exceptions.APIError(QUOTA_MESSAGE)))

    def test_recognition_is_case_insensitive(self):
        self.assertTrue(is_capacity_error(ovh.exceptions.APIError("QUOTA EXCEEDED")))

    def test_a_real_fault_is_not_mistaken_for_a_shortage(self):
        for message in ("Invalid signature",
                        "This service does not exist",
                        "Internal server error"):
            with self.subTest(message):
                self.assertFalse(
                    is_capacity_error(ovh.exceptions.APIError(message)))


class CreateTest(unittest.TestCase):
    def provider(self, post_raises=None):
        cfg = OvhConfig.from_dict({"project": "p",
                                   "provisionScript": "/dev/null"})
        return OvhProvider(cfg, client=FakeOvhClient(post_raises=post_raises))

    def test_quota_exceeded_returns_false_rather_than_raising(self):
        """Raising took the whole pass down, including every teardown rule."""
        p = self.provider(ovh.exceptions.APIError(QUOTA_MESSAGE))
        self.assertIs(p.create(bootstrap(), placement()), False)

    def test_quota_is_not_retried_across_flavors(self):
        """The binding quota is project-wide, so a smaller flavor fails the
        same and each attempt costs a round trip."""
        cfg = OvhConfig.from_dict({"project": "p",
                                   "provisionScript": "/dev/null"})
        client = FakeOvhClient(ovh.exceptions.APIError(QUOTA_MESSAGE))
        client.get = lambda path, **kw: (
            [{"name": n, "id": n, "available": True}
             for n in ("d2-8", "d2-4", "d2-2")] if path.endswith("/flavor")
            else [{"name": "Ubuntu 24.04", "id": "image-1"}])
        self.assertIs(
            OvhProvider(cfg, client=client).create(
                bootstrap(), placement(flavors=["d2-8", "d2-4", "d2-2"])),
            False)
        self.assertEqual(len(client.posts), 1)

    def test_a_real_api_error_still_raises(self):
        """Swallowing these turns a bad credential into a silent no-scale-up."""
        p = self.provider(ovh.exceptions.APIError("Invalid signature"))
        with self.assertRaises(ovh.exceptions.APIError):
            p.create(bootstrap(), placement())

    def test_a_successful_create_returns_the_flavor_it_rented(self):
        """Truthy, so the old `if created:` reads the same -- but the caller
        needs the name: a machine still booting has no node to measure, and
        sizing it as the wrong flavor makes big pods rent one every pass."""
        p = self.provider()
        self.assertEqual(p.create(bootstrap(), placement()), "d2-8")

    def test_the_flavor_subset_restricts_what_may_be_rented(self):
        """The core passes only flavors big enough for the pods this machine is
        for, so a stock shortage cannot hand back one too small to run them."""
        p = self.provider()
        self.assertIs(p.create(bootstrap(), placement(flavors=["d2-8", "d2-4"]),
                               flavors=["d2-4"]), False)


if __name__ == "__main__":
    unittest.main()
