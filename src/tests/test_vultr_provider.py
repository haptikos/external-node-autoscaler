"""The Vultr-shaped ways this can go wrong, pinned.

Every test here is a trap that differs from OVH, because the provider that came
first is what someone will be copying from. Two of them are the mirror image of
bugs this repo has already paid for once.
"""
import base64
import calendar
import os
import time
import unittest
from unittest import mock

import requests

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import pki
from autoscaler.providers.base import Bootstrap, Machine, ProviderConfigError
from autoscaler.providers.vultr.config import VultrConfig, VultrPlacement
from autoscaler.providers.vultr.provider import (
    VultrApiError, VultrProvider, _Client, created_epoch, gpu_brand, gpu_count,
    is_capacity_error, needs_block_storage, public_ipv4)

# ALL FIVE ARE ABRIDGED FROM THE LIVE /v2/plans RESPONSE, not invented. The
# first cut of this provider guessed the GPU fields (`gpu_type`, `gpu_vram_gb`)
# and was wrong about all of them, so these keep the real names and the real
# value shapes -- including gpu_count arriving as a STRING, and as a FRACTION.
#
# Two vCPU, 4 GB. `gpu_brand: "none"` is on every non-GPU plan, which is why it
# cannot be used as a truthiness test.
PLAN_CPU = {"id": "vc2-2c-4gb", "vcpu_count": 2, "ram": 4096, "type": "vc2",
            "gpu_brand": "none", "locations": ["ewr", "lax"]}
# vcg: shared Cloud GPU. Carries a count, and it is a whole one here.
PLAN_GPU = {"id": "vcg-l40s-16c-180g-48vram", "vcpu_count": 16, "ram": 184320,
            "type": "vcg", "gpu_brand": "NVIDIA", "gpu_type": "NVIDIA_L40S",
            "gpu_vram_gb": 48, "gpu_count": "1", "locations": ["ewr"]}
# vcg fractional: a quarter of an A40. gpu_count is "1/4".
PLAN_FRACTIONAL = {"id": "vcg-a40-6c-30g-12vram", "vcpu_count": 6, "ram": 30720,
                   "type": "vcg", "gpu_brand": "NVIDIA",
                   "gpu_type": "NVIDIA_A40", "gpu_vram_gb": 12,
                   "gpu_count": "1/4", "locations": ["ewr"]}
# vdm: dedicated metal, a whole card, and NO count field at all.
PLAN_METAL = {"id": "vcg-a40-24c-120g-48vram", "vcpu_count": 24, "ram": 122880,
              "type": "vdm", "gpu_brand": "NVIDIA", "locations": ["lhr"]}
# vdm, AMD. provision.sh installs the NVIDIA stack, so this cannot work.
PLAN_AMD = {"id": "vcg-mi325x-252c-2872g-1536vram", "vcpu_count": 252,
            "ram": 2940928, "type": "vdm", "gpu_brand": "AMD",
            "locations": ["ewr"]}
PLAN_ELSEWHERE = {"id": "vc2-8c-32gb", "vcpu_count": 8, "ram": 32768,
                  "type": "vc2", "gpu_brand": "none", "locations": ["lax"]}
# vx1: no local disk. Renting one needs a block volume created and passed at
# instance-create, and Vultr refuses the plain payload with
# "One or more block devices are required."
PLAN_BLOCK_ONLY = {"id": "vx1-g-2c-8g", "vcpu_count": 2, "ram": 8192,
                   "type": "vx1", "gpu_brand": "none",
                   "storage_type": "block_storage", "locations": ["ewr"]}
# The other half of the vx1 family HAS a local disk and is fine.
PLAN_LOCAL_AND_BLOCK = {"id": "vx1-l-2c-8g", "vcpu_count": 2, "ram": 8192,
                        "type": "vx1", "gpu_brand": "none",
                        "storage_type": "local_and_block_storage",
                        "locations": ["ewr"]}

# Generated once: every create() renders the cloud-config, which needs a
# complete CA set, and keygen is the slowest thing in this file.
MACHINE_PKI, _ = pki.generate("external-vultr-0001")


def bootstrap(name="external-vultr-0001"):
    return Bootstrap(name=name, liqo_version="v1.2.0", pki=MACHINE_PKI,
                     hub_egress_ips=(), ssh_public_keys=(), pool="cpu-dryrun")


class FakeClient:
    """Just the calls the provider makes, holding real state."""

    def __init__(self, plans=(PLAN_CPU, PLAN_GPU, PLAN_FRACTIONAL,
                          PLAN_METAL, PLAN_AMD, PLAN_ELSEWHERE,
                          PLAN_BLOCK_ONLY, PLAN_LOCAL_AND_BLOCK),
                 instances=(), post_errors=None, delete_error=None):
        self.plans = list(plans)
        self.instances = list(instances)
        #: plan id -> exception to raise for it, so a test can make one plan
        #: refuse and check the fallback reaches the next.
        self.post_errors = post_errors or {}
        self.delete_error = delete_error
        self.posts = []
        self.deletes = []

    def get_all(self, path, key, **params):
        if path == "/plans":
            return self.plans
        if path == "/instances":
            return self.instances
        if path == "/os":
            return [{"id": 2284, "name": "Ubuntu 24.04 LTS x64"}]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, payload):
        self.posts.append((path, payload))
        err = self.post_errors.get(payload.get("plan"))
        if err:
            raise err
        return {"instance": {"id": "inst-1"}}

    def delete(self, path):
        self.deletes.append(path)
        if self.delete_error:
            raise self.delete_error
        return {}


def provider(client=None, **cfg_over):
    cfg = VultrConfig.from_dict({"provisionScript": "/dev/null", **cfg_over})
    return VultrProvider(cfg, client=client or FakeClient())


def placement(**over):
    return VultrPlacement.from_dict({"flavors": ["vc2-2c-4gb"], "gpu": False,
                                     **over})


class TheUnitTrap(unittest.TestCase):
    """Vultr's ram is MB. OVH's is GB. Reading this one as GB makes every shape
    a thousand times too big, so the packer fits the whole workload onto one
    machine and the pool never scales up — the mirror image of the OVH bug that
    made it rent a machine per pod."""

    def test_ram_is_megabytes(self):
        shapes = provider().flavor_capacity(placement())
        # 4096 MB, in bytes. Not 4096 GB.
        self.assertEqual(shapes["vc2-2c-4gb"]["memory"], 4_096_000_000)

    def test_a_four_gig_plan_is_about_four_gig(self):
        """The assertion above passes for any consistent unit; this one fails
        if the exponent is wrong in either direction."""
        gb = provider().flavor_capacity(placement())["vc2-2c-4gb"]["memory"] / 1e9
        self.assertTrue(3 < gb < 5, f"a 4GB plan came out as {gb}GB")

    def test_vcpus_are_millicores(self):
        """A bare int is read as ALREADY canonical, so vcpu_count=2 would mean
        two millicores and the pool would look 1000x smaller than it is."""
        self.assertEqual(provider().flavor_capacity(placement())["vc2-2c-4gb"]["cpu"],
                         2000)


class GpuFieldsTest(unittest.TestCase):
    """The shape of Vultr's GPU reporting, which the first cut of this provider
    got wrong in three separate ways at once."""

    def shape(self, flavor, region=None):
        return provider().flavor_capacity(
            placement(flavors=[flavor], gpu=True, region=region))[flavor]

    def test_gpu_brand_none_is_not_a_gpu(self):
        """It is on EVERY plan, so `if plan.get("gpu_brand")` is true for a
        1-core 512MB box."""
        self.assertEqual(gpu_brand(PLAN_CPU), "")
        self.assertEqual(gpu_brand(PLAN_METAL), "nvidia")

    def test_a_whole_card_count_is_read_from_the_api(self):
        """gpu_count arrives as a STRING."""
        self.assertEqual(gpu_count(PLAN_GPU), 1)
        self.assertEqual(self.shape("vcg-l40s-16c-180g-48vram")["nvidia.com/gpu"], 1)

    def test_a_fractional_count_is_not_rounded_to_one(self):
        """"1/4" of an A40 is a real value in this field. The guest of a WORKING
        vGPU would see one device, but provision.sh installs the stock apt
        driver, which cannot drive vGPU -- so the machine never finishes booting
        and a reported capacity would have the loop rent one every pass."""
        self.assertIsNone(gpu_count(PLAN_FRACTIONAL))
        self.assertNotIn("nvidia.com/gpu", self.shape("vcg-a40-6c-30g-12vram"))

    def test_a_fractional_flavor_says_why_at_startup(self):
        with self.assertLogs("provider.vultr", "WARNING") as caught:
            self.shape("vcg-a40-6c-30g-12vram")
        self.assertIn("FRACTIONAL", "\n".join(caught.output))

    def test_dedicated_metal_reports_no_count_and_none_is_invented(self):
        """vdm plans carry gpu_brand and nothing else. Absent means unmeasured
        and the loop rents one to find out; a count guessed from the flavor
        name would size every future pass off the guess."""
        self.assertIsNone(gpu_count(PLAN_METAL))
        self.assertNotIn("nvidia.com/gpu",
                         self.shape("vcg-a40-24c-120g-48vram", region="lhr"))

    def test_cpu_and_memory_are_still_reported_for_an_unsized_gpu_flavor(self):
        shape = self.shape("vcg-a40-24c-120g-48vram", region="lhr")
        self.assertEqual(shape["cpu"], 24000)
        self.assertEqual(shape["memory"], 122_880_000_000)

    def test_an_amd_flavor_never_advertises_nvidia_gpu(self):
        """provision.sh installs the NVIDIA stack. An AMD card needs ROCm and a
        different device plugin, so this machine has no working driver at all --
        not merely an unknown count."""
        self.assertNotIn("nvidia.com/gpu",
                         self.shape("vcg-mi325x-252c-2872g-1536vram"))

    def test_an_amd_flavor_says_so_at_startup(self):
        with self.assertLogs("provider.vultr", "WARNING") as caught:
            self.shape("vcg-mi325x-252c-2872g-1536vram")
        self.assertIn("NVIDIA stack", "\n".join(caught.output))

    def test_a_non_gpu_flavor_in_a_gpu_pool_is_still_flagged(self):
        with self.assertLogs("provider.vultr", "WARNING") as caught:
            self.shape("vc2-2c-4gb")
        self.assertIn("no GPU on it", "\n".join(caught.output))


class RegionalCatalogTest(unittest.TestCase):
    def test_a_plan_not_sold_here_is_not_offered(self):
        """It is invisible, not out of stock — so it must not be sized on
        either, or the pool plans against capacity it can never rent."""
        shapes = provider().flavor_capacity(placement(flavors=["vc2-8c-32gb"]))
        self.assertEqual(shapes, {})

    def test_a_plan_not_sold_here_is_never_rented(self):
        client = FakeClient()
        p = provider(client)
        self.assertIs(p.create(bootstrap(), placement(flavors=["vc2-8c-32gb"])),
                      False)
        self.assertEqual(client.posts, [])

    def test_the_region_override_selects_a_different_catalog(self):
        p = provider()
        self.assertEqual(p.region_for(placement(region="lax")), "lax")
        shapes = p.flavor_capacity(placement(flavors=["vc2-8c-32gb"], region="lax"))
        self.assertIn("vc2-8c-32gb", shapes)


class BlockStorageTest(unittest.TestCase):
    """A flavor with no local disk is refused at create with `400 Server add
    failed: One or more block devices are required.` -- a genuinely bad request,
    so is_capacity_error correctly declines to read it as a stock shortage and
    it raises. Renting one properly means creating a block volume, passing it,
    and deleting it afterwards: a second resource with its own lifecycle that
    leaks and keeps billing if a teardown misses it. So it is excluded."""

    def test_block_only_is_detected(self):
        self.assertTrue(needs_block_storage(PLAN_BLOCK_ONLY))

    def test_a_local_disk_alongside_block_is_fine(self):
        """Half the vx1 family has a local disk to boot from. Excluding the
        whole family on the type prefix would drop 18 usable flavors."""
        self.assertFalse(needs_block_storage(PLAN_LOCAL_AND_BLOCK))
        self.assertFalse(needs_block_storage(PLAN_CPU))

    def test_it_is_not_sized(self):
        """Sizing it would have the packer plan against capacity that can never
        be rented."""
        shapes = provider().flavor_capacity(placement(flavors=["vx1-g-2c-8g"]))
        self.assertEqual(shapes, {})

    def test_it_is_never_rented(self):
        """The bug this replaces: create() attempted it every pass and the 400
        propagated as a traceback."""
        client = FakeClient()
        self.assertIs(provider(client).create(bootstrap(),
                                              placement(flavors=["vx1-g-2c-8g"])),
                      False)
        self.assertEqual(client.posts, [])

    def test_a_usable_flavor_after_it_is_still_reached(self):
        """Excluding must not stop the fall-through, or one bad entry disables
        the rest of the list."""
        client = FakeClient()
        got = provider(client).create(
            bootstrap(), placement(flavors=["vx1-g-2c-8g", "vc2-2c-4gb"]))
        self.assertEqual(got, "vc2-2c-4gb")

    def test_it_says_why_at_startup(self):
        with self.assertLogs("provider.vultr", "WARNING") as caught:
            provider().flavor_capacity(placement(flavors=["vx1-g-2c-8g"]))
        self.assertIn("block volume", "\n".join(caught.output))


class AddressTest(unittest.TestCase):
    def test_a_pending_instance_has_no_address_rather_than_0000(self):
        """0.0.0.0 is truthy. Passed through it reaches the kubeconfig and the
        peering fails on connect, reading like a network fault."""
        self.assertIsNone(public_ipv4({"main_ip": "0.0.0.0"}))

    def test_an_assigned_address_is_returned(self):
        self.assertEqual(public_ipv4({"main_ip": "198.51.100.2"}), "198.51.100.2")

    def test_a_missing_key_is_not_an_error(self):
        self.assertIsNone(public_ipv4({}))

    def test_list_machines_reports_a_pending_instance_with_no_ip(self):
        client = FakeClient(instances=[
            {"id": "i1", "label": "external-vultr-0001", "status": "pending",
             "server_status": "none", "main_ip": "0.0.0.0",
             "date_created": "2026-08-22T10:00:00+00:00"}])
        machine = provider(client).list_machines("external-")["external-vultr-0001"]
        self.assertIsNone(machine.ip)


class TimestampTest(unittest.TestCase):
    def test_created_is_parsed_as_utc(self):
        instance = {"date_created": "2026-08-22T10:00:00+00:00"}
        self.assertEqual(created_epoch(instance),
                         calendar.timegm((2026, 8, 22, 10, 0, 0, 0, 0, 0)))

    def test_the_local_timezone_does_not_move_the_boot_deadline(self):
        """time.mktime reads the struct as LOCAL time, so on any pod not running
        in UTC every machine's age was wrong by the offset — which silently
        moves the boot-timeout deadline in whichever direction the pod's
        timezone points."""
        instance = {"date_created": "2026-08-22T10:00:00+00:00"}
        seen = []
        for tz in ("UTC", "Asia/Tokyo", "America/Los_Angeles"):
            with mock.patch.dict(os.environ, {"TZ": tz}):
                time.tzset()
                seen.append(created_epoch(instance))
        time.tzset()
        self.assertEqual(len(set(seen)), 1, f"age shifted with the timezone: {seen}")


class NameIsIdentityTest(unittest.TestCase):
    def test_the_name_reaches_vultr_verbatim(self):
        """It is the k3s node name, the Liqo cluster id, the tenant namespace
        suffix and the virtual node name. Every teardown path matches on it."""
        client = FakeClient()
        provider(client).create(bootstrap("external-vultr-abc123"), placement())
        payload = client.posts[0][1]
        self.assertEqual(payload["label"], "external-vultr-abc123")
        self.assertEqual(payload["hostname"], "external-vultr-abc123")

    def test_list_machines_keys_on_the_label(self):
        client = FakeClient(instances=[
            {"id": "i1", "label": "external-vultr-0001", "status": "active",
             "server_status": "ok", "main_ip": "198.51.100.2",
             "date_created": "2026-08-22T10:00:00+00:00"}])
        self.assertIn("external-vultr-0001",
                      provider(client).list_machines("external-"))

    def test_someone_elses_instance_is_not_returned(self):
        """A shared account is the normal case, and every reap path acts on
        what this returns."""
        client = FakeClient(instances=[
            {"id": "i1", "label": "my-database", "status": "active",
             "server_status": "ok", "main_ip": "198.51.100.3",
             "date_created": "2026-08-22T10:00:00+00:00"}])
        self.assertEqual(provider(client).list_machines("external-"), {})


class UserDataTest(unittest.TestCase):
    def test_user_data_is_base64(self):
        """OVH takes the document raw. Sent raw here, cloud-init sees garbage:
        the machine boots into nothing and it looks like a broken script."""
        client = FakeClient()
        provider(client).create(bootstrap(), placement())
        decoded = base64.b64decode(client.posts[0][1]["user_data"]).decode()
        self.assertTrue(decoded.startswith("#cloud-config"), decoded[:40])

    def test_the_document_carries_this_machines_cas(self):
        client = FakeClient()
        provider(client).create(bootstrap(), placement())
        decoded = base64.b64decode(client.posts[0][1]["user_data"]).decode()
        self.assertIn("/var/lib/rancher/k3s/server/tls/server-ca.crt", decoded)


class BillableOptionsTest(unittest.TestCase):
    """Options that cost money and default ON in Vultr's own deploy form.

    Nothing pinned these, and they are one silent key-drop away from applying to
    every machine the operator rents, for its whole life, on a bill nobody reads
    line by line.
    """

    def payload(self):
        client = FakeClient()
        provider(client).create(bootstrap(), placement())
        return client.posts[0][1]

    def test_automatic_backups_are_disabled(self):
        """+20% on every instance, and worthless here: a rented machine holds no
        state worth restoring. It is a k3s cluster of one that is reaped when
        idle, and its only durable thing -- the model cache -- is re-downloaded
        on the next machine anyway."""
        self.assertEqual(self.payload()["backups"], "disabled")

    def test_it_is_never_sent_as_enabled(self):
        """A truthy-looking value is the way this breaks: Vultr wants the
        strings "enabled"/"disabled", so `False` or `0` would be neither and the
        account default would apply."""
        self.assertNotIn(self.payload()["backups"], ("enabled", True, 1))

    def test_ipv6_is_not_requested(self):
        """Nothing here serves ingress -- traffic arrives through the Liqo
        tunnel -- and provision.sh's ufw rules and the k3s --tls-san are both
        IPv4 only, so an address nothing uses is one more thing to reason about."""
        self.assertIs(self.payload()["enable_ipv6"], False)


class CapacityErrorTest(unittest.TestCase):
    def test_no_stock_is_recognised(self):
        for message in ("Plan is not available in this location",
                        "The plan is sold out",
                        "No available capacity for this plan"):
            with self.subTest(message):
                self.assertTrue(is_capacity_error(VultrApiError(400, message)))

    def test_a_503_is_a_shortage(self):
        self.assertTrue(is_capacity_error(VultrApiError(503, "try again")))

    def test_a_real_fault_is_not_mistaken_for_a_shortage(self):
        """400 also covers a genuinely bad payload, which must raise — a broken
        request that reads as "no stock" is a pool that silently never scales."""
        for status, message in ((400, "Invalid os_id"),
                                (401, "Invalid API key"),
                                (403, "forbidden"),
                                (500, "internal error")):
            with self.subTest(f"{status} {message}"):
                self.assertFalse(is_capacity_error(VultrApiError(status, message)))

    def test_a_shortage_returns_false_rather_than_raising(self):
        """Raising took the whole pass down, including every teardown rule."""
        client = FakeClient(post_errors={
            "vc2-2c-4gb": VultrApiError(400, "Plan is not available in this location")})
        self.assertIs(provider(client).create(bootstrap(), placement()), False)

    def test_a_shortage_falls_through_to_the_next_plan(self):
        """Unlike OVH, where the binding limit is a project-wide quota and a
        smaller flavor fails the same, stock here is per plan."""
        client = FakeClient(post_errors={
            "vc2-2c-4gb": VultrApiError(400, "Plan is not available in this location")})
        got = provider(client).create(
            bootstrap(), placement(flavors=["vc2-2c-4gb", "vcg-l40s-16c-180g-48vram"]))
        self.assertEqual(got, "vcg-l40s-16c-180g-48vram")
        self.assertEqual([p[1]["plan"] for p in client.posts],
                         ["vc2-2c-4gb", "vcg-l40s-16c-180g-48vram"])

    def test_a_real_fault_is_an_error_not_a_warning(self):
        """It no longer raises -- see RefusalTest -- but it must not be logged
        as a routine shortage either, or a broken payload reads as an
        out-of-stock market and nobody looks."""
        client = FakeClient(post_errors={"vc2-2c-4gb": VultrApiError(400, "Invalid os_id")})
        with self.assertLogs("provider.vultr", "ERROR") as caught:
            provider(client).create(bootstrap(), placement())
        self.assertIn("Invalid os_id", "\n".join(caught.output))


# Verbatim from the operator log, 2026-08-22. Vultr gates its high-end GPU and
# bare-metal products behind per-account approval, and nothing in the API
# predicts it -- `deploy_ondemand` describes the plan, not your right to rent
# it -- so it can only surface here.
GATED = "Server add failed: Please open a support request for access to this product."


class RefusalTest(unittest.TestCase):
    """Every refusal is logged and skipped. No traceback, and deliberately no
    table of which message means what: Vultr's own text says what to do, and a
    classifier over those strings is a thing to maintain that goes stale in
    silence. The only split kept is severity."""

    def refuse(self, message, status=400, flavors=None):
        client = FakeClient(post_errors={"vc2-2c-4gb": VultrApiError(status, message)})
        got = provider(client).create(
            bootstrap(), placement(flavors=flavors or ["vc2-2c-4gb"]))
        return client, got

    def test_a_gated_product_does_not_raise(self):
        """The bug this replaces: a traceback per pass for a decision only a
        human can change."""
        _, got = self.refuse(GATED)
        self.assertIs(got, False)

    def test_a_broken_payload_does_not_raise_either(self):
        """Simplicity over classification, chosen deliberately: an operator
        reading `Invalid os_id` in the log knows more than any mapping of that
        string to an exception type would have told them."""
        _, got = self.refuse("Invalid os_id")
        self.assertIs(got, False)

    def test_it_is_logged_every_pass_not_once(self):
        """Suppressing after the first would leave a stuck pool looking healthy.
        The repetition IS the signal that nobody has acted on it yet."""
        client = FakeClient(post_errors={"vc2-2c-4gb": VultrApiError(400, GATED)})
        p = provider(client)
        with self.assertLogs("provider.vultr", "ERROR") as caught:
            for _ in range(3):
                p.create(bootstrap(), placement())
        gated = [line for line in caught.output if "support request" in line]
        self.assertEqual(len(gated), 3)

    def test_vultrs_own_message_reaches_the_log(self):
        """It carries the fix. Nothing here needs to know what it means."""
        client = FakeClient(post_errors={"vc2-2c-4gb": VultrApiError(400, GATED)})
        with self.assertLogs("provider.vultr", "ERROR") as caught:
            provider(client).create(bootstrap(), placement())
        self.assertIn("open a support request", "\n".join(caught.output).lower())

    def test_a_shortage_is_a_warning_not_an_error(self):
        """The one distinction worth keeping: an out-of-stock GPU market is a
        normal Tuesday and must not page anyone."""
        client = FakeClient(post_errors={
            "vc2-2c-4gb": VultrApiError(400, "Plan is not available in this location")})
        with self.assertLogs("provider.vultr", "WARNING") as caught:
            provider(client).create(bootstrap(), placement())
        self.assertFalse([line for line in caught.output if line.startswith("ERROR")])

    def test_no_api_refusal_escapes_as_an_exception(self):
        """THIS IS WHAT LEAKED CREDENTIALS. provision() writes a machine's CAs
        to a Secret BEFORE asking for the machine, then drops them on a clean
        False. An exception means "outcome unknown" -- a timeout after the API
        accepted the request looks identical -- so it deliberately HOLDS them
        for rule 6's 1200s window instead.

        Every entry below is a definite no from Vultr, so every one must come
        back False. When these raised instead, each failed pass stranded a
        Secret for twenty minutes and they piled up faster than rule 6 cleared
        them."""
        refusals = [
            (400, "Server add failed: One or more block devices are required."),
            (400, GATED),
            (400, "Plan is not available in this location"),
            (400, "Invalid os_id"),
            (401, "Invalid API key"),
            (403, "forbidden"),
            (500, "internal error"),
            (503, "try again later"),
        ]
        for status, message in refusals:
            with self.subTest(f"{status} {message[:40]}"):
                client = FakeClient(post_errors={
                    "vc2-2c-4gb": VultrApiError(status, message)})
                self.assertIs(
                    provider(client).create(bootstrap(), placement()), False)

    def test_an_unreachable_api_still_raises(self):
        """The other half, and it must NOT be swallowed: if the request never
        got an answer, Vultr may have created the instance anyway. Dropping the
        CAs then leaves a running, billing box whose credentials no longer
        exist -- unreachable, unpeerable, indistinguishable from one that never
        booted. Raising is what makes provision() hold them."""
        class Unreachable:
            def get_all(self, path, key, **kw):
                return list(FakeClient().plans) if path == "/plans" else [
                    {"id": 2284, "name": "Ubuntu 24.04 LTS x64"}]

            def post(self, path, payload):
                raise requests.ConnectionError("connection reset by peer")

        with self.assertRaises(requests.ConnectionError):
            provider(Unreachable()).create(bootstrap(), placement())

    def test_a_refusal_falls_through_to_the_next_flavor(self):
        client, got = self.refuse(
            GATED, flavors=["vc2-2c-4gb", "vcg-l40s-16c-180g-48vram"])
        self.assertEqual(got, "vcg-l40s-16c-180g-48vram")


class CreateReturnsTheFlavorTest(unittest.TestCase):
    def test_the_plan_actually_rented_comes_back(self):
        """A machine still booting has no node to measure, so the core records
        this; assuming the wrong size makes big pods rent a machine every pass."""
        self.assertEqual(provider().create(bootstrap(), placement()),
                         "vc2-2c-4gb")

    def test_the_flavors_argument_restricts_the_candidates(self):
        """The core passes only plans big enough for the pods this machine is
        for. Ignoring it — the shadowed-parameter bug OVH shipped once — rents
        one too small to run them."""
        client = FakeClient()
        got = provider(client).create(
            bootstrap(), placement(flavors=["vc2-2c-4gb", "vcg-l40s-16c-180g-48vram"]),
            flavors=["vcg-l40s-16c-180g-48vram"])
        self.assertEqual(got, "vcg-l40s-16c-180g-48vram")
        self.assertEqual([p[1]["plan"] for p in client.posts],
                         ["vcg-l40s-16c-180g-48vram"])


class DestroyTest(unittest.TestCase):
    def machine(self):
        return Machine(name="external-vultr-0001", id="inst-1", status="active",
                       created=0.0)

    def test_a_missing_instance_is_not_an_error(self):
        """Already gone by another path. The caller's job is to reach a state,
        not to be the one that got there."""
        client = FakeClient(delete_error=VultrApiError(404, "not found"))
        provider(client).destroy(self.machine())

    def test_any_other_failure_raises(self):
        """Swallowing this leaves an instance running and billing with nothing
        tracking it."""
        client = FakeClient(delete_error=VultrApiError(403, "forbidden"))
        with self.assertRaises(VultrApiError):
            provider(client).destroy(self.machine())

    def test_the_provider_native_id_is_what_gets_deleted(self):
        client = FakeClient()
        provider(client).destroy(self.machine())
        self.assertEqual(client.deletes, ["/instances/inst-1"])


class PaginationTest(unittest.TestCase):
    """An account with more than one page of instances would otherwise hide the
    rest from every reap path, leaving machines running and billing."""

    def client(self, pages):
        c = _Client("https://api.vultr.com/v2", "token")
        self.calls = []

        def fake(method, path, **kw):
            self.calls.append((path, (kw.get("params") or {}).get("cursor")))
            return pages[len(self.calls) - 1]

        c._request = fake
        return c

    def test_every_page_is_followed(self):
        c = self.client([
            {"instances": [{"id": "1"}], "meta": {"links": {"next": "cur2"}}},
            {"instances": [{"id": "2"}], "meta": {"links": {"next": ""}}},
        ])
        self.assertEqual([i["id"] for i in c.get_all("/instances", "instances")],
                         ["1", "2"])

    def test_the_cursor_is_passed_back(self):
        c = self.client([
            {"instances": [{"id": "1"}], "meta": {"links": {"next": "cur2"}}},
            {"instances": [], "meta": {"links": {}}},
        ])
        c.get_all("/instances", "instances")
        self.assertEqual([cursor for _, cursor in self.calls], [None, "cur2"])

    def test_a_single_page_makes_one_call(self):
        c = self.client([{"instances": [{"id": "1"}], "meta": {"links": {}}}])
        c.get_all("/instances", "instances")
        self.assertEqual(len(self.calls), 1)


class HttpErrorTest(unittest.TestCase):
    def response(self, status, body, text=""):
        r = mock.Mock()
        r.status_code = status
        r.ok = 200 <= status < 300
        r.content = b"x"
        r.text = text
        r.json.return_value = body
        return r

    def test_the_api_error_message_is_carried_through(self):
        c = _Client("https://api.vultr.com/v2", "token")
        c.session = mock.Mock()
        c.session.request.return_value = self.response(
            400, {"error": "Plan is not available in this location"})
        with self.assertRaises(VultrApiError) as e:
            c.get("/plans")
        self.assertEqual(e.exception.status, 400)
        self.assertTrue(is_capacity_error(e.exception))

    def test_a_204_is_not_an_error(self):
        c = _Client("https://api.vultr.com/v2", "token")
        c.session = mock.Mock()
        r = self.response(204, {})
        r.content = b""
        c.session.request.return_value = r
        self.assertEqual(c.delete("/instances/inst-1"), {})


class CredentialsTest(unittest.TestCase):
    def test_a_missing_key_names_itself_at_startup(self):
        """Without this it surfaces as a 401 on the first scale-up, which reads
        like a revoked key rather than an absent one."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderConfigError) as e:
                VultrProvider.from_config({})
        self.assertIn("VULTR_API_KEY", str(e.exception))


class OsResolutionTest(unittest.TestCase):
    def test_a_pinned_os_id_skips_the_lookup(self):
        client = FakeClient()
        client.get_all = mock.Mock(side_effect=AssertionError("should not look up"))
        p = VultrProvider(VultrConfig.from_dict({"osId": 2284}), client=client)
        self.assertEqual(p.os_id(), 2284)

    def test_the_name_is_resolved_by_substring_match(self):
        self.assertEqual(provider().os_id(), 2284)

    def test_no_match_is_a_failure_naming_the_escape_hatch(self):
        p = provider(**{"osName": "Plan 9"})
        with self.assertRaises(RuntimeError) as e:
            p.os_id()
        self.assertIn("osId", str(e.exception))


if __name__ == "__main__":
    unittest.main()
